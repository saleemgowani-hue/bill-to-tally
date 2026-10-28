"""
Bank statement extraction from a photo or PDF — the vision-OCR counterpart
of bank_statement_import.py's Excel/CSV reader. Both paths produce the same
BankRow objects (identically hashed — see make_row_hash) so a statement can
be re-imported as either an Excel export or a scan without creating
duplicates, and every downstream step (bank_service.import_bank_statement,
matching, allocation, Tally posting) is unaware of which path a line came
from.

Reuses the exact same OCRProvider abstraction as ai_invoice_parser.py
(services/ocr_service.py) — only the prompt and JSON schema are different,
since a statement is a flat list of transactions, not an invoice with a
supplier and line items.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional

from PIL import Image

from services.ai_invoice_parser import normalize_date, to_number, to_text
from services.bank_statement_import import BankRow, make_row_hash
from services.ocr_service import OCRError, OCRProvider, OCRResponse

BANK_SCHEMA_HINT = """Return ONLY a JSON object with exactly this shape (no markdown, no commentary):

{
  "transactions": [
    {
      "date": "", "narration": "", "reference_no": "",
      "debit": 0, "credit": 0, "balance": 0, "confidence": 0
    }
  ],
  "overall_confidence": 0
}"""

BANK_EXTRACTION_PROMPT = """You are an expert bank-reconciliation data entry operator reading a page of an Indian bank statement (a photo or a PDF page).

Extract every transaction row exactly as printed. Rules:
1. NEVER guess. If a value is not printed or not legible, use "" for text and 0 for numbers, and lower that row's confidence.
2. Read every row on the page, in the order printed. If several pages are supplied they belong to ONE statement; list every page's rows in order.
3. Dates must be ISO format YYYY-MM-DD. Indian statements are usually DD/MM/YYYY — convert accordingly.
4. Numbers must be plain (1234.50), with no currency symbols or commas.
5. debit is money leaving the account (withdrawal); credit is money entering it (deposit). A row has exactly one of the two non-zero, never both.
6. If the statement has one signed "Amount" column instead of separate Debit/Credit columns, put a negative amount in debit (as a positive number) and a positive amount in credit.
7. narration is the full description/particulars text exactly as printed, including any reference codes within it.
8. reference_no is a cheque number, UTR, or transaction ID if printed as its own column — leave "" if it is only embedded in the narration text.
9. balance is the running/closing balance printed on that row, if shown.
10. Skip opening-balance header rows and any row that has no amount at all.
11. confidence is 0.0-1.0 per row: how sure you are that every field on that row was read correctly.
12. overall_confidence reflects image legibility and how completely the statement was captured.
13. If the image is not a bank statement at all, return {"transactions": [], "overall_confidence": 0}."""

REPAIR_PROMPT = """The following text was supposed to be a single valid JSON object matching the required bank-statement schema, but it is malformed. Return ONLY the corrected, valid JSON object. Do not add or invent any data that is not already present."""


class BankStatementParseError(OCRError):
    pass


@dataclass
class ParsedBankStatement:
    provider: str
    model: str
    rows: List[BankRow]
    overall_confidence: float = 0.0
    raw_text: str = ""
    raw_json: str = ""
    repaired: bool = False
    attempts: int = 1
    duration_ms: int = 0
    warnings: List[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


def _to_rows(data: dict) -> List[BankRow]:
    rows: List[BankRow] = []
    for entry in data.get("transactions", []) or []:
        date_value = normalize_date(entry.get("date", "")) or to_text(entry.get("date", ""), 20)
        narration = to_text(entry.get("narration", ""), 2000)
        if not narration and not date_value:
            continue
        debit = to_number(entry.get("debit", 0))
        credit = to_number(entry.get("credit", 0))
        if not debit and not credit:
            continue
        reference_no = to_text(entry.get("reference_no", ""), 80)
        balance = to_number(entry.get("balance", 0))
        row_hash = make_row_hash(date_value, narration, reference_no, debit, credit)
        rows.append(
            BankRow(
                txn_date=date_value, narration=narration, reference_no=reference_no,
                debit=debit, credit=credit, balance=balance, row_hash=row_hash,
            )
        )
    return rows


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except (ValueError, TypeError):
                return None
    return None


class BankStatementParser:
    """The bank-statement mirror of ai_invoice_parser.InvoiceParser."""

    def __init__(self, provider: OCRProvider, max_attempts: int = 2):
        self.provider = provider
        self.max_attempts = max(1, max_attempts)

    def parse_images(self, images: List[Image.Image]) -> ParsedBankStatement:
        if not images:
            raise BankStatementParseError("No page images supplied.")
        if not self.provider.supports_structured:
            raise BankStatementParseError(
                f"The '{self.provider.name}' engine can only read plain text, not a "
                "bank statement's table layout. Switch to an AI vision engine in "
                "Settings -> AI / OCR, or upload the statement as Excel/CSV instead."
            )

        warnings: List[str] = []
        last_error: Optional[Exception] = None
        response: Optional[OCRResponse] = None
        repaired = False

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.provider.extract_structured(
                    images, BANK_EXTRACTION_PROMPT, BANK_SCHEMA_HINT
                )
                data = response.structured
                if data is None:
                    data = _extract_json(response.raw_response or response.text)
                if data is None:
                    raise ValueError("Response was not valid JSON.")
                rows = _to_rows(data)
                warnings.extend(response.warnings)
                return ParsedBankStatement(
                    provider=response.provider, model=response.model, rows=rows,
                    overall_confidence=to_number(data.get("overall_confidence", 0)),
                    raw_text=response.text, raw_json=response.raw_response,
                    repaired=repaired, attempts=attempt, duration_ms=response.duration_ms,
                    warnings=warnings,
                )
            except Exception as exc:  # noqa: BLE001 - broad on purpose, retried below
                last_error = exc
                if attempt < self.max_attempts and response is not None:
                    try:
                        repair = self.provider.extract_structured(
                            [], f"{REPAIR_PROMPT}\n\n{(response.raw_response or response.text)[:12000]}",
                            BANK_SCHEMA_HINT,
                        )
                        response = repair
                        repaired = True
                        continue
                    except Exception:
                        pass
                break

        raise BankStatementParseError(
            f"Could not extract transactions from this statement: {last_error}"
        )
