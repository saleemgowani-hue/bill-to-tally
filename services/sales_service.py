"""
Sales orchestration — the mirror image of purchase_service.py.

  EXTRACTED -> REVIEW REQUIRED -> CONFIRMED -> READY -> POSTED
                                     |
                                     +-> DUPLICATE / CANCELLED / FAILED

The only two things that actually differ from a purchase bill:
  1. The counterparty is a Customer, not a Supplier, and stock moves DOWN
     instead of up when a sale is posted (see apply_sales_stock).
  2. The Tally voucher built is a Sales voucher (VoucherPayload.transaction_kind
     = "sales") — see services/tally_connector.build_sales_voucher_xml for the
     accounting entries this produces.

Everything else — GST/arithmetic validation, product matching, duplicate
detection, the confirm/post state machine — reuses the exact same generic
services the purchase pipeline uses (InvoiceValidator, ProductMatcher,
excel_import, ai_invoice_parser with document_type="sales").
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PIL import Image
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.database import get_mapping
from database.models import (
    Company,
    Customer,
    InvoiceStatus,
    MatchStatus,
    OCRResult,
    Product,
    SalesInvoice,
    SalesInvoiceItem,
    SalesTransaction,
    StockMovement,
    User,
)
from services import audit_service
from services.ai_invoice_parser import InvoiceParser, ParsedInvoice
from services.customer_service import find_or_create_customer
from services.invoice_validator import InvoiceValidator, ValidationReport
from services.product_matcher import ProductMatcher
from services.tally_connector import TallyConnector, TallyResult, VoucherPayload
from utils.config import settings
from utils.image_tools import file_hash, save_image
from utils.logging import get_logger

log = get_logger("sales")


class SalesError(RuntimeError):
    pass


@dataclass
class SalesIngestResult:
    invoice: SalesInvoice
    parsed: Optional[ParsedInvoice]
    report: ValidationReport
    duplicate_reason: str = ""
    warnings: List[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


# --------------------------------------------------------------------------
# Duplicate detection — mirrors invoice_validator.check_duplicate against
# SalesInvoice instead of Invoice.
# --------------------------------------------------------------------------
@dataclass
class SalesDuplicateCheck:
    is_duplicate: bool
    confidence: float
    reason: str
    invoice_id: Optional[int] = None
    invoice_label: str = ""


def check_duplicate_sales(
    session: Session,
    company_id: int,
    customer_name: str,
    invoice_number: str,
    invoice_date: str,
    grand_total: float,
    file_hash_value: str = "",
    exclude_invoice_id: Optional[int] = None,
) -> SalesDuplicateCheck:
    query = session.query(SalesInvoice).filter(
        SalesInvoice.company_id == company_id,
        SalesInvoice.status.notin_(
            [InvoiceStatus.CANCELLED.value, InvoiceStatus.DUPLICATE.value]
        ),
    )
    if exclude_invoice_id:
        query = query.filter(SalesInvoice.id != exclude_invoice_id)

    if file_hash_value:
        same_file = query.filter(SalesInvoice.file_hash == file_hash_value).first()
        if same_file:
            return SalesDuplicateCheck(
                True, 1.0,
                "The exact same bill image/sheet has already been uploaded.",
                same_file.id,
                f"{same_file.customer_name} / {same_file.invoice_number}",
            )

    customer_key = (customer_name or "").strip().lower()
    number_key = re.sub(r"[^a-z0-9]", "", (invoice_number or "").lower())

    if number_key:
        for candidate in query.all():
            cand_number = re.sub(r"[^a-z0-9]", "", (candidate.invoice_number or "").lower())
            cand_customer = (candidate.customer_name or "").strip().lower()
            if cand_number and cand_number == number_key:
                if cand_customer == customer_key:
                    return SalesDuplicateCheck(
                        True, 0.97,
                        f"Invoice {candidate.invoice_number} to "
                        f"{candidate.customer_name} was already processed on "
                        f"{candidate.created_at:%d-%m-%Y}.",
                        candidate.id,
                        f"{candidate.customer_name} / {candidate.invoice_number}",
                    )
                return SalesDuplicateCheck(
                    True, 0.70,
                    f"Invoice number {candidate.invoice_number} already exists "
                    f"(under customer '{candidate.customer_name}').",
                    candidate.id,
                    f"{candidate.customer_name} / {candidate.invoice_number}",
                )

    if customer_key and invoice_date and grand_total:
        for candidate in query.all():
            if (
                (candidate.customer_name or "").strip().lower() == customer_key
                and (candidate.invoice_date or "") == invoice_date
                and abs((candidate.grand_total or 0) - grand_total) < 1.0
            ):
                return SalesDuplicateCheck(
                    True, 0.80,
                    f"A bill to {candidate.customer_name} dated {invoice_date} "
                    f"for ₹{grand_total:,.2f} already exists.",
                    candidate.id,
                    f"{candidate.customer_name} / {candidate.invoice_number}",
                )

    return SalesDuplicateCheck(False, 0.0, "No duplicate found.")


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
def _finalize_sales_invoice(
    session: Session,
    company: Company,
    user: User,
    invoice: SalesInvoice,
    data: dict,
    validator: InvoiceValidator,
    digest: str,
    overall_confidence: float,
    ocr_provider: str,
) -> Tuple[ValidationReport, str]:
    """Shared tail of the sales ingest pipeline — mirrors
    purchase_service._finalize_invoice. The "supplier" key in `data` holds
    the CUSTOMER's details here (see ai_invoice_parser.SALES_EXTRACTION_PROMPT)."""
    customer_info = data.get("supplier") or {}
    invoice_info = data.get("invoice") or {}
    customer = find_or_create_customer(
        session, company.id,
        customer_info.get("name", ""),
        customer_info.get("gstin", ""),
        customer_info.get("address", ""),
    )

    invoice.customer_id = customer.id if customer else None
    invoice.customer_name = customer_info.get("name", "")[:200]
    invoice.customer_gstin = customer_info.get("gstin", "")[:20]
    invoice.customer_address = customer_info.get("address", "")
    invoice.invoice_number = invoice_info.get("invoice_number", "")[:80]
    invoice.invoice_date = invoice_info.get("invoice_date", "")
    invoice.due_date = invoice_info.get("due_date", "")
    invoice.subtotal = float(data.get("subtotal") or 0)
    invoice.discount_total = float(data.get("discount_total") or 0)
    invoice.tax_total = float(data.get("tax_total") or 0)
    invoice.cess_total = float(data.get("cess_total") or 0)
    invoice.round_off = float(data.get("round_off") or 0)
    invoice.grand_total = float(data.get("grand_total") or 0)
    invoice.overall_confidence = overall_confidence
    invoice.ocr_provider = ocr_provider
    extraction_notes = data.get("extraction_notes") or []
    if extraction_notes:
        invoice.notes = "\n".join(extraction_notes)

    # -- validation -----------------------------------------------------
    # Fully reused: the GST/arithmetic checks and the intra/inter-state
    # comparison (our company's GSTIN vs. the "supplier" field's GSTIN) are
    # symmetric whether that counterparty is a vendor or a customer.
    report = validator.validate(data, buyer_gstin=company.gstin or "")
    invoice.tax_scope = report.tax_scope
    invoice.validation_json = json.dumps(report.to_dict())[:60000]

    # -- product matching ------------------------------------------------
    matcher = ProductMatcher(session, company.id)
    for index, item in enumerate(data.get("items", [])):
        match = matcher.match(item, customer.id if customer else None)
        session.add(
            SalesInvoiceItem(
                invoice_id=invoice.id,
                company_id=company.id,
                product_id=match.product_id,
                raw_name=item.get("raw_name", "")[:250],
                normalized_name=item.get("normalized_name", "")[:250],
                hsn=item.get("hsn", "")[:20],
                barcode=item.get("barcode", "")[:80],
                quantity=item.get("quantity", 0.0),
                unit=item.get("unit", "")[:20],
                rate=item.get("rate", 0.0),
                discount=item.get("discount", 0.0),
                taxable_value=item.get("taxable_value", 0.0),
                gst_rate=item.get("gst_rate", 0.0),
                cgst=item.get("cgst", 0.0),
                sgst=item.get("sgst", 0.0),
                igst=item.get("igst", 0.0),
                cess=item.get("cess", 0.0),
                total=item.get("total", 0.0),
                confidence=item.get("confidence", 0.0),
                match_status=match.status,
                match_score=match.score,
                match_reason=(match.reason or "")[:250],
                line_warnings=json.dumps(report.line_flags.get(index, []))[:4000],
            )
        )

    # -- duplicate check --------------------------------------------------
    dupe = check_duplicate_sales(
        session, company.id, invoice.customer_name, invoice.invoice_number,
        invoice.invoice_date, invoice.grand_total, digest, exclude_invoice_id=invoice.id,
    )
    duplicate_reason = ""
    if dupe.is_duplicate:
        invoice.duplicate_of_id = dupe.invoice_id
        invoice.status = InvoiceStatus.DUPLICATE.value
        duplicate_reason = dupe.reason
        audit_service.record(
            session, company.id, "SALES_DUPLICATE_DETECTED", dupe.reason,
            user=user, sales_invoice_id=invoice.id, commit=False,
        )
    else:
        invoice.status = InvoiceStatus.REVIEW_REQUIRED.value

    return report, duplicate_reason


