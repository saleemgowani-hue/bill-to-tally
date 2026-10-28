"""
Invoice validation.

Two hard rules from the product spec:
  * Never silently correct an accounting figure. Report the discrepancy and
    let a human decide.
  * Never assume GST treatment that the bill does not show.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database.models import Invoice, InvoiceStatus
from utils.config import settings
from utils.logging import get_logger

log = get_logger("validator")


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class Issue:
    severity: str
    field: str
    message: str
    line_index: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "field": self.field,
            "message": self.message,
            "line_index": self.line_index,
        }


@dataclass
class ValidationReport:
    issues: List[Issue] = field(default_factory=list)
    calculated_subtotal: float = 0.0
    calculated_tax: float = 0.0
    calculated_cess: float = 0.0
    calculated_total: float = 0.0
    extracted_total: float = 0.0
    difference: float = 0.0
    tax_scope: str = "UNKNOWN"
    requires_review: bool = True
    line_flags: Dict[int, List[str]] = field(default_factory=dict)

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR.value]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING.value]

    @property
    def is_blocking(self) -> bool:
        return bool(self.errors)

    def add(self, severity: Severity, field_name: str, message: str, line=None) -> None:
        self.issues.append(Issue(severity.value, field_name, message, line))
        if line is not None:
            self.line_flags.setdefault(line, []).append(message)

    def to_dict(self) -> dict:
        return {
            "issues": [i.to_dict() for i in self.issues],
            "calculated_subtotal": self.calculated_subtotal,
            "calculated_tax": self.calculated_tax,
            "calculated_cess": self.calculated_cess,
            "calculated_total": self.calculated_total,
            "extracted_total": self.extracted_total,
            "difference": self.difference,
            "tax_scope": self.tax_scope,
            "requires_review": self.requires_review,
        }


def money(value: Any) -> float:
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def state_code_from_gstin(gstin: Optional[str]) -> str:
    text = re.sub(r"\s", "", (gstin or "").upper())
    return text[:2] if len(text) >= 2 and text[:2].isdigit() else ""


def detect_tax_scope(
    supplier_gstin: Optional[str],
    buyer_gstin: Optional[str],
    items: List[dict],
) -> str:
    """
    INTRA-STATE / INTER-STATE / UNKNOWN.

    GSTIN state codes are the primary signal. If they are missing we fall back
    to what is actually printed in the tax columns — and if that is ambiguous
    we return UNKNOWN rather than guessing.
    """
    supplier_state = state_code_from_gstin(supplier_gstin)
    buyer_state = state_code_from_gstin(buyer_gstin)
    if supplier_state and buyer_state:
        return "INTRA-STATE" if supplier_state == buyer_state else "INTER-STATE"

    has_cgst_sgst = any(money(i.get("cgst")) or money(i.get("sgst")) for i in items)
    has_igst = any(money(i.get("igst")) for i in items)
    if has_cgst_sgst and not has_igst:
        return "INTRA-STATE"
    if has_igst and not has_cgst_sgst:
        return "INTER-STATE"
    return "UNKNOWN"


class InvoiceValidator:
    def __init__(
        self,
        tolerance: Optional[float] = None,
        confidence_high: Optional[float] = None,
        confidence_review: Optional[float] = None,
    ):
        self.tolerance = tolerance if tolerance is not None else settings.ROUNDING_TOLERANCE
        self.confidence_high = (
            confidence_high if confidence_high is not None else settings.CONFIDENCE_HIGH
        )
        self.confidence_review = (
            confidence_review
            if confidence_review is not None
            else settings.CONFIDENCE_REVIEW
        )

    # ------------------------------------------------------------------
    def validate(self, data: dict, buyer_gstin: str = "") -> ValidationReport:
        report = ValidationReport()
        items = data.get("items") or []
        supplier = data.get("supplier") or {}
        invoice = data.get("invoice") or {}

        if not items:
            report.add(
                Severity.ERROR, "items", "No line items were extracted from this bill."
            )

        # -- header sanity ---------------------------------------------
        if not (supplier.get("name") or "").strip():
            report.add(Severity.ERROR, "supplier.name", "Supplier name is missing.")
        gstin = (supplier.get("gstin") or "").strip()
        if gstin and not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]", gstin):
            report.add(
                Severity.WARNING,
                "supplier.gstin",
                f"GSTIN '{gstin}' does not match the standard 15-character format.",
            )
        if not (invoice.get("invoice_number") or "").strip():
            report.add(
                Severity.ERROR, "invoice.invoice_number", "Invoice number is missing."
            )
        if not (invoice.get("invoice_date") or "").strip():
            report.add(Severity.ERROR, "invoice.invoice_date", "Invoice date is missing.")

        report.tax_scope = detect_tax_scope(gstin, buyer_gstin, items)
        if report.tax_scope == "UNKNOWN" and items:
            report.add(
                Severity.WARNING,
                "tax_scope",
                "Could not determine whether this is an intra-state or inter-state "
                "purchase. Confirm the tax treatment before posting.",
            )

        # -- line by line ----------------------------------------------
        subtotal = tax_total = cess_total = 0.0
        for index, item in enumerate(items):
            subtotal += self._validate_line(report, index, item, report.tax_scope)
            tax_total += money(item.get("cgst")) + money(item.get("sgst")) + money(
                item.get("igst")
            )
            cess_total += money(item.get("cess"))

        report.calculated_subtotal = round(subtotal, 2)
        report.calculated_tax = round(tax_total, 2)
        report.calculated_cess = round(cess_total, 2)
        round_off = money(data.get("round_off"))
        report.calculated_total = round(subtotal + tax_total + cess_total + round_off, 2)
        report.extracted_total = money(data.get("grand_total"))

        # -- invoice level reconciliation ------------------------------
        if report.extracted_total:
            report.difference = round(
                report.extracted_total - report.calculated_total, 2
            )
            if abs(report.difference) > self.tolerance:
                report.add(
                    Severity.ERROR,
                    "grand_total",
                    f"Invoice total mismatch: extracted ₹{report.extracted_total:,.2f} "
                    f"vs calculated ₹{report.calculated_total:,.2f} "
                    f"(difference ₹{report.difference:,.2f}).",
                )
            elif abs(report.difference) > 0.009:
                report.add(
                    Severity.INFO,
                    "grand_total",
                    f"Rounding difference of ₹{report.difference:,.2f} accepted.",
                )
        else:
            report.add(
                Severity.WARNING,
                "grand_total",
                "Grand total was not readable on the bill; the calculated total "
                "will be used only after you confirm it.",
            )

        declared_subtotal = money(data.get("subtotal"))
        if declared_subtotal and abs(declared_subtotal - report.calculated_subtotal) > self.tolerance:
            report.add(
                Severity.WARNING,
                "subtotal",
                f"Printed taxable value ₹{declared_subtotal:,.2f} differs from the "
                f"sum of line items ₹{report.calculated_subtotal:,.2f}.",
            )

        declared_tax = money(data.get("tax_total"))
        if declared_tax and abs(declared_tax - report.calculated_tax) > self.tolerance:
            report.add(
                Severity.WARNING,
                "tax_total",
                f"Printed tax total ₹{declared_tax:,.2f} differs from the sum of "
                f"line taxes ₹{report.calculated_tax:,.2f}.",
            )

        # -- confidence -------------------------------------------------
        overall = float(data.get("overall_confidence") or 0.0)
        if overall < self.confidence_review:
            report.add(
                Severity.ERROR,
                "confidence",
                f"Overall extraction confidence is {overall:.0%} — below the "
                f"{self.confidence_review:.0%} mandatory-review threshold.",
            )
        elif overall < self.confidence_high:
            report.add(
                Severity.WARNING,
                "confidence",
                f"Overall extraction confidence is {overall:.0%}. Please verify "
                "the highlighted fields.",
            )

        report.requires_review = bool(report.errors or report.warnings)
        return report

    # ------------------------------------------------------------------
    def _validate_line(
        self, report: ValidationReport, index: int, item: dict, tax_scope: str
    ) -> float:
        label = item.get("raw_name") or f"line {index + 1}"
        quantity = money(item.get("quantity"))
        rate = money(item.get("rate"))
        discount = money(item.get("discount"))
        taxable = money(item.get("taxable_value"))
        gst_rate = money(item.get("gst_rate"))
        cgst = money(item.get("cgst"))
        sgst = money(item.get("sgst"))
        igst = money(item.get("igst"))
        cess = money(item.get("cess"))
        total = money(item.get("total"))
        confidence = float(item.get("confidence") or 0.0)

        if quantity <= 0:
            report.add(
                Severity.ERROR, "quantity", f"{label}: quantity is zero or missing.", index
            )
        if rate <= 0:
            report.add(
                Severity.ERROR, "rate", f"{label}: rate is zero or missing.", index
            )
        if quantity > 100_000:
            report.add(
                Severity.WARNING,
                "quantity",
                f"{label}: quantity {quantity:,.0f} looks unusually large — confirm it.",
                index,
            )
        if rate > 1_000_000:
            report.add(
                Severity.WARNING,
                "rate",
                f"{label}: rate ₹{rate:,.2f} looks unusually large — confirm it.",
                index,
            )
        if discount < 0:
            report.add(
                Severity.WARNING, "discount", f"{label}: discount is negative.", index
            )
        if discount > quantity * rate and quantity and rate:
            report.add(
                Severity.ERROR,
                "discount",
                f"{label}: discount ₹{discount:,.2f} exceeds the line value.",
                index,
            )

        # Qty x Rate - Discount = Taxable value
        computed_taxable = round(quantity * rate - discount, 2)
        if taxable:
            if abs(taxable - computed_taxable) > self.tolerance:
                report.add(
                    Severity.ERROR,
                    "taxable_value",
                    f"{label}: taxable value ₹{taxable:,.2f} does not reconcile with "
                    f"qty × rate − discount = ₹{computed_taxable:,.2f}.",
                    index,
                )
        else:
            taxable = computed_taxable

        # Tax check against the printed GST %
        if gst_rate:
            expected_tax = round(taxable * gst_rate / 100.0, 2)
            actual_tax = round(cgst + sgst + igst, 2)
            if actual_tax and abs(actual_tax - expected_tax) > max(self.tolerance, expected_tax * 0.02):
                report.add(
                    Severity.WARNING,
                    "gst",
                    f"{label}: tax on the bill ₹{actual_tax:,.2f} does not match "
                    f"{gst_rate:g}% of ₹{taxable:,.2f} (₹{expected_tax:,.2f}).",
                    index,
                )
            if not actual_tax:
                report.add(
                    Severity.WARNING,
                    "gst",
                    f"{label}: GST {gst_rate:g}% is shown but no tax amount was read.",
                    index,
                )

        if cgst and igst:
            report.add(
                Severity.ERROR,
                "gst",
                f"{label}: both CGST/SGST and IGST are present on the same line.",
                index,
            )
        if tax_scope == "INTER-STATE" and (cgst or sgst):
            report.add(
                Severity.WARNING,
                "gst",
                f"{label}: invoice looks inter-state but CGST/SGST was read.",
                index,
            )
        if tax_scope == "INTRA-STATE" and igst:
            report.add(
                Severity.WARNING,
                "gst",
                f"{label}: invoice looks intra-state but IGST was read.",
                index,
            )
        if cgst and sgst and abs(cgst - sgst) > 0.5:
            report.add(
                Severity.WARNING,
                "gst",
                f"{label}: CGST ₹{cgst:,.2f} and SGST ₹{sgst:,.2f} should normally be equal.",
                index,
            )

        # Line total
        computed_total = round(taxable + cgst + sgst + igst + cess, 2)
        if total and abs(total - computed_total) > self.tolerance:
            report.add(
                Severity.ERROR,
                "total",
                f"{label}: line total ₹{total:,.2f} does not reconcile with "
                f"taxable + tax = ₹{computed_total:,.2f}.",
                index,
            )

        # Confidence
        # A confidence score says how sure the AI was, and the whole point of
        # the review screen is to settle that. Once a person has opened the
        # line, checked it against the paper bill and saved it, the AI's
        # uncertainty has been answered — so it stops blocking. Arithmetic and
        # GST checks above are unaffected: those are facts about the numbers,
        # not about how confidently they were read, and they still block.
        verified = bool(item.get("verified"))
        if verified and confidence < self.confidence_high:
            report.add(
                Severity.INFO,
                "confidence",
                f"{label}: read at {confidence:.0%} confidence, checked and saved by a user.",
                index,
            )
        elif confidence and confidence < self.confidence_review:
            report.add(
                Severity.ERROR,
                "confidence",
                f"{label}: read with {confidence:.0%} confidence — open the line, "
                "check it against the paper bill, then press Save line.",
                index,
            )
        elif confidence and confidence < self.confidence_high:
            report.add(
                Severity.WARNING,
                "confidence",
                f"{label}: read with {confidence:.0%} confidence — please verify.",
                index,
            )
        return taxable


# --------------------------------------------------------------------------
# Duplicate detection
# --------------------------------------------------------------------------
@dataclass
class DuplicateCheck:
    is_duplicate: bool
    confidence: float
    reason: str
    invoice_id: Optional[int] = None
    invoice_label: str = ""


def check_duplicate(
    session: Session,
    company_id: int,
    supplier_name: str,
    invoice_number: str,
    invoice_date: str,
    grand_total: float,
    file_hash: str = "",
    exclude_invoice_id: Optional[int] = None,
) -> DuplicateCheck:
    """
    Look for an existing invoice that is probably the same bill.

    Strongest signal is an identical file. Next is supplier + invoice number.
    Weakest is same supplier, date and amount with no invoice number.
    """
    query = session.query(Invoice).filter(
        Invoice.company_id == company_id,
        Invoice.status.notin_(
            [InvoiceStatus.CANCELLED.value, InvoiceStatus.DUPLICATE.value]
        ),
    )
    if exclude_invoice_id:
        query = query.filter(Invoice.id != exclude_invoice_id)

    if file_hash:
        same_file = query.filter(Invoice.file_hash == file_hash).first()
        if same_file:
            return DuplicateCheck(
                True,
                1.0,
                "The exact same bill image has already been uploaded.",
                same_file.id,
                f"{same_file.supplier_name} / {same_file.invoice_number}",
            )

    supplier_key = (supplier_name or "").strip().lower()
    number_key = re.sub(r"[^a-z0-9]", "", (invoice_number or "").lower())

    if number_key:
        for candidate in query.all():
            cand_number = re.sub(r"[^a-z0-9]", "", (candidate.invoice_number or "").lower())
            cand_supplier = (candidate.supplier_name or "").strip().lower()
            if cand_number and cand_number == number_key:
                if cand_supplier == supplier_key:
                    return DuplicateCheck(
                        True,
                        0.97,
                        f"Invoice {candidate.invoice_number} from "
                        f"{candidate.supplier_name} was already processed on "
                        f"{candidate.created_at:%d-%m-%Y}.",
                        candidate.id,
                        f"{candidate.supplier_name} / {candidate.invoice_number}",
                    )
                return DuplicateCheck(
                    True,
                    0.70,
                    f"Invoice number {candidate.invoice_number} already exists "
                    f"(under supplier '{candidate.supplier_name}').",
                    candidate.id,
                    f"{candidate.supplier_name} / {candidate.invoice_number}",
                )

    if supplier_key and invoice_date and grand_total:
        for candidate in query.all():
            if (
                (candidate.supplier_name or "").strip().lower() == supplier_key
                and (candidate.invoice_date or "") == invoice_date
                and abs((candidate.grand_total or 0) - grand_total) < 1.0
            ):
                return DuplicateCheck(
                    True,
                    0.80,
                    f"A bill from {candidate.supplier_name} dated {invoice_date} "
                    f"for ₹{grand_total:,.2f} already exists.",
                    candidate.id,
                    f"{candidate.supplier_name} / {candidate.invoice_number}",
                )

    return DuplicateCheck(False, 0.0, "No duplicate found.")
