"""
AI extraction layer.

Turns page images (or OCR text) into the strict invoice JSON contract. Nothing
downstream ever sees provider-specific output.

Safety posture: the parser never invents accounting values. A field it cannot
read is left empty/zero with a low confidence score so the review screen
forces a human decision.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from PIL import Image

from services.ocr_service import OCRError, OCRProvider, OCRResponse, get_provider
from utils.logging import get_logger

log = get_logger("parser")

SCHEMA_HINT = """Return ONLY a JSON object with exactly this shape (no markdown, no commentary):

{
  "supplier": {"name": "", "gstin": "", "address": ""},
  "invoice": {"invoice_number": "", "invoice_date": "", "due_date": ""},
  "items": [
    {
      "raw_name": "", "normalized_name": "", "hsn": "", "barcode": "",
      "quantity": 0, "unit": "", "rate": 0, "discount": 0,
      "taxable_value": 0, "gst_rate": 0, "cgst": 0, "sgst": 0,
      "igst": 0, "cess": 0, "total": 0, "confidence": 0
    }
  ],
  "subtotal": 0, "discount_total": 0, "tax_total": 0,
  "cgst_total": 0, "sgst_total": 0, "igst_total": 0, "cess_total": 0,
  "round_off": 0, "grand_total": 0, "overall_confidence": 0
}"""

EXTRACTION_PROMPT = """You are an expert accounts-payable data entry operator reading an Indian GST purchase invoice.

Extract the invoice exactly as printed. Rules:
1. NEVER guess. If a value is not printed or not legible, use "" for text and 0 for numbers, and lower that field's confidence.
2. Do not calculate values that are not on the bill. Copy the printed figures. Small OCR-level arithmetic checks are done later by the software.
3. Dates must be ISO format YYYY-MM-DD. Indian bills are usually DD/MM/YYYY — convert accordingly.
4. Numbers must be plain (1234.50), with no currency symbols, commas or percent signs.
5. gst_rate is the percentage (for example 18), not the amount.
6. If the bill shows a single combined GST amount per line, split it into cgst/sgst only when the bill itself shows CGST and SGST separately. Otherwise put the amount in the field that is actually printed. If tax amounts are printed only in the totals section (for example an 'IGST 1,085.76' line under the items) while the lines show just a GST %, copy those printed amounts into cgst_total / sgst_total / igst_total and leave the per-line cgst/sgst/igst at 0 — do not work them out yourself; the software does that and checks it against these totals.
7. raw_name is the product description exactly as printed. normalized_name is a cleaned-up version (expand obvious abbreviations, standardise pack size, keep brand).
8. barcode: only fill it if an EAN/UPC/GTIN number is printed on the line.
9. confidence is 0.0-1.0 per line: how sure you are that every number on that line was read correctly.
10. overall_confidence reflects image legibility and how completely the invoice was captured.
11. If several pages are supplied they belong to ONE invoice; merge the line items in order.
12. If the image is not a purchase invoice at all, return the schema with empty values and overall_confidence 0."""

REPAIR_PROMPT = """The following text was supposed to be a single valid JSON object matching the required invoice schema, but it is malformed. Return ONLY the corrected, valid JSON object. Do not add or invent any data that is not already present."""

# Sales mirrors the purchase prompt exactly, with the party roles reversed:
# on a sales invoice, WE are the seller (already known — the company's own
# details are not extracted), so the "supplier" JSON field is repurposed here
# to hold the CUSTOMER/buyer's details instead. Same schema, same downstream
# code (normalize_invoice, InvoiceValidator, ProductMatcher) — only the
# instructions to the model change.
SALES_EXTRACTION_PROMPT = """You are an expert accounts-receivable data entry operator reading an Indian GST sales invoice issued by our own company to a customer.