def ingest_sales_bill(
    session: Session,
    company: Company,
    user: User,
    filename: str,
    raw_bytes: bytes,
    images: List[Image.Image],
    parser: InvoiceParser,
    validator: Optional[InvoiceValidator] = None,
    store_image: bool = True,
) -> SalesIngestResult:
    """Extract → match → validate a sales invoice photo, and persist it."""
    validator = validator or InvoiceValidator()
    digest = file_hash(raw_bytes)

    invoice = SalesInvoice(
        company_id=company.id,
        status=InvoiceStatus.PROCESSING.value,
        source_file=filename[:255],
        source_pages=len(images),
        file_hash=digest,
        created_by=user.id,
    )
    session.add(invoice)
    session.flush()

    audit_service.record(
        session, company.id, "SALES_BILL_UPLOADED",
        f"Uploaded '{filename}' ({len(images)} page(s), {len(raw_bytes)/1024:.0f} KB)",
        user=user, sales_invoice_id=invoice.id, commit=False,
    )

    if store_image and images:
        try:
            folder = settings.UPLOAD_DIR / str(company.id) / "sales" / str(invoice.id)
            for index, img in enumerate(images, start=1):
                save_image(img, folder / f"page_{index}.jpg")
            invoice.source_file = f"{folder}/page_1.jpg"
        except Exception as exc:
            audit_service.record_error(
                session, company.id, "storage", f"Could not store page image: {exc}",
                exc, sales_invoice_id=invoice.id, commit=False,
            )

    parsed = parser.parse_images(images, document_type="sales")
    data = parsed.data

    session.add(
        OCRResult(
            company_id=company.id,
            sales_invoice_id=invoice.id,
            provider=parsed.provider,
            model=parsed.model,
            raw_text=(parsed.raw_text or "")[:60000],
            raw_json=(parsed.raw_json or "")[:60000],
            repaired=parsed.repaired,
            attempts=parsed.attempts,
            duration_ms=parsed.duration_ms,
        )
    )
    audit_service.record(
        session, company.id, "SALES_AI_EXTRACTED",
        f"{parsed.provider}/{parsed.model} extracted {len(data.get('items', []))} "
        f"line(s) at {parsed.overall_confidence:.0%} confidence in {parsed.duration_ms} ms",
        user=user, sales_invoice_id=invoice.id, commit=False,
    )

    report, duplicate_reason = _finalize_sales_invoice(
        session, company, user, invoice, data, validator, digest,
        overall_confidence=parsed.overall_confidence,
        ocr_provider=f"{parsed.provider}:{parsed.model}",
    )

    session.commit()
    log.info("Sales invoice %s ingested for company %s with status %s",
              invoice.id, company.id, invoice.status)
    return SalesIngestResult(
        invoice=invoice, parsed=parsed, report=report,
        duplicate_reason=duplicate_reason, warnings=list(parsed.warnings),
    )


