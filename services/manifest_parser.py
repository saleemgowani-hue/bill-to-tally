"""
AI extraction for transport manifests / bilties (lorry receipts, consignment
notes) — turns a photo or PDF of a transporter's handwritten manifest into a
row-per-consignment table, for export to Excel.

This is deliberately NOT routed through InvoiceParser (ai_invoice_parser.py):
a manifest has a completely different shape (header fields + a flat table of
consignments, no GST/tax math, no stock items) and a different destination
(an Excel download, not a purchase/sales voucher). The two share only the
generic JSON-repair helpers and the underlying OCRProvider abstraction.

Handwriting is harder than a printed bill, so this only works with a
structured ("vision") provider — see MANIFEST_PROVIDER_REQUIRED below.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from PIL import Image

from services.ai_invoice_parser import (
    ParseError,
    find_json_object,
    local_json_repair,
    strip_code_fences,
)
from services.ocr_service import OCRError, OCRProvider, OCRResponse
from utils.logging import get_logger

log = get_logger("manifest_parser")

MANIFEST_PROVIDER_REQUIRED = (
    "Reading a handwritten manifest needs an AI vision engine — plain OCR "
    "(RapidOCR/Tesseract/PaddleOCR) is built for printed text and cannot "
    "reliably read handwriting. Switch to Gemini, OpenAI, Claude, or Auto "
    "(Hybrid) in Settings → AI / OCR, then come back to this page."
)

SCHEMA_HINT = """Return ONLY a JSON object with exactly this shape (no markdown, no commentary):

{
  "manifest_no": "", "manifest_date": "", "from_station": "", "to_station": "",
  "rows": [
    {
      "s_no": 0, "c_no": "", "station": "", "consignee": "",
      "weight": "", "no_of_pkg": "", "to_pay": "", "paid": "", "amount": "",
      "confidence": 0
    }
  ],
  "overall_confidence": 0
}"""

EXTRACTION_PROMPT = """You are an expert data-entry operator reading a transporter's handwritten \
manifest / bilty / lorry receipt (a goods consignment list, NOT a GST invoice — there is no tax \
or item detail here, just one row per consignment).

Extract exactly what is written. Rules:
1. NEVER guess a digit or a name. If something is not legible, use "" and lower that row's confidence.
2. manifest_no, manifest_date, from_station, to_station come from the form's header area
   (fields such as "Manifest No.", "Dated", "From", "To").
   - from_station / to_station: ONLY the station or town name, e.g. "KOLKATA" or "Raipur". The
     printed "From" line often carries the transporter's street address after the town
     ("KOLKATA - 68, COTTON STREET") - leave the address out. Write the same station the same
     way on every form.
   - manifest_date: copy the digits exactly as written and give it as YYYY-MM-DD. A two-digit
     year means 20YY (05.10.26 is 2026-10-05). Never change the year, day or month.
   If a date is only partly legible, copy exactly what is written instead.
