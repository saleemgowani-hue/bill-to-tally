"""
Bank statement import — Excel/CSV only for now (most netbanking portals let
you export a statement as XLS/XLSX/CSV; scanned/PDF statement photos are not
handled by this module).

Column headers are matched loosely, the same way services/excel_import.py
does for purchase/sales sheets, because every bank's export uses slightly
different wording (Narration vs Description vs Particulars, Withdrawal vs
Debit, etc).

A statement usually has separate Debit and Credit columns; some instead have
one signed "Amount" column. Both shapes are supported.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

import pandas as pd

from services.ai_invoice_parser import to_number, to_text
from services.excel_import import ExcelImportError, read_table

COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "date": ("date", "txn date", "transaction date", "value date", "posting date"),
    "narration": (
        "narration", "description", "particulars", "details", "transaction details",
        "remarks", "transaction remarks",
    ),
    "reference_no": (
        "reference", "reference no", "reference no.", "chq no", "chq/ref no",
        "cheque no", "utr", "utr no", "transaction id",
    ),
    "debit": ("debit", "withdrawal", "withdrawal amt", "debit amount", "dr"),
    "credit": ("credit", "deposit", "deposit amt", "credit amount", "cr"),
    "amount": ("amount", "amount (inr)", "transaction amount"),
    "balance": ("balance", "closing balance", "available balance", "running balance"),
}

REQUIRED_FIELDS = ("date", "narration")


def make_row_hash(txn_date: str, narration: str, reference_no: str, debit: float, credit: float) -> str:
    """Single source of truth for a transaction's dedupe hash — used by both
    the Excel/CSV path here and the photo/PDF OCR path in
    bank_statement_parser.py, so the same line imported either way hashes
    the same and duplicate detection works across both."""
    return hashlib.sha256(
        "|".join([txn_date, narration, reference_no, f"{debit:.2f}", f"{credit:.2f}"]).encode()
    ).hexdigest()


def _normalize_header(text: str) -> str:
    text = str(text or "").strip().lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def detect_columns(columns: List[str]) -> Dict[str, str]:
    normalized = {col: _normalize_header(col) for col in columns}
    mapping: Dict[str, str] = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for col, norm in normalized.items():
            if norm in aliases or norm == field_name.replace("_", " "):
                mapping[field_name] = col
                break
    return mapping


@dataclass
class BankRow:
    txn_date: str
    narration: str
    reference_no: str
    debit: float
    credit: float
    balance: float
    row_hash: str


def _cell(row, mapping: Dict[str, str], field_name: str, default=""):
    col = mapping.get(field_name)
    if col is None:
        return default
    value = row.get(col, default)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    return value


def parse_bank_statement(raw_bytes: bytes, filename: str) -> List[BankRow]:
    df = read_table(raw_bytes, filename)
    mapping = detect_columns(list(df.columns))

    missing = [f for f in REQUIRED_FIELDS if f not in mapping]
    if missing:
        raise ExcelImportError(
            "Could not find a column for: " + ", ".join(missing) +
            ". A bank statement needs at least a date and a narration/description column."
        )
    if "debit" not in mapping and "credit" not in mapping and "amount" not in mapping:
        raise ExcelImportError(
            "Could not find a Debit/Credit column, or a single signed Amount column."
        )

    rows: List[BankRow] = []
    for _, raw_row in df.iterrows():
        narration = to_text(_cell(raw_row, mapping, "narration", ""))
        date_value = to_text(_cell(raw_row, mapping, "date", ""))
        if not narration and not date_value:
            continue

        debit = to_number(_cell(raw_row, mapping, "debit", 0))
        credit = to_number(_cell(raw_row, mapping, "credit", 0))
        if "amount" in mapping and not debit and not credit:
            signed = to_number(_cell(raw_row, mapping, "amount", 0))
            if signed < 0:
                debit = abs(signed)
            elif signed > 0:
                credit = signed
        if not debit and not credit:
            continue

        reference_no = to_text(_cell(raw_row, mapping, "reference_no", ""))
        balance = to_number(_cell(raw_row, mapping, "balance", 0))

        row_hash = make_row_hash(date_value, narration, reference_no, debit, credit)

        rows.append(
            BankRow(
                txn_date=date_value, narration=narration, reference_no=reference_no,
                debit=debit, credit=credit, balance=balance, row_hash=row_hash,
            )
        )

    if not rows:
        raise ExcelImportError("No usable transaction rows found in this statement.")
    return rows


def sample_statement_bytes() -> bytes:
    rows = [
        {"Date": "01-09-2026", "Narration": "NEFT DR-HDFC0000123-SHREEJI DISTRIBUTORS-INV-101",
         "Reference No": "N123456789", "Debit": 12600, "Credit": "", "Balance": 87400},
        {"Date": "05-09-2026", "Narration": "NEFT CR-ICIC0000456-RETAIL MART-SI-1",
         "Reference No": "N987654321", "Debit": "", "Credit": 3200.5, "Balance": 90600.5},
        {"Date": "07-09-2026", "Narration": "BANK CHARGES-QTR MAINTENANCE",
         "Reference No": "", "Debit": 236, "Credit": "", "Balance": 90364.5},
    ]
    columns = ["Date", "Narration", "Reference No", "Debit", "Credit", "Balance"]
    df = pd.DataFrame(rows, columns=columns)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Statement")
    return buffer.getvalue()