def ingest_sales_excel(
    session: Session,
    company: Company,
    user: User,
    filename: str,
    raw_bytes: bytes,
    validator: Optional[InvoiceValidator] = None,
) -> List[SalesIngestResult]:
    """Import sales data from an Excel/CSV sheet — mirrors
    purchase_service.ingest_excel. Every row group (by customer + invoice
    number) goes through the same validation/matching/duplicate pipeline."""
    from services.excel_import import build_invoices, read_table

    validator = validator or InvoiceValidator()
    df = read_table(raw_bytes, filename)
    groups = build_invoices(df)

    results: List[SalesIngestResult] = []
    for group in groups:
        data = group.data
        digest = hashlib.sha256(
            "|".join([
                "excel-sales", str(company.id),
                (data["supplier"]["name"] or "").lower(),
                (data["invoice"]["invoice_number"] or "").lower(),
                data["invoice"]["invoice_date"] or "",
                f"{data['grand_total']:.2f}",
                str(uuid.uuid4()) if not data["invoice"]["invoice_number"] else "",
            ]).encode()
        ).hexdigest()

        invoice = SalesInvoice(
            company_id=company.id,
            status=InvoiceStatus.PROCESSING.value,
            source_file=filename[:255],
            source_pages=1,
            file_hash=digest,
            created_by=user.id,
        )
        session.add(invoice)
        session.flush()

        audit_service.record(
            session, company.id, "SALES_EXCEL_IMPORTED",
            f"Imported {group.row_count} line(s) from '{filename}' "
            f"for {data['supplier']['name'] or 'unknown customer'} / "
            f"{data['invoice']['invoice_number'] or 'no invoice number'}",
            user=user, sales_invoice_id=invoice.id, commit=False,
        )

        report, duplicate_reason = _finalize_sales_invoice(
            session, company, user, invoice, data, validator, digest,
            overall_confidence=1.0,
            ocr_provider="excel:import",
        )

        session.commit()
        results.append(
            SalesIngestResult(
                invoice=invoice, parsed=None, report=report,
                duplicate_reason=duplicate_reason, warnings=list(group.warnings),
            )
        )
    return results


