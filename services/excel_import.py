"""
Excel / CSV → invoice import.

Lets a shop bring purchase data in from a spreadsheet instead of a bill
photo. This module only does *extraction* — turning rows into the same
normalized invoice dict shape `ai_invoice_parser.normalize_invoice` produces
from OCR. Everything downstream (supplier matching, GST/arithmetic
validation, product matching, duplicate detection, human review, Tally
export) is the existing pipeline in `purchase_service._finalize_invoice` —
see `purchase_service.ingest_excel`, which calls into this module.

Design choices worth knowing about:

* Column headers are matched loosely (case/space/punctuation-insensitive,
  with common aliases — see COLUMN_ALIASES) so a shop's own sheet usually
  works without renaming columns first.
* Rows are grouped into separate invoices by (supplier, invoice number) when
  those columns exist, so one sheet can carry many bills at once. If the
  sheet has no supplier/invoice-number columns at all, every row is treated
  as one single invoice.
* Anything the sheet does not provide (GST split, taxable value, line total)
  is computed with plain arithmetic (qty*rate-discount, etc.) only when the
  sheet leaves it blank — never overwritten if the sheet did provide a
  number. We do not invent or "correct" figures beyond that; a mismatch is
  left for the validator to flag on the review screen, exactly like a
  badly-photographed bill would be.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import pandas as pd

from services.ai_invoice_parser import normalize_invoice, to_number, to_text


class ExcelImportError(RuntimeError):
    pass


# canonical field -> accepted header spellings (already lower/space-normalized)
COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "supplier_name": (
        "supplier", "supplier name", "vendor", "vendor name", "party",
        "party name", "firm", "firm name", "seller", "seller name",
        # Sales-side aliases — the same canonical field doubles as "the other
        # party on the bill", which for a sales invoice is the customer.
        "customer", "customer name", "buyer", "buyer name", "client", "client name",
    ),
    "supplier_gstin": (
        "gstin", "supplier gstin", "vendor gstin", "party gstin",
        "gst no", "gst number", "gst no.",
        "customer gstin", "buyer gstin", "client gstin",
    ),
    "supplier_address": (
        "address", "supplier address", "vendor address",
        "customer address", "buyer address",
    ),
    "invoice_number": (
        "invoice no", "invoice no.", "invoice number", "invoice #",
        "bill no", "bill no.", "bill number", "inv no", "inv no.", "inv number",
    ),
    "invoice_date": ("invoice date", "bill date", "date"),
    "item_name": (
        "item", "item name", "product", "product name", "description",
        "particulars", "goods", "goods description",
    ),
    "hsn": ("hsn", "hsn code", "hsn/sac", "hsn sac", "sac"),
    "barcode": ("barcode", "ean", "ean code"),
    "quantity": ("qty", "quantity", "qty.", "no of units", "nos"),
    "unit": ("unit", "uom", "units"),
    "rate": ("rate", "price", "unit price", "unit rate", "rate per unit"),
    "discount": ("discount", "disc", "disc.", "discount amount"),
    "taxable_value": ("taxable value", "taxable amt", "taxable amount"),
    "gst_rate": ("gst", "gst%", "gst %", "gst rate", "tax%", "tax %", "tax rate"),
    "cgst": ("cgst", "cgst amt", "cgst amount"),
    "sgst": ("sgst", "sgst amt", "sgst amount"),
    "igst": ("igst", "igst amt", "igst amount"),
    "cess": ("cess", "cess amt", "cess amount"),
    "total": ("total", "amount", "line total", "net amount", "total amount"),
}

# A row needs at least an item name to count as a purchase line.
REQUIRED_FIELDS = ("item_name",)


def _normalize_header(text: str) -> str:
    text = str(text or "").strip().lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def detect_columns(columns: List[str]) -> Dict[str, str]:
    """Map canonical field name -> actual dataframe column name, best-effort."""
    normalized = {col: _normalize_header(col) for col in columns}
    mapping: Dict[str, str] = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for col, norm in normalized.items():
            if norm in aliases or norm == field_name.replace("_", " "):
                mapping[field_name] = col
                break
    return mapping


def read_table(raw_bytes: bytes, filename: str) -> pd.DataFrame:
    """Load a .xlsx / .xls / .csv file into a DataFrame."""
    name = (filename or "").lower()
    try:
        if name.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw_bytes))
        elif name.endswith(".xls"):
            df = pd.read_excel(io.BytesIO(raw_bytes))
        else:
            df = pd.read_excel(io.BytesIO(raw_bytes), engine="openpyxl")
    except Exception as exc:
        raise ExcelImportError(f"Could not read '{filename}': {exc}") from exc

    df = df.dropna(how="all")
    df.columns = [str(c) for c in df.columns]
    if df.empty:
        raise ExcelImportError("The file has no data rows.")
    return df


@dataclass
class ExcelGroup:
    data: dict
    warnings: List[str] = field(default_factory=list)
    row_count: int = 0


def _cell(row, mapping: Dict[str, str], field_name: str, default=""):
    col = mapping.get(field_name)
    if col is None:
        return default
    value = row.get(col, default)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    return value


def build_invoices(df: pd.DataFrame) -> List[ExcelGroup]:
    """Turn a raw DataFrame into one or more normalized invoice dicts."""
    mapping = detect_columns(list(df.columns))
    missing = [f for f in REQUIRED_FIELDS if f not in mapping]
    if missing:
        raise ExcelImportError(
            "Could not find a column for: " + ", ".join(missing) +
            ". Rename that column (e.g. 'Item' or 'Product Name') and re-upload, "
            "or download the sample template for the expected headers."
        )

    has_grouping = "invoice_number" in mapping or "supplier_name" in mapping
    groups: Dict[Tuple[str, str], dict] = {}
    order: List[Tuple[str, str]] = []

    for _, row in df.iterrows():
        item_name = to_text(_cell(row, mapping, "item_name", ""))
        if not item_name:
            continue  # blank or summary/total row — skip it quietly

        supplier_name = to_text(_cell(row, mapping, "supplier_name", ""))
        invoice_number = to_text(_cell(row, mapping, "invoice_number", ""))
        key = (
            (supplier_name.lower(), invoice_number.lower())
            if has_grouping else ("", "")
        )

        if key not in groups:
            groups[key] = {
                "supplier": {
                    "name": supplier_name,
                    "gstin": to_text(_cell(row, mapping, "supplier_gstin", "")),
                    "address": to_text(_cell(row, mapping, "supplier_address", "")),
                },
                "invoice": {
                    "invoice_number": invoice_number,
                    "invoice_date": to_text(_cell(row, mapping, "invoice_date", "")),
                    "due_date": "",
                },
                "items": [],
                "subtotal": 0.0, "discount_total": 0.0, "tax_total": 0.0,
                "cess_total": 0.0, "round_off": 0.0, "grand_total": 0.0,
                "overall_confidence": 1.0,
            }
            order.append(key)

        quantity = to_number(_cell(row, mapping, "quantity", 0))
        rate = to_number(_cell(row, mapping, "rate", 0))
        discount = to_number(_cell(row, mapping, "discount", 0))
        cgst = to_number(_cell(row, mapping, "cgst", 0))
        sgst = to_number(_cell(row, mapping, "sgst", 0))
        igst = to_number(_cell(row, mapping, "igst", 0))
        cess = to_number(_cell(row, mapping, "cess", 0))

        taxable_value = to_number(_cell(row, mapping, "taxable_value", 0))
        if not taxable_value:
            taxable_value = round(quantity * rate - discount, 2)

        total = to_number(_cell(row, mapping, "total", 0))
        if not total:
            total = round(taxable_value + cgst + sgst + igst + cess, 2)

        groups[key]["items"].append({
            "raw_name": item_name,
            "normalized_name": item_name,
            "hsn": to_text(_cell(row, mapping, "hsn", ""), 20),
            "barcode": re.sub(r"\D", "", to_text(_cell(row, mapping, "barcode", ""), 30)),
            "unit": to_text(_cell(row, mapping, "unit", "PCS"), 20) or "PCS",
            "quantity": quantity,
            "rate": rate,
            "discount": discount,
            "taxable_value": taxable_value,
            "gst_rate": to_number(_cell(row, mapping, "gst_rate", 0)),
            "cgst": cgst,
            "sgst": sgst,
            "igst": igst,
            "cess": cess,
            "total": total,
            "confidence": 1.0,
        })

    if not groups:
        raise ExcelImportError(
            "No usable rows found — every row is missing an item name."
        )

    results: List[ExcelGroup] = []
    for key in order:
        raw = groups[key]
        # A line-item sheet rarely prints its own "grand total" cell — it is
        # simply the sum of the line totals. subtotal/tax_total/cess_total
        # get this same treatment inside normalize_invoice when left at 0;
        # grand_total does not, because the OCR path always has a printed
        # figure to trust. Excel doesn't, so compute it here.
        raw["grand_total"] = round(sum(item["total"] for item in raw["items"]), 2)
        data = normalize_invoice(raw)
        warnings: List[str] = []
        if not data["supplier"]["name"]:
            warnings.append(
                "No supplier name found for this group — add it in Review Bills."
            )
        if not data["invoice"]["invoice_number"]:
            warnings.append(
                "No invoice number found — add it in Review Bills so duplicate "
                "bills can be caught."
            )
        results.append(
            ExcelGroup(data=data, warnings=warnings, row_count=len(raw["items"]))
        )
    return results


def sample_template_bytes() -> bytes:
    """A blank sheet with the recognized headers, for the 'download template' button."""
    columns = [
        "Supplier Name", "Supplier GSTIN", "Invoice Number", "Invoice Date",
        "Item Name", "HSN", "Barcode", "Qty", "Unit", "Rate", "Discount",
        "Taxable Value", "GST Rate", "CGST", "SGST", "IGST", "Cess", "Total",
    ]
    sample_rows = [
        {
            "Supplier Name": "ABC Traders", "Supplier GSTIN": "23ABCDE1234F1Z5",
            "Invoice Number": "INV-101", "Invoice Date": "01-09-2026",
            "Item Name": "Basmati Rice 25kg", "HSN": "1006", "Barcode": "",
            "Qty": 10, "Unit": "BAG", "Rate": 1200, "Discount": 0,
            "Taxable Value": 12000, "GST Rate": 5, "CGST": 300, "SGST": 300,
            "IGST": 0, "Cess": 0, "Total": 12600,
        },
        {
            "Supplier Name": "ABC Traders", "Supplier GSTIN": "23ABCDE1234F1Z5",
            "Invoice Number": "INV-101", "Invoice Date": "01-09-2026",
            "Item Name": "Refined Oil 15L", "HSN": "1512", "Barcode": "",
            "Qty": 5, "Unit": "CTN", "Rate": 1800, "Discount": 50,
            "Taxable Value": 8950, "GST Rate": 5, "CGST": 223.75, "SGST": 223.75,
            "IGST": 0, "Cess": 0, "Total": 9397.5,
        },
    ]
    df = pd.DataFrame(sample_rows, columns=columns)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Purchases")
    return buffer.getvalue()