Extract the invoice exactly as printed. Rules:
1. NEVER guess. If a value is not printed or not legible, use "" for text and 0 for numbers, and lower that field's confidence.
2. Put the CUSTOMER's (buyer's) name, GSTIN and address in the "supplier" field of the schema below — that field means "the other party on this bill", which for a sales invoice is the customer, not us. Do not extract our own company's details.
3. Do not calculate values that are not on the bill. Copy the printed figures. Small OCR-level arithmetic checks are done later by the software.
4. Dates must be ISO format YYYY-MM-DD. Indian bills are usually DD/MM/YYYY — convert accordingly.
5. Numbers must be plain (1234.50), with no currency symbols, commas or percent signs.
6. gst_rate is the percentage (for example 18), not the amount.
7. If the bill shows a single combined GST amount per line, split it into cgst/sgst only when the bill itself shows CGST and SGST separately. Otherwise put the amount in the field that is actually printed. If tax amounts are printed only in the totals section (for example an 'IGST 1,085.76' line under the items) while the lines show just a GST %, copy those printed amounts into cgst_total / sgst_total / igst_total and leave the per-line cgst/sgst/igst at 0 — do not work them out yourself; the software does that and checks it against these totals.
8. raw_name is the product description exactly as printed. normalized_name is a cleaned-up version (expand obvious abbreviations, standardise pack size, keep brand).
9. barcode: only fill it if an EAN/UPC/GTIN number is printed on the line.
10. confidence is 0.0-1.0 per line: how sure you are that every number on that line was read correctly.
11. overall_confidence reflects image legibility and how completely the invoice was captured.
12. If several pages are supplied they belong to ONE invoice; merge the line items in order.
13. If the image is not a sales invoice at all, return the schema with empty values and overall_confidence 0."""

NUMERIC_ITEM_FIELDS = (
    "quantity", "rate", "discount", "taxable_value", "gst_rate",
    "cgst", "sgst", "igst", "cess", "total", "confidence",
)
NUMERIC_TOTAL_FIELDS = (
    "subtotal", "discount_total", "tax_total", "cess_total",
    "cgst_total", "sgst_total", "igst_total",
    "round_off", "grand_total", "overall_confidence",
)

UNIT_ALIASES = {
    "pc": "PCS", "pcs": "PCS", "piece": "PCS", "pieces": "PCS", "nos": "PCS",
    "no": "PCS", "unit": "PCS", "units": "PCS", "ea": "PCS",
    "box": "BOX", "boxes": "BOX", "ctn": "CTN", "carton": "CTN", "case": "CTN",
    "kg": "KG", "kgs": "KG", "kilogram": "KG", "gm": "GM", "g": "GM",
    "gram": "GM", "grams": "GM", "ltr": "LTR", "ltrs": "LTR", "l": "LTR",
    "litre": "LTR", "liter": "LTR", "ml": "ML", "bag": "BAG", "bags": "BAG",
    "pkt": "PKT", "packet": "PKT", "pack": "PKT", "dz": "DOZ", "doz": "DOZ",
    "dozen": "DOZ", "bdl": "BDL", "bundle": "BDL", "mtr": "MTR", "meter": "MTR",
}


class ParseError(RuntimeError):
    pass


@dataclass
class ParsedInvoice:
    data: Dict[str, Any]
    provider: str = ""
    model: str = ""
    raw_text: str = ""
    raw_json: str = ""
    repaired: bool = False
    attempts: int = 1
    duration_ms: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def items(self) -> List[dict]:
        return self.data.get("items", [])

    @property
    def overall_confidence(self) -> float:
        return float(self.data.get("overall_confidence") or 0.0)


# --------------------------------------------------------------------------
# Coercion helpers
# --------------------------------------------------------------------------
def to_number(value: Any, default: float = 0.0) -> float:
    """'₹ 1,234.50' -> 1234.5 ; '18 %' -> 18.0 ; '(120)' -> -120.0"""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace(",", "")
    # Pull out the first well-formed number rather than stripping characters
    # blindly — "Rs. 99.99/-" must give 99.99, not fail on leftover punctuation.
    match = re.search(r"-?\d+(?:\.\d+)?|-?\.\d+", text)
    if not match:
        return default
    try:
        number = float(match.group(0))
    except ValueError:
        return default
    return -number if negative and number > 0 else number


def to_text(value: Any, max_len: int = 250) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:max_len]


def normalize_unit(value: Any) -> str:
    raw = to_text(value, 20).lower().strip(". ")
    if not raw:
        return ""
    return UNIT_ALIASES.get(raw, raw.upper()[:10])


DATE_PATTERNS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y",
    "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%b %d, %Y",
)


def normalize_date(value: Any) -> str:
    text = to_text(value, 40)
    if not text:
        return ""
    text = text.replace(",", ", ").strip()
    for pattern in DATE_PATTERNS:
        try:
            parsed = dt.datetime.strptime(text, pattern)
            if parsed.year < 100:
                parsed = parsed.replace(year=2000 + parsed.year)
            return parsed.date().isoformat()
        except ValueError:
            continue
    match = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        year = year + 2000 if year < 100 else year
        if month > 12 and day <= 12:      # printed as MM/DD
            day, month = month, day
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            return ""
    return ""


GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]\b")


def normalize_gstin(value: Any) -> str:
    # Search the full string first; truncating to 15 before searching would cut
    # a valid GSTIN in half whenever the bill prints a "GSTIN:" prefix.
    text = to_text(value, 120).upper().replace(" ", "")
    match = GSTIN_RE.search(text)
    if match:
        return match.group(0)
    return re.sub(r"[^A-Z0-9]", "", text)[:15]


# --------------------------------------------------------------------------
# JSON extraction / repair
# --------------------------------------------------------------------------
def strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def find_json_object(text: str) -> Optional[str]:
    """Return the first balanced {...} block, ignoring braces inside strings."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def local_json_repair(text: str) -> str:
    """Cheap deterministic fixes before spending another API call."""
    fixed = text
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)          # trailing commas
    fixed = re.sub(r"[\u2018\u2019\u201c\u201d]", '"', fixed)  # smart quotes
    fixed = re.sub(r"\bNaN\b|\bInfinity\b", "0", fixed)
    fixed = re.sub(r"\bNone\b", "null", fixed)
    fixed = re.sub(r"\bTrue\b", "true", fixed)
    fixed = re.sub(r"\bFalse\b", "false", fixed)
    fixed = re.sub(r"(?<=[:,\[\s])\.(\d)", r"0.\1", fixed)  # .5 -> 0.5
    fixed = re.sub(r'(\d)\s*"\s*(?=[,}])', r"\1", fixed)
    return fixed