# --------------------------------------------------------------------------
# Review / edit
# --------------------------------------------------------------------------
def sales_invoice_to_dict(invoice: SalesInvoice, items: List[SalesInvoiceItem]) -> dict:
    return {
        "supplier": {
            "name": invoice.customer_name or "",
            "gstin": invoice.customer_gstin or "",
            "address": invoice.customer_address or "",
        },
        "invoice": {
            "invoice_number": invoice.invoice_number or "",
            "invoice_date": invoice.invoice_date or "",
            "due_date": invoice.due_date or "",
        },
        "items": [
            {
                "raw_name": i.raw_name or "", "normalized_name": i.normalized_name or "",
                "hsn": i.hsn or "", "barcode": i.barcode or "",
                "quantity": i.quantity or 0.0, "unit": i.unit or "",
                "rate": i.rate or 0.0, "discount": i.discount or 0.0,
                "taxable_value": i.taxable_value or 0.0, "gst_rate": i.gst_rate or 0.0,
                "cgst": i.cgst or 0.0, "sgst": i.sgst or 0.0, "igst": i.igst or 0.0,
                "cess": i.cess or 0.0, "total": i.total or 0.0,
                "confidence": i.confidence or 0.0, "verified": bool(i.edited_by_user),
            }
            for i in items
        ],
        "subtotal": invoice.subtotal or 0.0,
        "discount_total": invoice.discount_total or 0.0,
        "tax_total": invoice.tax_total or 0.0,
        "cess_total": invoice.cess_total or 0.0,
        "round_off": invoice.round_off or 0.0,
        "grand_total": invoice.grand_total or 0.0,
        "overall_confidence": invoice.overall_confidence or 0.0,
    }


def recalculate_sales_totals(session: Session, invoice: SalesInvoice) -> None:
    items = (
        session.query(SalesInvoiceItem)
        .filter(SalesInvoiceItem.invoice_id == invoice.id).all()
    )
    invoice.subtotal = round(sum(i.taxable_value or 0 for i in items), 2)
    invoice.discount_total = round(sum(i.discount or 0 for i in items), 2)
    invoice.tax_total = round(
        sum((i.cgst or 0) + (i.sgst or 0) + (i.igst or 0) for i in items), 2
    )
    invoice.cess_total = round(sum(i.cess or 0 for i in items), 2)
    if items:
        invoice.overall_confidence = min(
            1.0 if item.edited_by_user else (item.confidence or 0.0)
            for item in items
        )
    session.commit()


