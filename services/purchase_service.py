"""
Purchase orchestration.

Owns the state machine and is the only place that is allowed to move stock:

  EXTRACTED -> REVIEW REQUIRED -> CONFIRMED -> READY -> POSTED
                                     |
                                     +-> DUPLICATE / CANCELLED / FAILED

Stock is applied exactly once, guarded by a unique idempotency key on
purchase_transactions.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PIL import Image
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.database import get_mapping
from database.models import (
    Company,
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    MatchStatus,
    OCRResult,
    Product,
    PurchaseTransaction,
    StockMovement,
    Supplier,
    User,
)
from services import audit_service
from services.ai_invoice_parser import InvoiceParser, ParsedInvoice
from services.invoice_validator import (
    InvoiceValidator,
    ValidationReport,
    check_duplicate,
)
from services.product_matcher import ProductMatcher
from services.tally_connector import TallyConnector, TallyResult, VoucherPayload
from utils.config import settings
from utils.image_tools import file_hash, save_image
from utils.logging import get_logger

log = get_logger("purchase")


class PurchaseError(RuntimeError):
    pass


@dataclass
class IngestResult:
    invoice: Invoice
    parsed: ParsedInvoice
    report: ValidationReport
    duplicate_reason: str = ""
    warnings: List[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
def find_or_create_supplier(
    session: Session, company_id: int, name: str, gstin: str = "", address: str = ""
) -> Optional[Supplier]:
    name = (name or "").strip()
    if not name:
        return None
    query = session.query(Supplier).filter(Supplier.company_id == company_id)
    if gstin:
        found = query.filter(Supplier.gstin == gstin).first()
        if found:
            return found
    found = query.filter(Supplier.name.ilike(name)).first()
    if found:
        if gstin and not found.gstin:
            found.gstin = gstin
        return found
    supplier = Supplier(
        company_id=company_id,
        name=name[:200],
        gstin=gstin or None,
        address=address or None,
        state_code=(gstin or "")[:2] or None,
    )
    session.add(supplier)
    session.flush()
    return supplier


def _finalize_invoice(
    session: Session,
    company: Company,
    user: User,
    invoice: Invoice,
    data: dict,
    validator: InvoiceValidator,
    digest: str,
    overall_confidence: float,
    ocr_provider: str,
) -> Tuple[ValidationReport, str]:
    """Shared tail of the ingest pipeline.

    Everything from here on (supplier resolution, validation, product
    matching, duplicate detection) works off a normalized invoice `data`
    dict — the same shape `ai_invoice_parser.normalize_invoice` produces —
    regardless of whether that dict came from OCR on a photo or from an
    Excel/CSV import. Keeping this in one place means every ingestion route
    gets identical GST/arithmetic checks, identical product matching, and
    identical duplicate protection.
    """
    supplier_info = data.get("supplier") or {}
    invoice_info = data.get("invoice") or {}
    supplier = find_or_create_supplier(
        session, company.id,
        supplier_info.get("name", ""),
        supplier_info.get("gstin", ""),
        supplier_info.get("address", ""),
    )

    invoice.supplier_id = supplier.id if supplier else None
    invoice.supplier_name = supplier_info.get("name", "")[:200]
    invoice.supplier_gstin = supplier_info.get("gstin", "")[:20]
    invoice.supplier_address = supplier_info.get("address", "")
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
    report = validator.validate(data, buyer_gstin=company.gstin or "")
    invoice.tax_scope = report.tax_scope
    invoice.validation_json = json.dumps(report.to_dict())[:60000]

    # -- product matching ------------------------------------------------
    matcher = ProductMatcher(session, company.id)
    for index, item in enumerate(data.get("items", [])):
        match = matcher.match(item, supplier.id if supplier else None)
        session.add(
            InvoiceItem(
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
    dupe = check_duplicate(
        session, company.id, invoice.supplier_name, invoice.invoice_number,
        invoice.invoice_date, invoice.grand_total, digest, exclude_invoice_id=invoice.id,
    )
    duplicate_reason = ""
    if dupe.is_duplicate:
        invoice.duplicate_of_id = dupe.invoice_id
        invoice.status = InvoiceStatus.DUPLICATE.value
        duplicate_reason = dupe.reason
        audit_service.record(
            session, company.id, "DUPLICATE_DETECTED", dupe.reason,
            user=user, invoice_id=invoice.id, commit=False,
        )
    else:
        invoice.status = InvoiceStatus.REVIEW_REQUIRED.value

    return report, duplicate_reason


def ingest_bill(
    session: Session,
    company: Company,
    user: User,
    filename: str,
    raw_bytes: bytes,
    images: List[Image.Image],
    parser: InvoiceParser,
    validator: Optional[InvoiceValidator] = None,
    store_image: bool = True,
) -> IngestResult:
    """Run the full extract → match → validate pipeline and persist the invoice."""
    validator = validator or InvoiceValidator()
    digest = file_hash(raw_bytes)

    invoice = Invoice(
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
        session, company.id, "BILL_UPLOADED",
        f"Uploaded '{filename}' ({len(images)} page(s), {len(raw_bytes)/1024:.0f} KB)",
        user=user, invoice_id=invoice.id, commit=False,
    )

    if store_image and images:
        try:
            folder = settings.UPLOAD_DIR / str(company.id) / str(invoice.id)
            for index, img in enumerate(images, start=1):
                save_image(img, folder / f"page_{index}.jpg")
            invoice.source_file = f"{folder}/page_1.jpg"
        except Exception as exc:  # storage failure must not kill the pipeline
            audit_service.record_error(
                session, company.id, "storage", f"Could not store page image: {exc}",
                exc, invoice.id, commit=False,
            )

    # -- extraction -----------------------------------------------------
    parsed = parser.parse_images(images)
    data = parsed.data

    session.add(
        OCRResult(
            company_id=company.id,
            invoice_id=invoice.id,
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
        session, company.id, "AI_EXTRACTED",
        f"{parsed.provider}/{parsed.model} extracted {len(data.get('items', []))} "
        f"line(s) at {parsed.overall_confidence:.0%} confidence in {parsed.duration_ms} ms",
        user=user, invoice_id=invoice.id, commit=False,
    )

    report, duplicate_reason = _finalize_invoice(
        session, company, user, invoice, data, validator, digest,
        overall_confidence=parsed.overall_confidence,
        ocr_provider=f"{parsed.provider}:{parsed.model}",
    )

    session.commit()
    log.info(
        "Invoice %s ingested for company %s with status %s",
        invoice.id, company.id, invoice.status,
    )
    return IngestResult(
        invoice=invoice,
        parsed=parsed,
        report=report,
        duplicate_reason=duplicate_reason,
        warnings=list(parsed.warnings),
    )


def ingest_excel(
    session: Session,
    company: Company,
    user: User,
    filename: str,
    raw_bytes: bytes,
    validator: Optional[InvoiceValidator] = None,
) -> List[IngestResult]:
    """Import purchase data from an Excel/CSV sheet instead of a bill photo.

    Every row group (one per supplier + invoice number found in the sheet)
    goes through the exact same validation, product matching and duplicate
    checks as a photographed bill — see `_finalize_invoice`. Nothing is
    posted to Tally here; each imported bill lands in Review Bills like any
    other, so the user checks it before Confirm / Post to Tally.
    """
    from services.excel_import import build_invoices, read_table  # local import avoids a hard pandas dependency for callers that never use this path

    validator = validator or InvoiceValidator()
    df = read_table(raw_bytes, filename)
    groups = build_invoices(df)

    results: List[IngestResult] = []
    for group in groups:
        data = group.data
        digest = hashlib.sha256(
            "|".join([
                "excel", str(company.id),
                (data["supplier"]["name"] or "").lower(),
                (data["invoice"]["invoice_number"] or "").lower(),
                data["invoice"]["invoice_date"] or "",
                f"{data['grand_total']:.2f}",
                str(uuid.uuid4()) if not data["invoice"]["invoice_number"] else "",
            ]).encode()
        ).hexdigest()
        # The uuid fallback above only kicks in when there is no invoice
        # number at all, so two genuinely identical re-uploads of a sheet
        # that *does* carry invoice numbers still hash the same and get
        # caught by the duplicate check below.

        invoice = Invoice(
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
            session, company.id, "EXCEL_IMPORTED",
            f"Imported {group.row_count} line(s) from '{filename}' "
            f"for {data['supplier']['name'] or 'unknown supplier'} / "
            f"{data['invoice']['invoice_number'] or 'no invoice number'}",
            user=user, invoice_id=invoice.id, commit=False,
        )

        report, duplicate_reason = _finalize_invoice(
            session, company, user, invoice, data, validator, digest,
            overall_confidence=1.0,
            ocr_provider="excel:import",
        )

        session.commit()
        log.info(
            "Invoice %s imported from Excel for company %s with status %s",
            invoice.id, company.id, invoice.status,
        )
        results.append(
            IngestResult(
                invoice=invoice,
                parsed=None,
                report=report,
                duplicate_reason=duplicate_reason,
                warnings=list(group.warnings),
            )
        )
    return results


# --------------------------------------------------------------------------
# Editing / recalculation
# --------------------------------------------------------------------------
def invoice_to_dict(invoice: Invoice, items: List[InvoiceItem]) -> dict:
    return {
        "supplier": {
            "name": invoice.supplier_name or "",
            "gstin": invoice.supplier_gstin or "",
            "address": invoice.supplier_address or "",
        },
        "invoice": {
            "invoice_number": invoice.invoice_number or "",
            "invoice_date": invoice.invoice_date or "",
            "due_date": invoice.due_date or "",
        },
        "items": [
            {
                "raw_name": i.raw_name or "",
                "normalized_name": i.normalized_name or "",
                "hsn": i.hsn or "",
                "barcode": i.barcode or "",
                "quantity": i.quantity or 0.0,
                "unit": i.unit or "",
                "rate": i.rate or 0.0,
                "discount": i.discount or 0.0,
                "taxable_value": i.taxable_value or 0.0,
                "gst_rate": i.gst_rate or 0.0,
                "cgst": i.cgst or 0.0,
                "sgst": i.sgst or 0.0,
                "igst": i.igst or 0.0,
                "cess": i.cess or 0.0,
                "total": i.total or 0.0,
                "confidence": i.confidence or 0.0,
                "verified": bool(i.edited_by_user),
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


def recalculate_totals(session: Session, invoice: Invoice) -> None:
    """Recompute header totals from the (possibly edited) line items."""
    items = (
        session.query(InvoiceItem).filter(InvoiceItem.invoice_id == invoice.id).all()
    )
    invoice.subtotal = round(sum(i.taxable_value or 0 for i in items), 2)
    invoice.discount_total = round(sum(i.discount or 0 for i in items), 2)
    invoice.tax_total = round(
        sum((i.cgst or 0) + (i.sgst or 0) + (i.igst or 0) for i in items), 2
    )
    invoice.cess_total = round(sum(i.cess or 0 for i in items), 2)

    # The invoice-level confidence is only as good as its weakest line, and a
    # line a person has opened and saved is no longer in doubt. Recomputing it
    # this way means correcting the one bad line clears the invoice block.
    #
    # The alternative — demanding every line be re-saved before the block
    # lifts — is busywork on the lines that were read perfectly, and it teaches
    # people to hit Save without reading. That is the opposite of what this
    # review screen is for. The original AI figure is kept on the OCRResult
    # row and in the audit trail.
    if items:
        invoice.overall_confidence = min(
            1.0 if item.edited_by_user else (item.confidence or 0.0)
            for item in items
        )
    session.commit()


def revalidate(
    session: Session, company: Company, invoice: Invoice,
    validator: Optional[InvoiceValidator] = None,
) -> ValidationReport:
    validator = validator or InvoiceValidator()
    items = (
        session.query(InvoiceItem)
        .filter(InvoiceItem.invoice_id == invoice.id)
        .order_by(InvoiceItem.id)
        .all()
    )
    report = validator.validate(
        invoice_to_dict(invoice, items), buyer_gstin=company.gstin or ""
    )
    invoice.tax_scope = report.tax_scope
    invoice.validation_json = json.dumps(report.to_dict())[:60000]
    for index, item in enumerate(items):
        item.line_warnings = json.dumps(report.line_flags.get(index, []))[:4000]
    session.commit()
    return report


def unresolved_items(session: Session, invoice_id: int) -> List[InvoiceItem]:
    """Lines that still have no product linked."""
    return (
        session.query(InvoiceItem)
        .filter(InvoiceItem.invoice_id == invoice_id, InvoiceItem.product_id.is_(None))
        .all()
    )


# --------------------------------------------------------------------------
# Confirmation
# --------------------------------------------------------------------------
def confirm_invoice(
    session: Session, company: Company, user: User, invoice: Invoice,
    override_reason: str = "",
) -> Tuple[bool, str]:
    """Move an invoice to CONFIRMED. Blocking problems stop this."""
    if not user.can_confirm:
        return False, "Your role does not permit confirming purchase entries."
    if invoice.status in (InvoiceStatus.POSTED.value,):
        return False, "This invoice has already been posted."

    report = revalidate(session, company, invoice)
    pending = unresolved_items(session, invoice.id)
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
        f"Confirmed invoice {invoice.invoice_number} from {invoice.supplier_name} "
        f"for ₹{invoice.grand_total:,.2f}"
    )
    if override_reason:
        detail += f" | OVERRIDE: {override_reason}"
    audit_service.record(
        session, company.id, "INVOICE_CONFIRMED", detail, user=user, invoice_id=invoice.id
    )
    return True, "Invoice confirmed and ready for Tally."


# --------------------------------------------------------------------------
# Tally payload
# --------------------------------------------------------------------------
def idempotency_key(company_id: int, invoice: Invoice) -> str:
    seed = "|".join(
        [
            str(company_id),
            (invoice.supplier_name or "").strip().lower(),
            (invoice.invoice_number or "").strip().lower(),
            f"{invoice.grand_total or 0:.2f}",
            (invoice.invoice_date or ""),
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]


def build_voucher_payload(
    session: Session, company: Company, invoice: Invoice
) -> VoucherPayload:
    items = (
        session.query(InvoiceItem)
        .filter(InvoiceItem.invoice_id == invoice.id)
        .order_by(InvoiceItem.id)
        .all()
    )
    products = {
        p.id: p
        for p in session.query(Product).filter(Product.company_id == company.id).all()
    }

    supplier = (
        session.query(Supplier).filter(Supplier.id == invoice.supplier_id).one_or_none()
        if invoice.supplier_id
        else None
    )
    supplier_ledger = (
        (supplier.tally_ledger_name if supplier and supplier.tally_ledger_name else None)
        or invoice.supplier_name
        or "Unknown Supplier"
    )

    default_godown = get_mapping(session, company.id, "default_godown", "")
    voucher_items = []
    for item in items:
        product = products.get(item.product_id)
        stock_item = (
            (product.tally_stock_item if product and product.tally_stock_item else None)
            or (product.name if product else None)
            or item.normalized_name
            or item.raw_name
        )
        voucher_items.append(
            {
                "stock_item": stock_item,
                "quantity": item.quantity or 0.0,
                "unit": item.unit or (product.unit if product else "PCS") or "PCS",
                "rate": item.rate or 0.0,
                "taxable_value": item.taxable_value or 0.0,
                "hsn": item.hsn or (product.hsn if product else "") or "",
                "godown": (product.godown if product and product.godown else default_godown),
                # Per-line tax and discount. Tally takes tax as voucher-level
                # ledger entries and ignores these, but an Excel sheet has to
                # show the tax against the line it belongs to — which is how
                # the supplier printed it in the first place.
                "discount": item.discount or 0.0,
                "gst_rate": item.gst_rate or 0.0,
                "cgst": item.cgst or 0.0,
                "sgst": item.sgst or 0.0,
                "igst": item.igst or 0.0,
                "cess": item.cess or 0.0,
                "total": item.total or 0.0,
            }
        )

    tax_lines = []
    cgst = round(sum(i.cgst or 0 for i in items), 2)
    sgst = round(sum(i.sgst or 0 for i in items), 2)
    igst = round(sum(i.igst or 0 for i in items), 2)
    cess = round(sum(i.cess or 0 for i in items), 2)
    for key, value in (
        ("cgst_ledger", cgst), ("sgst_ledger", sgst),
        ("igst_ledger", igst), ("cess_ledger", cess),
    ):
        if value:
            tax_lines.append(
                {"ledger": get_mapping(session, company.id, key, key), "amount": value}
            )

    return VoucherPayload(
        company_name=company.tally_company_name or settings.TALLY_COMPANY or company.name,
        voucher_type=get_mapping(session, company.id, "voucher_type", "Purchase"),
        supplier_ledger=supplier_ledger,
        purchase_ledger=get_mapping(session, company.id, "purchase_ledger", "Purchase Account"),
        invoice_number=invoice.invoice_number or f"AUTO-{invoice.id}",
        invoice_date=invoice.invoice_date or dt.date.today().isoformat(),
        narration=(
            f"Auto-prepared by {settings.APP_NAME} from bill image. "
            f"Invoice {invoice.invoice_number} dated {invoice.invoice_date}."
        ),
        godown=default_godown,
        round_off_ledger=get_mapping(session, company.id, "round_off_ledger", "Round Off"),
        round_off_amount=invoice.round_off or 0.0,
        tax_lines=tax_lines,
        items=voucher_items,
        grand_total=invoice.grand_total or 0.0,
        reference_id=idempotency_key(company.id, invoice),
    )


# --------------------------------------------------------------------------
# Posting + stock
# --------------------------------------------------------------------------
def apply_stock(
    session: Session, company_id: int, invoice: Invoice, transaction: PurchaseTransaction
) -> int:
    """Increase stock once per transaction. Returns the number of lines applied."""
    if transaction.stock_applied:
        log.info("Stock already applied for transaction %s", transaction.id)
        return 0

    items = (
        session.query(InvoiceItem).filter(InvoiceItem.invoice_id == invoice.id).all()
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
        after = round(before + float(item.quantity), 4)
        product.current_stock = after
        if item.rate:
            product.purchase_rate = item.rate
        session.add(
            StockMovement(
                company_id=company_id,
                product_id=product.id,
                transaction_id=transaction.id,
                invoice_id=invoice.id,
                quantity_before=before,
                quantity_change=float(item.quantity),
                quantity_after=after,
                reason=f"Purchase {invoice.invoice_number or invoice.id}",
            )
        )
        applied += 1
    transaction.stock_applied = True
    session.commit()
    log.info("Stock applied for invoice %s (%s lines)", invoice.id, applied)
    return applied


def post_to_tally(
    session: Session,
    company: Company,
    user: User,
    invoice: Invoice,
    connector: TallyConnector,
    run_preflight: bool = True,
) -> Tuple[TallyResult, Optional[PurchaseTransaction]]:
    """
    Post (or export) a confirmed invoice, then move stock — once.

    Stock is only touched after the connector reports success.
    """
    if not user.can_post_to_tally:
        return TallyResult(False, "Your role does not permit posting to Tally."), None
    if invoice.status not in (
        InvoiceStatus.CONFIRMED.value,
        InvoiceStatus.READY.value,
        InvoiceStatus.EXPORTED.value,
        InvoiceStatus.FAILED.value,
    ):
        return (
            TallyResult(
                False,
                f"Invoice is '{invoice.status}'. Only a confirmed invoice can be posted.",
            ),
            None,
        )

    key = idempotency_key(company.id, invoice)
    existing = (
        session.query(PurchaseTransaction)
        .filter(
            PurchaseTransaction.company_id == company.id,
            PurchaseTransaction.idempotency_key == key,
        )
        .one_or_none()
    )
    if existing and existing.stock_applied:
        return (
            TallyResult(
                False,
                "This invoice has already been posted and its stock applied. "
                "Duplicate posting was blocked.",
                mode=existing.mode or "",
                voucher_id=existing.tally_voucher_id or "",
            ),
            existing,
        )

    payload = build_voucher_payload(session, company, invoice)

    if run_preflight:
        problems = connector.preflight(payload)
        if problems:
            invoice.status = InvoiceStatus.FAILED.value
            session.commit()
            audit_service.record(
                session, company.id, "TALLY_PREFLIGHT_FAILED",
                "; ".join(problems)[:2000], user=user, invoice_id=invoice.id,
            )
            return (
                TallyResult(
                    False,
                    "Tally pre-check failed — nothing was posted:\n- "
                    + "\n- ".join(problems),
                    request_xml="",
                ),
                None,
            )

    audit_service.record(
        session, company.id, "TALLY_POST_ATTEMPTED",
        f"Voucher for invoice {invoice.invoice_number} via {connector.name} "
        f"(test_mode={connector.test_mode})",
        user=user, invoice_id=invoice.id,
    )

    try:
        result = connector.post_voucher(payload)
    except Exception as exc:
        invoice.status = InvoiceStatus.FAILED.value
        session.commit()
        audit_service.record_error(
            session, company.id, "tally", f"Posting raised: {exc}", exc, invoice.id
        )
        return TallyResult(False, f"Tally posting failed: {exc}"), None

    if not result.success:
        invoice.status = InvoiceStatus.FAILED.value
        session.commit()
        audit_service.record(
            session, company.id, "TALLY_POST_FAILED", result.message[:2000],
            user=user, invoice_id=invoice.id,
        )
        return result, None

    transaction = existing or PurchaseTransaction(
        company_id=company.id,
        invoice_id=invoice.id,
        idempotency_key=key,
        created_by=user.id,
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
            session.query(PurchaseTransaction)
            .filter(
                PurchaseTransaction.company_id == company.id,
                PurchaseTransaction.idempotency_key == key,
            )
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

    # Writing an XML file is not the same as Tally accepting it. In export
    # mode the file may never be imported, or the import may fail — so the
    # bill waits in EXPORTED and stock stays where it is until a person
    # confirms the import. Anything else lets our stock and Tally's diverge
    # silently, which is exactly what this software exists to prevent.
    if result.mode == "XML_EXPORT":
        invoice.status = InvoiceStatus.EXPORTED.value
        session.commit()
        audit_service.record(
            session, company.id, "TALLY_XML_EXPORTED",
            f"{result.message} | file={result.export_path} | "
            "stock NOT applied yet — awaiting confirmation of the Tally import",
            user=user, invoice_id=invoice.id,
        )
        result.warnings.append(
            "Stock has NOT been updated yet. Import this file in Tally, then press "
            "'I have imported this in Tally' to update stock."
        )
        return result, transaction

    lines = apply_stock(session, company.id, invoice, transaction)

    invoice.status = InvoiceStatus.POSTED.value
    invoice.posted_at = dt.datetime.now(dt.timezone.utc)
    session.commit()

    audit_service.record(
        session, company.id, "TALLY_POST_SUCCESS",
        f"{result.message} | mode={result.mode} | stock updated on {lines} product(s)",
        user=user, invoice_id=invoice.id,
    )
    result.warnings.append(f"Stock increased on {lines} product(s).")
    return result, transaction


def confirm_tally_import(
    session: Session,
    company: Company,
    user: User,
    invoice: Invoice,
) -> Tuple[bool, str]:
    """The user has imported the exported XML into Tally themselves.

    This is the moment the voucher actually exists in their books, so it is
    the moment stock moves. Guarded by the same idempotency record as a live
    post, so confirming twice cannot double the stock.
    """
    if not user.can_post_to_tally:
        return False, "Your role does not permit updating stock from Tally imports."
    if invoice.status != InvoiceStatus.EXPORTED.value:
        return False, (
            f"Invoice is '{invoice.status}'. Only an exported invoice awaiting "
            "manual import can be confirmed this way."
        )

    key = idempotency_key(company.id, invoice)
    transaction = (
        session.query(PurchaseTransaction)
        .filter(
            PurchaseTransaction.company_id == company.id,
            PurchaseTransaction.idempotency_key == key,
        )
        .one_or_none()
    )
    if transaction is None:
        return False, "No export record found for this invoice. Generate the XML again."
    if transaction.stock_applied:
        return False, "Stock for this invoice has already been applied."

    lines = apply_stock(session, company.id, invoice, transaction)
    invoice.status = InvoiceStatus.POSTED.value
    invoice.posted_at = dt.datetime.now(dt.timezone.utc)
    session.commit()

    audit_service.record(
        session, company.id, "TALLY_IMPORT_CONFIRMED",
        f"User confirmed the XML was imported into Tally; "
        f"stock updated on {lines} product(s)",
        user=user, invoice_id=invoice.id,
    )
    return True, f"Stock updated on {lines} product(s)."


def cancel_invoice(
    session: Session, company: Company, user: User, invoice: Invoice, reason: str
) -> Tuple[bool, str]:
    if invoice.status == InvoiceStatus.POSTED.value:
        return False, "A posted invoice cannot be cancelled here. Reverse it in Tally."
    invoice.status = InvoiceStatus.CANCELLED.value
    invoice.notes = (invoice.notes or "") + f"\nCancelled: {reason}"
    session.commit()
    audit_service.record(
        session, company.id, "INVOICE_CANCELLED", reason, user=user, invoice_id=invoice.id
    )
    return True, "Invoice cancelled."


def create_manual_invoice(
    session: Session, company: Company, user: User, header: dict, lines: List[dict]
) -> Invoice:
    """Fallback path when AI cannot read a bill at all."""
    supplier = find_or_create_supplier(
        session, company.id, header.get("supplier_name", ""), header.get("gstin", "")
    )
    invoice = Invoice(
        company_id=company.id,
        supplier_id=supplier.id if supplier else None,
        supplier_name=(header.get("supplier_name") or "")[:200],
        supplier_gstin=(header.get("gstin") or "")[:20],
        invoice_number=(header.get("invoice_number") or "")[:80],
        invoice_date=header.get("invoice_date") or "",
        grand_total=float(header.get("grand_total") or 0),
        round_off=float(header.get("round_off") or 0),
        overall_confidence=1.0,          # a human typed it
        status=InvoiceStatus.REVIEW_REQUIRED.value,
        source_file="manual entry",
        ocr_provider="manual",
        file_hash=hashlib.sha256(
            f"manual|{company.id}|{header.get('invoice_number')}|{uuid.uuid4()}".encode()
        ).hexdigest(),
        created_by=user.id,
    )
    session.add(invoice)
    session.flush()

    for line in lines:
        session.add(
            InvoiceItem(
                invoice_id=invoice.id,
                company_id=company.id,
                product_id=line.get("product_id"),
                raw_name=(line.get("name") or "")[:250],
                normalized_name=(line.get("name") or "")[:250],
                hsn=(line.get("hsn") or "")[:20],
                quantity=float(line.get("quantity") or 0),
                unit=(line.get("unit") or "PCS")[:20],
                rate=float(line.get("rate") or 0),
                discount=float(line.get("discount") or 0),
                taxable_value=round(
                    float(line.get("quantity") or 0) * float(line.get("rate") or 0)
                    - float(line.get("discount") or 0),
                    2,
                ),
                gst_rate=float(line.get("gst_rate") or 0),
                cgst=float(line.get("cgst") or 0),
                sgst=float(line.get("sgst") or 0),
                igst=float(line.get("igst") or 0),
                cess=float(line.get("cess") or 0),
                total=float(line.get("total") or 0),
                confidence=1.0,
                match_status=(
                    MatchStatus.MATCHED.value
                    if line.get("product_id")
                    else MatchStatus.NEW.value
                ),
                match_score=1.0 if line.get("product_id") else 0.0,
                match_reason="manual entry",
            )
        )
    recalculate_totals(session, invoice)
    audit_service.record(
        session, company.id, "MANUAL_INVOICE_CREATED",
        f"Manual entry for {invoice.supplier_name} / {invoice.invoice_number}",
        user=user, invoice_id=invoice.id,
    )
    return invoice


# --------------------------------------------------------------------------
# Dashboard metrics
# --------------------------------------------------------------------------
def dashboard_metrics(session: Session, company_id: int) -> Dict[str, float]:
    invoices = session.query(Invoice).filter(Invoice.company_id == company_id).all()
    posted = [i for i in invoices if i.status == InvoiceStatus.POSTED.value]
    new_products = (
        session.query(InvoiceItem)
        .filter(
            InvoiceItem.company_id == company_id,
            InvoiceItem.match_status.in_(
                [MatchStatus.NEW.value, MatchStatus.NEW_BARCODE.value]
            ),
            InvoiceItem.product_id.is_(None),
        )
        .count()
    )
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
        "low_confidence": sum(
            1 for i in invoices
            if (i.overall_confidence or 0) < settings.CONFIDENCE_REVIEW
            and i.status != InvoiceStatus.CANCELLED.value
        ),
        "purchase_value": round(sum(i.grand_total or 0 for i in posted), 2),
        "pending_value": round(
            sum(
                i.grand_total or 0
                for i in invoices
                if i.status in (InvoiceStatus.REVIEW_REQUIRED.value, InvoiceStatus.CONFIRMED.value)
            ),
            2,
        ),
        "new_products": new_products,
    }
