"""
Glue between the universal extraction and the stored Purchase/Sales
invoices: saves the extra fields (charges, document type, e-way bill, amount
in words ...) on the invoice row, feeds them back into re-validation, and
turns the charges into Tally ledger lines so the voucher total matches the
bill total.
"""
from __future__ import annotations

import json
from typing import List

from database.database import get_mapping
from services.invoice_validator import (
    STATUS_READY,
    ValidationReport,
    verification_status,
)

CHARGE_FIELDS = ("freight", "packing_charges", "other_charges")

# mapping key, default ledger name (purchase), default ledger name (sales)
CHARGE_LEDGERS = {
    "freight": ("freight_ledger", "Freight Inward", "Freight Outward"),
    "packing_charges": ("packing_ledger", "Packing Charges", "Packing Charges Recovered"),
    "other_charges": ("other_charges_ledger", "Other Charges", "Other Charges Recovered"),
}


def apply_universal_fields(invoice, data: dict, report: ValidationReport) -> None:
    extras = data.get("universal") or {}
    charges = extras.get("charges") or {}
    for name in CHARGE_FIELDS:
        setattr(invoice, name, float(charges.get(name) or 0.0))
    invoice.document_type = (extras.get("document_type") or "")[:40] or None
    if extras:
        invoice.universal_json = json.dumps(extras, ensure_ascii=False)[:60000]
    invoice.verification_status = verification_status(report)


def universal_block(invoice) -> dict:
    """The 'universal' part of an invoice dict, rebuilt from the row (so edits
    to the charges on the review screen are what gets validated)."""
    try:
        extras = json.loads(invoice.universal_json or "{}")
    except (TypeError, ValueError):
        extras = {}
    if not extras and not any(getattr(invoice, n, 0) for n in CHARGE_FIELDS):
        return {}
    extras["charges"] = {n: float(getattr(invoice, n, 0) or 0) for n in CHARGE_FIELDS}
    return extras


def refresh_status(invoice, report: ValidationReport, confirmed: bool = False) -> None:
    invoice.verification_status = verification_status(report, confirmed=confirmed)


def mark_ready(invoice) -> None:
    invoice.verification_status = STATUS_READY


def charge_ledger_lines(session, company_id: int, invoice, kind: str = "purchase") -> List[dict]:
    """Voucher lines for freight/packing/other charges. Debited on a
    purchase, credited on a sale — same side as the tax lines."""
    lines = []
    for name in CHARGE_FIELDS:
        value = round(float(getattr(invoice, name, 0) or 0), 2)
        if abs(value) < 0.005:
            continue
        key, purchase_default, sales_default = CHARGE_LEDGERS[name]
        default = sales_default if kind == "sales" else purchase_default
        lines.append({
            "ledger": get_mapping(session, company_id, key, default) or default,
            "amount": value,
            "group": (
                "Indirect Incomes" if kind == "sales"
                else "Direct Expenses" if name == "freight" else "Indirect Expenses"
            ),
            "kind": "charge",
        })
    return lines


def auto_create_enabled(session, company_id: int) -> bool:
    """Auto-create missing Tally ledgers / units / stock items before posting.
    On by default (customer build behaviour); can be switched off in Settings."""
    from database.database import get_setting
    return str(get_setting(session, company_id, "auto_create_masters", "1")) != "0"


def ensure_tally_masters(session, company, user, connector, payload, invoice_field: str,
                         invoice_id: int) -> dict:
    """Create missing masters and write what was created to the audit log."""
    from services import audit_service
    if not auto_create_enabled(session, company.id):
        return {"created": [], "failed": []}
    payload.party_group = payload.party_group or get_mapping(
        session, company.id,
        "customer_ledger_group" if payload.transaction_kind == "sales" else "supplier_ledger_group",
        "Sundry Debtors" if payload.transaction_kind == "sales" else "Sundry Creditors",
    )
    payload.stock_group = payload.stock_group or get_mapping(
        session, company.id, "stock_group", "Primary"
    )
    result = connector.ensure_masters(payload)
    if result["created"] or result["failed"]:
        detail = ""
        if result["created"]:
            detail += "Created in Tally: " + "; ".join(result["created"])
        if result["failed"]:
            detail += (" | " if detail else "") + "Could not create: " + "; ".join(result["failed"])
        kwargs = {invoice_field: invoice_id}
        audit_service.record(
            session, company.id, "TALLY_MASTERS_AUTO_CREATED", detail[:4000],
            user=user, **kwargs,
        )
    return result