def revalidate_sales(
    session: Session, company: Company, invoice: SalesInvoice,
    validator: Optional[InvoiceValidator] = None,
) -> ValidationReport:
    validator = validator or InvoiceValidator()
    items = (
        session.query(SalesInvoiceItem)
        .filter(SalesInvoiceItem.invoice_id == invoice.id)
        .order_by(SalesInvoiceItem.id).all()
    )
    report = validator.validate(
        sales_invoice_to_dict(invoice, items), buyer_gstin=company.gstin or ""
    )
    invoice.tax_scope = report.tax_scope
    invoice.validation_json = json.dumps(report.to_dict())[:60000]
    for index, item in enumerate(items):
        item.line_warnings = json.dumps(report.line_flags.get(index, []))[:4000]
    session.commit()
    return report


def unresolved_sales_items(session: Session, invoice_id: int) -> List[SalesInvoiceItem]:
    return (
        session.query(SalesInvoiceItem)
        .filter(SalesInvoiceItem.invoice_id == invoice_id, SalesInvoiceItem.product_id.is_(None))
        .all()
    )


# --------------------------------------------------------------------------
# Confirmation
# --------------------------------------------------------------------------
def confirm_sales_invoice(
    session: Session, company: Company, user: User, invoice: SalesInvoice,
    override_reason: str = "",
) -> Tuple[bool, str]:
    if not user.can_confirm:
        return False, "Your role does not permit confirming sales entries."
    if invoice.status in (InvoiceStatus.POSTED.value,):
        return False, "This invoice has already been posted."

    report = revalidate_sales(session, company, invoice)
    pending = unresolved_sales_items(session, invoice.id)
    if pending:
        names = ", ".join(i.raw_name or "?" for i in pending[:5])
        return False, (
            f"{len(pending)} line(s) are not linked to a product yet: {names}. "
            "Map or create them before confirming."
        )
    if report.is_blocking and not override_reason:
        return False, (
            f"{len(report.errors)} blocking problem(s) must be resolved or "
            "explicitly overridden with a reason."
        )

    invoice.status = InvoiceStatus.CONFIRMED.value
    invoice.confirmed_by = user.id
    invoice.confirmed_at = dt.datetime.now(dt.timezone.utc)
    session.commit()

    detail = (
        f"Confirmed sales invoice {invoice.invoice_number} to {invoice.customer_name} "
        f"for ₹{invoice.grand_total:,.2f}"
    )
    if override_reason:
        detail += f" | OVERRIDE: {override_reason}"
    audit_service.record(
        session, company.id, "SALES_INVOICE_CONFIRMED", detail,
        user=user, sales_invoice_id=invoice.id,
    )
    return True, "Sales invoice confirmed and ready for Tally."


# --------------------------------------------------------------------------
# Tally payload
# --------------------------------------------------------------------------
def sales_idempotency_key(company_id: int, invoice: SalesInvoice) -> str:
    seed = "|".join([
        "sales", str(company_id),
        (invoice.customer_name or "").strip().lower(),
        (invoice.invoice_number or "").strip().lower(),
        f"{invoice.grand_total or 0:.2f}",
        (invoice.invoice_date or ""),
    ])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]