def loads_strict(text: str) -> dict:
    payload = find_json_object(strip_code_fences(text))
    if payload is None:
        raise ParseError("No JSON object found in the model response.")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = json.loads(local_json_repair(payload))
    if not isinstance(data, dict):
        raise ParseError("Model response was not a JSON object.")
    return data


# --------------------------------------------------------------------------
# Schema normalisation
# --------------------------------------------------------------------------
def empty_invoice() -> dict:
    return {
        "supplier": {"name": "", "gstin": "", "address": ""},
        "invoice": {"invoice_number": "", "invoice_date": "", "due_date": ""},
        "items": [],
        "subtotal": 0.0,
        "discount_total": 0.0,
        "tax_total": 0.0,
        "cgst_total": 0.0,
        "sgst_total": 0.0,
        "igst_total": 0.0,
        "cess_total": 0.0,
        "round_off": 0.0,
        "grand_total": 0.0,
        "overall_confidence": 0.0,
    }


def _spread(total: float, weights: List[float]) -> List[float]:
    """Split `total` across lines in proportion to `weights`; the last line
    takes the remainder so the pieces add back to the printed total exactly."""
    weight_sum = sum(weights)
    if weight_sum <= 0:
        return [0.0] * len(weights)
    shares = [round(total * w / weight_sum, 2) for w in weights[:-1]]
    shares.append(round(total - sum(shares), 2))
    return shares