3. Each table row is one consignment. Typical columns, left to right, are: S.No, C.No.
   (consignment/bilty number), Station (destination town for that row — a ditto mark like
   " or -\"- means "same as the row above", so repeat the station name above, do not write the
   ditto mark itself), Consignee (the receiving party's name), Weight, No. of Pkg (packages),
   To Pay, Paid, Amount. Column order and labels vary between transporters — read the actual
   printed headers on this form and map sensibly if they differ.
4. s_no, weight, no_of_pkg are usually plain numbers — copy as plain numbers with no units.
5. c_no (consignment number) is often a 5-7 digit number but copy it exactly as written,
   including any letters.
6. to_pay, paid, amount: these cells often hold EITHER a rupee amount OR a mark, not both.
   - A tick/check mark (✓) means that charge has already been collected/settled — write "Paid".
   - An X, dash, or blank cell means not applicable for that row — write "".
   - Otherwise copy the number exactly as written, with no currency symbol or commas.
7. confidence is 0.0-1.0 per row: how sure you are every cell in that row was read correctly.
   A row with any illegible handwriting should score well below 0.8.
8. overall_confidence reflects image legibility and how completely the manifest was captured.
9. If several pages are supplied they belong to ONE manifest; append all rows in order, keep
   reading s_no upward across pages (don't restart at 1 on page 2 unless the form does).
10. If the image is not a transport manifest/bilty at all, return the schema with empty values
    and overall_confidence 0."""

# Rows scoring below this are flagged for a human check (kept equal to
# services.excel_export.LOW_CONFIDENCE_THRESHOLD).
LOW_CONFIDENCE = 0.80

REPAIR_PROMPT = """The following text was supposed to be a single valid JSON object matching the required manifest schema, but it is malformed. Return ONLY the corrected, valid JSON object. Do not add or invent any data that is not already present."""


def to_text(value: Any, max_len: int = 120) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:max_len]


_ADDRESS_WORDS = re.compile(
    r"\d|\b(street|st\.|road|rd\.|marg|nagar|lane|gali|plot|floor|chowk|bazaar|market|mobile)\b",
    re.I,
)


def normalize_station(value: Any) -> str:
    """Just the station/town from a header 'From'/'To' field.

    The printed From line is often "KOLKATA - 68, COTTON STREET": the town
    followed by the transporter's street address. One reading of a form may
    keep the address and the next may not, which leaves a column that says
    different things for what is the same place - so the address part is cut
    when what follows the dash looks like one (a digit or a street word).
    A leading "From -" / "To :" label is dropped too.
    """
    text = to_text(value, 60)
    text = re.sub(r"^(from|to)\s*[-:.]\s*", "", text, flags=re.I).strip()
    parts = re.split(r"\s+[-\u2013\u2014]\s+", text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and _ADDRESS_WORDS.search(parts[1]):
        text = parts[0].strip()
    return text


DISPLAY_DATE_FORMAT = "%d/%m/%Y"      # how a manifest date is shown: 05/10/2026


def parse_date(value: Any) -> Optional[dt.date]:
    """A date as written on a manifest -> a real date, or None.

    Reads DD.MM.YY, DD-MM-YYYY, DD/MM/YYYY (two-digit year = 20YY) and the
    ISO YYYY-MM-DD the extraction asks the model for. Day comes first, as on
    Indian forms."""
    text = to_text(value, 20)
    iso = re.fullmatch(r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})", text)
    if iso:
        year, month, day = (int(g) for g in iso.groups())
    else:
        match = re.fullmatch(r"(\d{1,2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{2}|\d{4})", text)
        if not match:
            return None
        day, month, raw_year = match.groups()
        day, month = int(day), int(month)
        year = int(raw_year) + 2000 if len(raw_year) == 2 else int(raw_year)
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def normalize_date(value: Any) -> str:
    """Any recognisable manifest date -> DD/MM/YYYY (05.10.26 -> 05/10/2026).
    Anything that is not recognisable as a date is returned as written - the
    sheet should show what the form shows, not a guess."""
    parsed = parse_date(value)
    return parsed.strftime(DISPLAY_DATE_FORMAT) if parsed else to_text(value, 20)


def to_number_or_blank(value: Any) -> Any:
    """Keep "Paid" / "" as-is; coerce anything that looks numeric to a float
    or int so Excel treats it as a real number (sums, right-aligns, etc.)."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return "" if value != value else value        # NaN -> blank
    text = str(value).strip()
    if text == "" or text.lower() in ("paid", "x", "-", "na", "n/a"):
        return "Paid" if text.lower() == "paid" else ""
    cleaned = text.replace(",", "").replace("₹", "")
    try:
        number = float(cleaned)
        return int(number) if number == int(number) else number
    except ValueError:
        return text


@dataclass
class ManifestRow:
    s_no: Any = ""
    c_no: str = ""
    station: str = ""
    consignee: str = ""
    weight: Any = ""
    no_of_pkg: Any = ""
    to_pay: Any = ""
    paid: Any = ""
    amount: Any = ""
    confidence: float = 0.0


@dataclass
class ParsedManifest:
    manifest_no: str = ""
    manifest_date: str = ""
    from_station: str = ""
    to_station: str = ""
    rows: List[ManifestRow] = field(default_factory=list)
    overall_confidence: float = 0.0
    provider: str = ""
    model: str = ""
    raw_json: str = ""
    warnings: List[str] = field(default_factory=list)


def _blank_if_nan(value: Any) -> Any:
    """pandas hands back NaN/None/pd.NA for empty cells (and for the cells of a
    row the user just added); treat all of them as an empty string."""
    if value is None:
        return ""
    try:
        if value != value:          # NaN, and pd.NA compares unequal to itself
            return ""
    except (TypeError, ValueError):
        pass
    return value


def rows_from_records(records) -> List["ManifestRow"]:
    """Turn the editable table's records (dicts keyed by the on-screen column
    names) back into ManifestRow objects.

    st.data_editor round-trips a mixed numbers / "Paid" / blank column as plain
    strings (240 comes back as "240"), so every cell is re-coerced the same way
    the extractor's own output was - otherwise Excel's SUM() would silently skip
    what is now text. Rows the user left completely empty are dropped.
    """
    rows: List[ManifestRow] = []
    for record in records:
        get = lambda key: _blank_if_nan(record.get(key, ""))  # noqa: E731
        row = ManifestRow(
            s_no=to_number_or_blank(get("S.No")),
            c_no=to_text(get("C.No."), 30),
            station=to_text(get("Station"), 60),
            consignee=to_text(get("Consignee"), 80),
            weight=to_number_or_blank(get("Weight")),
            no_of_pkg=to_number_or_blank(get("No. of Pkg")),
            to_pay=to_number_or_blank(get("To Pay")),
            paid=to_number_or_blank(get("Paid")),
            amount=to_number_or_blank(get("Amount")),
            confidence=float(to_number_or_blank(get("Confidence")) or 0) / 100
            if isinstance(to_number_or_blank(get("Confidence")), (int, float)) else 0.0,
        )
        if not any([row.s_no, row.c_no, row.station, row.consignee, row.weight,
                    row.no_of_pkg, row.to_pay, row.paid, row.amount]):
            continue
        rows.append(row)
    return rows


def _normalize(data: dict) -> ParsedManifest:
    rows = []
    for raw in data.get("rows") or []:
        if not isinstance(raw, dict):
            continue
        rows.append(
            ManifestRow(
                s_no=to_number_or_blank(raw.get("s_no")),
                c_no=to_text(raw.get("c_no"), 30),
                station=to_text(raw.get("station"), 60),
                consignee=to_text(raw.get("consignee"), 80),
                weight=to_number_or_blank(raw.get("weight")),
                no_of_pkg=to_number_or_blank(raw.get("no_of_pkg")),
                to_pay=to_number_or_blank(raw.get("to_pay")),
                paid=to_number_or_blank(raw.get("paid")),
                amount=to_number_or_blank(raw.get("amount")),
                confidence=float(raw.get("confidence") or 0),
            )
        )
    return ParsedManifest(
        manifest_no=to_text(data.get("manifest_no"), 40),
        manifest_date=normalize_date(data.get("manifest_date")),
        from_station=normalize_station(data.get("from_station")),
        to_station=normalize_station(data.get("to_station")),
        rows=rows,
        overall_confidence=float(data.get("overall_confidence") or 0),
    )


def loads_strict(text: str) -> dict:
    payload = find_json_object(strip_code_fences(text))
    if payload is None:
        raise ParseError("No JSON object found in the model response.")
    import json

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = json.loads(local_json_repair(payload))
    if not isinstance(data, dict):
        raise ParseError("Model response was not a JSON object.")
    return data


class ManifestParser:
    """Provider-agnostic manifest extraction with one repair retry."""

    def __init__(self, provider: OCRProvider, max_attempts: int = 2):
        if not provider.supports_structured:
            raise ParseError(MANIFEST_PROVIDER_REQUIRED)
        self.provider = provider
        self.max_attempts = max(1, max_attempts)

    def parse_images(self, images: List[Image.Image]) -> ParsedManifest:
        if not images:
            raise ParseError("No page images supplied.")

        warnings: List[str] = []
        last_error: Optional[Exception] = None
        response: Optional[OCRResponse] = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.provider.extract_structured(
                    images, EXTRACTION_PROMPT, SCHEMA_HINT
                )
                warnings.extend(response.warnings)
                data = (
                    response.structured
                    if response.structured is not None
                    else loads_strict(response.raw_response)
                )
                parsed = _normalize(data)
                parsed.provider = self.provider.name
                parsed.model = response.model
                parsed.raw_json = response.raw_response or ""
                parsed.warnings = warnings
                return parsed
            except ParseError as exc:
                last_error = exc
                warnings.append(f"Attempt {attempt}: malformed JSON — retrying.")
                log.warning("Malformed JSON from %s: %s", self.provider.name, exc)
                if response is not None and attempt < self.max_attempts:
                    fixed = self._ask_for_repair(response.raw_response)
                    if fixed is not None:
                        parsed = _normalize(fixed)
                        parsed.provider = self.provider.name
                        parsed.model = response.model
                        parsed.raw_json = response.raw_response or ""
                        parsed.warnings = warnings + ["Model output was repaired before use."]
                        return parsed
            except OCRError as exc:
                last_error = exc
                warnings.append(f"Attempt {attempt}: {exc}")
                log.warning("Provider error: %s", exc)

        raise ParseError(f"Extraction failed after {self.max_attempts} attempts: {last_error}")

    def _ask_for_repair(self, broken: str) -> Optional[dict]:
        try:
            response = self.provider.extract_structured(
                [], f"{REPAIR_PROMPT}\n\n{(broken or '')[:12000]}", SCHEMA_HINT
            )
            return response.structured or loads_strict(response.raw_response)
        except Exception as exc:  # repair is best-effort only
            log.warning("JSON repair call failed: %s", exc)
            return None