def build_sales_voucher_payload(
    session: Session, company: Company, invoice: SalesInvoice
) -> VoucherPayload:
    items = (
        session.query(SalesInvoiceItem)
        .filter(SalesInvoiceItem.invoice_id == invoice.id)
        .order_by(SalesInvoiceItem.id).all()
    )
    products = {
        p.id: p
        for p in session.query(Product).filter(Product.company_id == company.id).all()
    }

    customer = (
        session.query(Customer).filter(Customer.id == invoice.customer_id).one_or_none()
        if invoice.customer_id else None
    )
    customer_ledger = (
        (customer.tally_ledger_name if customer and customer.tally_ledger_name else None)
        or invoice.customer_name
        or "Unknown Customer"
    )

    default_godown = get_mapping(session, company.id, "default_godown", "")
    voucher_items = []
    for item in items:
        product = products.get(item.product_id)
        stock_item = (
            (product.tally_stock_item if product and product.tally_stock_item else None)
            or (product.name if product else None)
            or item.normalized_name or item.raw_name
        )
        voucher_items.append({
            "stock_item": stock_item,
            "quantity": item.quantity or 0.0,
            "unit": item.unit or (product.unit if product else "PCS") or "PCS",
            "rate": item.rate or (product.sale_rate if product else 0.0) or 0.0,
            "taxable_value": item.taxable_value or 0.0,
            "hsn": item.hsn or (product.hsn if product else "") or "",
            "godown": (product.godown if product and product.godown else default_godown),
            "discount": item.discount or 0.0,
            "gst_rate": item.gst_rate or 0.0,
            "cgst": item.cgst or 0.0, "sgst": item.sgst or 0.0,
            "igst": item.igst or 0.0, "cess": item.cess or 0.0,
            "total": item.total or 0.0,
        })

    tax_lines = []
    cgst = round(sum(i.cgst or 0 for i in items), 2)
    sgst = round(sum(i.sgst or 0 for i in items), 2)
    igst = round(sum(i.igst or 0 for i in items), 2)
    cess = round(sum(i.cess or 0 for i in items), 2)
    for key, value in (
        ("sales_cgst_ledger", cgst), ("sales_sgst_ledger", sgst),
        ("sales_igst_ledger", igst), ("sales_cess_ledger", cess),
    ):
        if value:
            default_name = key.replace("sales_", "").replace("_ledger", "").upper() + " Output"
            tax_lines.append(
                {"ledger": get_mapping(session, company.id, key, default_name), "amount": value}
            )

    return VoucherPayload(
        company_name=company.tally_company_name or settings.TALLY_COMPANY or company.name,
        voucher_type=get_mapping(session, company.id, "sales_voucher_type", "Sales"),
        supplier_ledger=customer_ledger,
        purchase_ledger=get_mapping(session, company.id, "sales_ledger", "Sales Account"),
        invoice_number=invoice.invoice_number or f"AUTO-{invoice.id}",
        invoice_date=invoice.invoice_date or dt.date.today().isoformat(),
        narration=(
            f"Auto-prepared by {settings.APP_NAME} from sales bill. "
            f"Invoice {invoice.invoice_number} dated {invoice.invoice_date}."
        ),
        godown=default_godown,
        round_off_ledger=get_mapping(session, company.id, "round_off_ledger", "Round Off"),
        round_off_amount=invoice.round_off or 0.0,
        tax_lines=tax_lines,
        items=voucher_items,
        grand_total=invoice.grand_total or 0.0,
        reference_id=sales_idempotency_key(company.id, invoice),
        transaction_kind="sales",
    )


# --------------------------------------------------------------------------
# Posting + stock
# --------------------------------------------------------------------------
def apply_sales_stock(
    session: Session, company_id: int, invoice: SalesInvoice, transaction: SalesTransaction
) -> int:
    """Decrease stock once per transaction — the mirror of apply_stock."""
    if transaction.stock_applied:
        return 0

    items = (
        session.query(SalesInvoiceItem).filter(SalesInvoiceItem.invoice_id == invoice.id).all()
    )
    applied = 0
    for item in items:
        if not item.product_id or not item.quantity:
            continue
        product = (
            session.query(Product)
            .filter(Product.company_id == company_id, Product.id == item.product_id)
            .one_or_none()
        )
        if product is None:
            continue
        before = product.current_stock or 0.0
        after = round(before - float(item.quantity), 4)
        product.current_stock = after
        if item.rate:
            product.sale_rate = item.rate
        session.add(
            StockMovement(
                company_id=company_id,
                product_id=product.id,
                sales_transaction_id=transaction.id,
                sales_invoice_id=invoice.id,
                quantity_before=before,
                quantity_change=-float(item.quantity),
                quantity_after=after,
                reason=f"Sale {invoice.invoice_number or invoice.id}",
            )
        )
        applied += 1
    transaction.stock_applied = True
    session.commit()
    log.info("Stock decreased for sales invoice %s (%s lines)", invoice.id, applied)
    return applied