def _apply_summary_taxes(out: dict) -> List[str]:
    """Fill per-line CGST/SGST/IGST when the bill prints tax only in its totals.

    Many Indian bills show just a GST % on each line and put the tax amounts
    (e.g. "IGST 1,085.76") in the totals block. The rest of the app — the
    validator and the Tally voucher — works from line-level tax, so without
    this the tax is lost and the invoice total never reconciles.

    This is deliberately conservative, in keeping with the rule that nothing
    on a bill is silently altered:
      * only runs when NO line carries a tax amount and the bill printed a
        CGST/SGST/IGST summary;
      * only accepts it when taxable value x GST % per line adds up to the
        printed summary (within a small tolerance) — i.e. the bill agrees
        with itself;
      * spreads the PRINTED amount, not a recalculated one, so the lines add
        back to exactly what the bill says;
      * returns a note so the reviewer can see it happened.
    If the numbers don't agree, nothing is touched and the normal mismatch
    warnings stay visible for a person to resolve.
    """
    items = out["items"]
    if not items or any(i["cgst"] or i["sgst"] or i["igst"] for i in items):
        return []
    cgst_t, sgst_t, igst_t = out["cgst_total"], out["sgst_total"], out["igst_total"]
    printed = round(cgst_t + sgst_t + igst_t, 2)
    if printed <= 0 or (igst_t > 0 and (cgst_t > 0 or sgst_t > 0)):
        return []

    bases, expected = [], []
    discount_fixed = 0
    for item in items:
        qty_rate = round(item["quantity"] * item["rate"], 2)
        net = round(qty_rate - item["discount"], 2)
        if item["discount"] > 0 and abs(net - qty_rate) > 0.01:
            # Some bills print a line's "Amount" as qty x rate, BEFORE its
            # own Disc column, and the extraction copies that printed
            # figure straight into taxable_value/total (it was told to
            # copy printed numbers, not calculate) — correct as a
            # transcription, but taxable_value/total are what the rest of
            # the app treats as post-discount, so left alone this both
            # fails the taxable-value check and throws off this
            # reconciliation for every line, not just the discounted one.
            # Correct only this clear case: the printed figure equals
            # qty*rate exactly, with the discount printed separately.
            if item["taxable_value"] and abs(item["taxable_value"] - qty_rate) <= 0.01:
                item["taxable_value"] = net
                discount_fixed += 1
            if item["total"] and abs(item["total"] - qty_rate) <= 0.01:
                item["total"] = 0.0  # rebuilt below from the corrected taxable_value + tax
        base = item["taxable_value"] or net
        bases.append(base)
        expected.append(round(base * item["gst_rate"] / 100.0, 2) if base > 0 else 0.0)
    if sum(1 for e in expected if e > 0) != len(items):
        return []          # a line with no rate/value: can't say the bill agrees with itself
    tolerance = max(1.0, 0.02 * len(items))
    if abs(sum(expected) - printed) > tolerance:
        return []

    heads = {"cgst": cgst_t, "sgst": sgst_t, "igst": igst_t}
    for head, amount in heads.items():
        if amount > 0:
            for item, share in zip(items, _spread(amount, expected)):
                item[head] = share
    for item, base in zip(items, bases):
        item["taxable_value"] = item["taxable_value"] or base
        line_tax = item["cgst"] + item["sgst"] + item["igst"] + item["cess"]
        # A bill that shows only tax in the summary prints the pre-tax figure as
        # each line's "Amount", so a line total equal to the taxable value (or
        # missing) is completed to taxable + tax. Any other printed total is kept.
        if item["total"] == 0 or abs(item["total"] - item["taxable_value"]) <= 0.01:
            item["total"] = round(item["taxable_value"] + line_tax, 2)

    label = " + ".join(f"{h.upper()} ₹{a:,.2f}" for h, a in heads.items() if a > 0)
    notes = [
        f"Auto-filled: this bill prints tax only in its totals ({label}), so it was "
        f"spread across the lines by taxable value × GST %. It matches the line "
        f"amounts — please confirm against the bill."
    ]
    if discount_fixed:
        notes.append(
            f"Auto-corrected: {discount_fixed} line(s) had their pre-discount "
            f"amount in taxable value/total — adjusted for the printed "
            f"discount so it matches the bill's own totals. Please confirm."
        )
    return notes


def normalize_invoice(data: dict) -> dict:
    """Coerce any provider output into the exact contract, without inventing data."""
    out = empty_invoice()
    supplier = data.get("supplier") or {}
    if isinstance(supplier, str):
        supplier = {"name": supplier}
    out["supplier"]["name"] = to_text(supplier.get("name"), 200)
    out["supplier"]["gstin"] = normalize_gstin(supplier.get("gstin"))
    out["supplier"]["address"] = to_text(supplier.get("address"), 500)

    invoice = data.get("invoice") or {}
    if isinstance(invoice, str):
        invoice = {"invoice_number": invoice}
    out["invoice"]["invoice_number"] = to_text(invoice.get("invoice_number"), 80)
    out["invoice"]["invoice_date"] = normalize_date(invoice.get("invoice_date"))
    out["invoice"]["due_date"] = normalize_date(invoice.get("due_date"))

    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    items = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        item = {
            "raw_name": to_text(entry.get("raw_name") or entry.get("name")),
            "normalized_name": to_text(
                entry.get("normalized_name") or entry.get("raw_name") or entry.get("name")
            ),
            "hsn": to_text(entry.get("hsn") or entry.get("hsn_sac"), 20),
            "barcode": re.sub(r"\D", "", to_text(entry.get("barcode"), 30)),
            "unit": normalize_unit(entry.get("unit")),
        }
        for key in NUMERIC_ITEM_FIELDS:
            item[key] = to_number(entry.get(key))
        if item["confidence"] > 1:          # model returned a percentage
            item["confidence"] = min(item["confidence"] / 100.0, 1.0)
        if item["gst_rate"] > 100:
            item["gst_rate"] = 0.0
        if not item["raw_name"] and item["quantity"] == 0 and item["total"] == 0:
            continue
        items.append(item)
    out["items"] = items

    for key in NUMERIC_TOTAL_FIELDS:
        out[key] = to_number(data.get(key))
    if out["overall_confidence"] > 1:
        out["overall_confidence"] = min(out["overall_confidence"] / 100.0, 1.0)
    if out["overall_confidence"] == 0 and items:
        scores = [i["confidence"] for i in items if i["confidence"] > 0]
        out["overall_confidence"] = round(sum(scores) / len(scores), 3) if scores else 0.0
    notes = _apply_summary_taxes(out)
    if notes:
        out["extraction_notes"] = notes
    if out["tax_total"] == 0:
        out["tax_total"] = round(out["cgst_total"] + out["sgst_total"] + out["igst_total"], 2)
    if out["tax_total"] == 0 and items:
        out["tax_total"] = round(
            sum(i["cgst"] + i["sgst"] + i["igst"] for i in items), 2
        )
    if out["cess_total"] == 0 and items:
        out["cess_total"] = round(sum(i["cess"] for i in items), 2)
    if out["subtotal"] == 0 and items:
        out["subtotal"] = round(sum(i["taxable_value"] for i in items), 2)
    return out


# --------------------------------------------------------------------------
# Heuristic fallback for text-only OCR engines
# --------------------------------------------------------------------------
LINE_RE = re.compile(
    r"^(?P<name>.+?)\s+"
    r"(?:(?P<hsn>\d{4,8})\s+)?"                 # a GST invoice very often prints HSN/SAC before Qty
    r"(?P<qty>\d+(?:\.\d+)?)\s+(?:rs\.?|inr|₹)?\s*(?P<rate>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s+(?P<disc>\d[\d,]*(?:\.\d+)?))?"
    r"(?:\s+(?P<gst>\d{1,2}(?:\.\d)?)\s*%?)?"   # the % sign is often only in the column header, not every row
    r"\s+(?:rs\.?|inr|₹)?\s*(?P<total>\d[\d,]*(?:\.\d+)?)\s*$",
    re.I,
)
PAGE_MARKER_RE = re.compile(r"^-{2,}\s*PAGE\s+\d+\s*-{2,}$", re.I)
INV_NO_RE = re.compile(
    r"(?:invoice|inv|bill)\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Za-z0-9/\-]{3,30})",
    re.I,
)
DATE_LINE_RE = re.compile(
    r"(?:invoice\s*date|bill\s*date|date)\s*[:\-]?\s*([\d]{1,4}[-/.][\d]{1,2}[-/.][\d]{2,4})",
    re.I,
)
TOTAL_RE = re.compile(
    r"(?:grand\s*total|net\s*amount|total\s*amount|invoice\s*total|bill\s*total)"
    r"\s*[:\-]?\s*(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
    re.I,
)
GST_PCT_RE = re.compile(r"\b(\d{1,2}(?:\.\d)?)\s*%")
_NUMERIC_TOKEN_RE = re.compile(r"^[\d,]+(?:\.\d+)?%?$")
# Tesseract (and sometimes RapidOCR) occasionally reads a 5-digit amount like
# "10000.00" as "10000 .00" or "10000. 00" - a stray space/tab right next to
# the decimal point, most often on a Taxable-value column. That space breaks
# the amount into two tokens, so the line no longer matches as a number and
# gets silently dropped (correctly - a torn-apart amount must not be guessed
# at), losing an otherwise perfectly good item. Rejoin it before parsing:
# only a digit/dot/digit pattern with nothing but [ \t] around the dot
# qualifies, so a genuine new sentence or column is never merged by mistake.
_SPLIT_DECIMAL_RE = re.compile(r"(\d)[ \t]*\.[ \t]*(\d{2}\b)")