def post_sales_to_tally(
    session: Session,
    company: Company,
    user: User,
    invoice: SalesInvoice,
    connector: TallyConnector,
    run_preflight: bool = True,
) -> Tuple[TallyResult, Optional[SalesTransaction]]:
    """Post (or export) a confirmed sales invoice, then move stock — once."""
    if not user.can_post_to_tally:
        return TallyResult(False, "Your role does not permit posting to Tally."), None
    if invoice.status not in (
        InvoiceStatus.CONFIRMED.value, InvoiceStatus.READY.value,
        InvoiceStatus.EXPORTED.value, InvoiceStatus.FAILED.value,
    ):
        return (
            TallyResult(False, f"Invoice is '{invoice.status}'. Only a confirmed invoice can be posted."),
            None,
        )

    key = sales_idempotency_key(company.id, invoice)
    existing = (
        session.query(SalesTransaction)
        .filter(SalesTransaction.company_id == company.id, SalesTransaction.idempotency_key == key)
        .one_or_none()
    )
    if existing and existing.stock_applied:
        return (
            TallyResult(
                False,
                "This invoice has already been posted and its stock applied. "
                "Duplicate posting was blocked.",
                mode=existing.mode or "", voucher_id=existing.tally_voucher_id or "",
            ),
            existing,
        )

    payload = build_sales_voucher_payload(session, company, invoice)

    if run_preflight:
        problems = connector.preflight(payload)
        if problems:
            invoice.status = InvoiceStatus.FAILED.value
            session.commit()
            audit_service.record(
                session, company.id, "SALES_TALLY_PREFLIGHT_FAILED",
                "; ".join(problems)[:2000], user=user, sales_invoice_id=invoice.id,
            )
            return (
                TallyResult(
                    False,
                    "Tally pre-check failed — nothing was posted:\n- " + "\n- ".join(problems),
                ),
                None,
            )

    audit_service.record(
        session, company.id, "SALES_TALLY_POST_ATTEMPTED",
        f"Voucher for sales invoice {invoice.invoice_number} via {connector.name} "
        f"(test_mode={connector.test_mode})",
        user=user, sales_invoice_id=invoice.id,
    )

    try:
        result = connector.post_voucher(payload)
    except Exception as exc:
        invoice.status = InvoiceStatus.FAILED.value
        session.commit()
        audit_service.record_error(
            session, company.id, "tally", f"Posting raised: {exc}", exc,
            sales_invoice_id=invoice.id,
        )
        return TallyResult(False, f"Tally posting failed: {exc}"), None

    if not result.success:
        invoice.status = InvoiceStatus.FAILED.value
        session.commit()
        audit_service.record(
            session, company.id, "SALES_TALLY_POST_FAILED", result.message[:2000],
            user=user, sales_invoice_id=invoice.id,
        )
        return result, None

    transaction = existing or SalesTransaction(
        company_id=company.id, invoice_id=invoice.id, idempotency_key=key, created_by=user.id,
    )
    transaction.mode = result.mode
    transaction.tally_voucher_id = result.voucher_id or ""
    transaction.request_xml = (result.request_xml or "")[:200000]
    transaction.tally_response = (result.response_xml or result.message)[:20000]
    transaction.export_path = result.export_path or ""
    transaction.amount = invoice.grand_total or 0.0
    session.add(transaction)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        transaction = (
            session.query(SalesTransaction)
            .filter(SalesTransaction.company_id == company.id, SalesTransaction.idempotency_key == key)
            .one()
        )
        if transaction.stock_applied:
            return (
                TallyResult(
                    False,
                    "A concurrent posting for this invoice already completed. "
                    "Stock was not applied twice.",
                ),
                transaction,
            )

    if result.mode == "XML_EXPORT":
        invoice.status = InvoiceStatus.EXPORTED.value
        session.commit()
        audit_service.record(
            session, company.id, "SALES_TALLY_XML_EXPORTED",
            f"{result.message} | file={result.export_path} | "
            "stock NOT applied yet — awaiting confirmation of the Tally import",
            user=user, sales_invoice_id=invoice.id,
        )
        result.warnings.append(
            "Stock has NOT been updated yet. Import this file in Tally, then press "
            "'I have imported this in Tally' to update stock."
        )
        return result, transaction

    lines = apply_sales_stock(session, company.id, invoice, transaction)

    invoice.status = InvoiceStatus.POSTED.value
    invoice.posted_at = dt.datetime.now(dt.timezone.utc)
    session.commit()

    audit_service.record(
        session, company.id, "SALES_TALLY_POST_SUCCESS",
        f"{result.message} | mode={result.mode} | stock decreased on {lines} product(s)",
        user=user, sales_invoice_id=invoice.id,
    )
    result.warnings.append(f"Stock decreased on {lines} product(s).")
    return result, transaction