def _split_tax_columns_line(line: str) -> Optional[dict]:
    """Parse a line from the OTHER very common Indian GST bill layout: one
    where CGST/SGST (or IGST) are printed as their OWN %-and-amount columns,
    alongside a separate Taxable-value column - e.g.

        Basmati Rice 25kg  10063020  6  1450.00  8700.00  2.5  217.50  2.5  217.50  9135.00
        Item               HSN       Qty Rate    Taxable  CGST% CGST   SGST% SGST  Total

    LINE_RE alone cannot read this: it only has two optional slots (discount,
    a single gst%) between rate and total, but this layout has five fields
    (taxable, cgst%, cgst, sgst%, sgst) in between. Even with perfectly clean,
    well-separated OCR text, LINE_RE either fails to match or (worse) matches
    by stuffing most of the row into "name" and picking the wrong numbers for
    qty/rate/total - caught by the arithmetic check in parse_text_heuristic,
    but that just silently drops a perfectly good line.

    This instead counts the trailing run of numeric-looking tokens and uses
    that COUNT to resolve which column layout it is (no backtracking
    ambiguity): 6/7 trailing numbers = IGST (without/with a discount column),
    8/9 = CGST+SGST (without/with a discount column). An optional leading
    4-8 digit whole number in that run is the HSN code.
    """
    tokens = line.split()
    i = len(tokens)
    while i > 0 and _NUMERIC_TOKEN_RE.match(tokens[i - 1]):
        i -= 1
    name_tokens, numbers = tokens[:i], tokens[i:]
    if not name_tokens or not numbers:
        return None

    hsn = None
    if re.fullmatch(r"\d{4,8}", numbers[0]) and len(numbers) - 1 in (6, 7, 8, 9):
        hsn, numbers = numbers[0], numbers[1:]

    shape = {
        6: ("qty", "rate", "taxable", "igst_pct", "igst_amt", "total"),
        7: ("qty", "rate", "disc", "taxable", "igst_pct", "igst_amt", "total"),
        8: ("qty", "rate", "taxable", "cgst_pct", "cgst_amt", "sgst_pct", "sgst_amt", "total"),
        9: ("qty", "rate", "disc", "taxable", "cgst_pct", "cgst_amt", "sgst_pct", "sgst_amt", "total"),
    }.get(len(numbers))
    if shape is None:
        return None
    fields = dict(zip(shape, numbers))

    name = " ".join(name_tokens).strip(" .:-|")
    if len(name) < 3 or name.isdigit():
        return None
    quantity = to_number(fields["qty"])
    rate = to_number(fields["rate"])
    discount = to_number(fields.get("disc")) if "disc" in fields else 0.0
    taxable = to_number(fields["taxable"])
    total = to_number(fields["total"])
    if quantity <= 0 or rate <= 0:
        return None

    # Same principle as the arithmetic check in parse_text_heuristic: the
    # printed taxable value must be ~qty*rate (minus discount), and the
    # printed total must be ~taxable + whatever tax amounts are on the line -
    # otherwise a column got misread/glued and this line must be dropped, not
    # guessed at.
    expected_taxable = round(quantity * rate - discount, 2)
    if abs(expected_taxable - taxable) > max(2.0, 0.1 * max(taxable, expected_taxable)):
        return None
    if "igst_amt" in fields:
        tax_amount = to_number(fields["igst_amt"])
        gst_rate = to_number(fields["igst_pct"])
    else:
        tax_amount = to_number(fields["cgst_amt"]) + to_number(fields["sgst_amt"])
        gst_rate = to_number(fields["cgst_pct"]) + to_number(fields["sgst_pct"])
    expected_total = round(taxable + tax_amount, 2)
    if abs(expected_total - total) > max(2.0, 0.1 * max(total, expected_total)):
        return None

    return {
        "name": name[:250],
        "hsn": hsn or "",
        "quantity": quantity,
        "rate": rate,
        "discount": discount,
        "taxable_value": taxable,
        "gst_rate": gst_rate,
        "cgst": to_number(fields.get("cgst_amt")),
        "sgst": to_number(fields.get("sgst_amt")),
        "igst": to_number(fields.get("igst_amt")),
        "total": total,
    }


def parse_text_heuristic(text: str) -> dict:
    """
    Deterministic best-effort parse of raw OCR text.

    Deliberately conservative: every line gets a low confidence so the review
    screen treats the whole invoice as mandatory-review.
    """
    data = empty_invoice()
    lines = [
        _SPLIT_DECIMAL_RE.sub(r"\1.\2", line.strip())
        for line in (text or "").splitlines()
        if line.strip()
    ]

    joined = "\n".join(lines)
    match = INV_NO_RE.search(joined)
    if match:
        data["invoice"]["invoice_number"] = match.group(1)
    match = DATE_LINE_RE.search(joined)
    if match:
        data["invoice"]["invoice_date"] = normalize_date(match.group(1))
    match = GSTIN_RE.search(joined.upper())
    if match:
        data["supplier"]["gstin"] = match.group(0)
    for line in lines[:6]:
        if PAGE_MARKER_RE.match(line.strip()):
            continue  # "--- PAGE 1 ---" (inserted by text-only OCR engines), not the bill
        cleaned = re.sub(r"[^A-Za-z0-9 &.\-]", "", line).strip()
        if len(cleaned) > 4 and not cleaned.lower().startswith(("tax", "invoice", "gst")):
            data["supplier"]["name"] = cleaned[:200]
            break

    totals = TOTAL_RE.findall(joined)
    if totals:
        data["grand_total"] = to_number(totals[-1])

    # Tax amounts printed in the totals block ("IGST: 1,085.76"). The last
    # matching line wins; the amount on it is its last two-decimal number.
    # _apply_summary_taxes only uses these if the lines agree with them.
    for head in ("cgst", "sgst", "igst"):
        for line in lines:
            if re.search(rf"\b{head}\b", line, re.I):
                amounts = re.findall(r"\d[\d,]*\.\d{2}", line)
                if amounts:
                    data[f"{head}_total"] = to_number(amounts[-1])

    for line in lines:
        if re.search(r"total|gst|cgst|sgst|igst|subtotal|amount in words", line, re.I):
            continue

        # Try the richer "separate Taxable + CGST%/CGST/SGST%/SGST (or IGST)
        # columns" layout first - see _split_tax_columns_line's docstring.
        # It is more specific (it demands an exact trailing column count), so
        # trying it first avoids LINE_RE's simpler pattern grabbing a plausible
        # but wrong subset of a richer row before we get a chance to read it
        # properly.
        split = _split_tax_columns_line(line)
        if split:
            data["items"].append(
                {
                    "raw_name": split["name"],
                    "normalized_name": split["name"],
                    "hsn": split["hsn"],
                    "barcode": "",
                    "quantity": split["quantity"],
                    "unit": "",
                    "rate": split["rate"],
                    "discount": split["discount"],
                    "taxable_value": split["taxable_value"],
                    "gst_rate": split["gst_rate"],
                    "cgst": split["cgst"],
                    "sgst": split["sgst"],
                    "igst": split["igst"],
                    "cess": 0.0,
                    "total": split["total"],
                    "confidence": 0.45,      # never trusted automatically
                }
            )
            continue

        found = LINE_RE.match(line)
        if not found:
            continue
        name = found.group("name").strip(" .:-|")
        if len(name) < 3 or name.isdigit():
            continue
        quantity = to_number(found.group("qty"))
        rate = to_number(found.group("rate"))
        total = to_number(found.group("total"))
        discount = to_number(found.group("disc")) if found.group("disc") else 0.0
        if quantity <= 0 or rate <= 0:
            continue
        # A dense GST bill (separate Taxable/CGST%/CGST/SGST%/SGST columns packed
        # tightly together) can make a text-only OCR engine glue two adjacent
        # columns into one token (e.g. HSN "10063020" + Qty "6" read as one
        # "100630206", or Rate "1450.00" + Taxable "8700.00" read as
        # "1450.008700.00"). LINE_RE then still "matches" by picking the wrong
        # numbers for qty/rate, producing a confidently-wrong item instead of no
        # item. Catch that here: on a real line, total is always qty*rate minus
        # discount, plus GST on top (at most ~28% GST + a little cess) - so a
        # much bigger mismatch means the columns were glued, not that GST was
        # added. Skip the line rather than post a wrong figure; the bill then
        # correctly falls to "No line items were extracted" / mandatory review.
        expected = round(quantity * rate - discount, 2)
        if total > 0 and abs(expected - total) > max(2.0, 0.35 * total):
            continue
        gst_match = GST_PCT_RE.search(line)
        gst_rate = to_number(gst_match.group(1)) if gst_match else (
            to_number(found.group("gst")) if found.group("gst") else 0.0
        )
        data["items"].append(
            {
                "raw_name": name[:250],
                "normalized_name": name[:250],
                "hsn": found.group("hsn") or "",
                "barcode": "",
                "quantity": quantity,
                "unit": "",
                "rate": rate,
                "discount": discount,
                "taxable_value": round(quantity * rate - discount, 2),
                "gst_rate": gst_rate,
                "cgst": 0.0,
                "sgst": 0.0,
                "igst": 0.0,
                "cess": 0.0,
                "total": total,
                "confidence": 0.45,          # never trusted automatically
            }
        )
    data["overall_confidence"] = 0.45 if data["items"] else 0.1
    return data


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------
class InvoiceParser:
    """Provider-agnostic invoice extraction with retry + repair."""

    def __init__(self, provider: Optional[OCRProvider] = None, max_attempts: int = 2):
        self.provider = provider or get_provider()
        self.max_attempts = max(1, max_attempts)

    def parse_images(
        self, images: List[Image.Image], document_type: str = "purchase"
    ) -> ParsedInvoice:
        if not images:
            raise ParseError("No page images supplied.")
        if self.provider.supports_structured:
            return self._parse_structured(images, document_type)
        return self._parse_via_text(images)

    # -- structured (vision LLM) path ------------------------------------
    def _parse_structured(
        self, images: List[Image.Image], document_type: str = "purchase"
    ) -> ParsedInvoice:
        prompt = (
            SALES_EXTRACTION_PROMPT if document_type == "sales" else EXTRACTION_PROMPT
        )
        warnings: List[str] = []
        last_error: Optional[Exception] = None
        response: Optional[OCRResponse] = None
        repaired = False

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.provider.extract_structured(
                    images, prompt, SCHEMA_HINT
                )
                warnings.extend(response.warnings)
                if response.structured is not None:
                    data = normalize_invoice(response.structured)
                    return ParsedInvoice(
                        data=data,
                        provider=self.provider.name,
                        model=response.model,
                        raw_json=response.raw_response
                        or json.dumps(response.structured),
                        attempts=attempt,
                        duration_ms=response.duration_ms,
                        warnings=warnings,
                    )
                data = loads_strict(response.raw_response)
                return ParsedInvoice(
                    data=normalize_invoice(data),
                    provider=self.provider.name,
                    model=response.model,
                    raw_json=response.raw_response,
                    repaired=repaired,
                    attempts=attempt,
                    duration_ms=response.duration_ms,
                    warnings=warnings,
                )
            except ParseError as exc:
                last_error = exc
                repaired = True
                warnings.append(f"Attempt {attempt}: malformed JSON — retrying.")
                log.warning("Malformed JSON from %s: %s", self.provider.name, exc)
                if response is not None and attempt < self.max_attempts:
                    fixed = self._ask_for_repair(response.raw_response)
                    if fixed is not None:
                        return ParsedInvoice(
                            data=normalize_invoice(fixed),
                            provider=self.provider.name,
                            model=response.model,
                            raw_json=response.raw_response,
                            repaired=True,
                            attempts=attempt + 1,
                            duration_ms=response.duration_ms,
                            warnings=warnings
                            + ["Model output was repaired before use."],
                        )
            except OCRError as exc:
                last_error = exc
                warnings.append(f"Attempt {attempt}: {exc}")
                log.warning("Provider error: %s", exc)

        raise ParseError(
            f"Extraction failed after {self.max_attempts} attempts: {last_error}"
        )

    def _ask_for_repair(self, broken: str) -> Optional[dict]:
        try:
            response = self.provider.extract_structured(
                [], f"{REPAIR_PROMPT}\n\n{broken[:12000]}", SCHEMA_HINT
            )
            payload = response.structured or loads_strict(response.raw_response)
            return payload
        except Exception as exc:  # repair is best-effort only
            log.warning("JSON repair call failed: %s", exc)
            return None

    # -- text OCR path ---------------------------------------------------
    def _parse_via_text(self, images: List[Image.Image]) -> ParsedInvoice:
        response = self.provider.extract_text(images)
        data = parse_text_heuristic(response.text)
        warnings = list(response.warnings)
        warnings.append(
            "Text-only OCR engine: fields were parsed with rule-based logic. "
            "Every line requires manual verification."
        )
        if not data["items"]:
            warnings.append("No line items could be identified from the OCR text.")
        return ParsedInvoice(
            data=normalize_invoice(data),
            provider=self.provider.name,
            model=response.model,
            raw_text=response.text,
            attempts=1,
            duration_ms=response.duration_ms,
            warnings=warnings,
        )