def confirm_sales_tally_import(
    session: Session, company: Company, user: User, invoice: SalesInvoice,
) -> Tuple[bool, str]:
    if not user.can_post_to_tally:
        return False, "Your role does not permit updating stock from Tally imports."
    if invoice.status != InvoiceStatus.EXPORTED.value:
        return False, (
            f"Invoice is '{invoice.status}'. Only an exported invoice awaiting "
            "manual import can be confirmed this way."
        )

    key = sales_idempotency_key(company.id, invoice)
    transaction = (
        session.query(SalesTransaction)
        .filter(SalesTransaction.company_id == company.id, SalesTransaction.idempotency_key == key)
        .one_or_none()
    )
    if transaction is None:
        return False, "No export record found for this invoice. Generate the XML again."
    if transaction.stock_applied:
        return False, "Stock for this invoice has already been applied."

    lines = apply_sales_stock(session, company.id, invoice, transaction)
    invoice.status = InvoiceStatus.POSTED.value
    invoice.posted_at = dt.datetime.now(dt.timezone.utc)
    session.commit()

    audit_service.record(
        session, company.id, "SALES_TALLY_IMPORT_CONFIRMED",
        f"User confirmed the XML was imported into Tally; "
        f"stock decreased on {lines} product(s)",
        user=user, sales_invoice_id=invoice.id,
    )
    return True, f"Stock updated on {lines} product(s)."


def cancel_sales_invoice(
    session: Session, company: Company, user: User, invoice: SalesInvoice, reason: str
) -> Tuple[bool, str]:
    if invoice.status == InvoiceStatus.POSTED.value:
        return False, "A posted invoice cannot be cancelled here. Reverse it in Tally."
    invoice.status = InvoiceStatus.CANCELLED.value
    invoice.notes = (invoice.notes or "") + f"\nCancelled: {reason}"
    session.commit()
    audit_service.record(
        session, company.id, "SALES_INVOICE_CANCELLED", reason,
        user=user, sales_invoice_id=invoice.id,
    )
    return True, "Sales invoice cancelled."


# --------------------------------------------------------------------------
# Dashboard metrics
# --------------------------------------------------------------------------
def sales_dashboard_metrics(session: Session, company_id: int) -> Dict[str, float]:
    invoices = session.query(SalesInvoice).filter(SalesInvoice.company_id == company_id).all()
    posted = [i for i in invoices if i.status == InvoiceStatus.POSTED.value]
    return {
        "processed": len(invoices),
        "pending": sum(
            1 for i in invoices
            if i.status in (InvoiceStatus.REVIEW_REQUIRED.value, InvoiceStatus.PROCESSING.value)
        ),
        "confirmed": sum(1 for i in invoices if i.status == InvoiceStatus.CONFIRMED.value),
        "exported": sum(1 for i in invoices if i.status == InvoiceStatus.EXPORTED.value),
        "posted": len(posted),
        "duplicates": sum(1 for i in invoices if i.status == InvoiceStatus.DUPLICATE.value),
        "failed": sum(1 for i in invoices if i.status == InvoiceStatus.FAILED.value),
        "sales_value": round(sum(i.grand_total or 0 for i in posted), 2),
        "pending_value": round(
            sum(
                i.grand_total or 0 for i in invoices
                if i.status in (InvoiceStatus.REVIEW_REQUIRED.value, InvoiceStatus.CONFIRMED.value)
            ), 2,
        ),
    }
