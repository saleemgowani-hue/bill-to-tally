"""
Turns PP-StructureV3's output into the same invoice dict shape every other
engine produces (see ai_invoice_parser.empty_invoice) — no changes needed
anywhere downstream (normalize_invoice, InvoiceValidator, ProductMatcher,
Tally XML) to support this engine.

Kept separate from services/ocr_service.py and free of any paddleocr import,
so it can be unit-tested (and read) without the SDK installed, and so a
version-to-version change in PaddleOCR's SDK only touches
_extract_page_content in ocr_service.py, not this file's parsing/mapping
logic.

PP-StructureV3 gives two things per page that matter here:
  1. A markdown rendering of the page for the header fields (supplier/
     GSTIN/invoice no/date/totals), via ai_invoice_parser.parse_text_heuristic
     on the plain-text portion (the same logic already proven on plain OCR
     text).
  2. Real detected tables — parsed here into line items, using a
     column-header alias map (a supplier's "Qty"/"Quantity"/"Qty." all mean
     the same thing) rather than assuming one fixed column order/naming,
     per the brief's requirement to handle varying invoice layouts.
     PaddleX's table-recognition module returns a table as HTML
     (`pred_html`, raw `<table>/<tr>/<td>` markup — confirmed via
     PaddleX's own table-recognition docs), and `save_to_markdown()`
     embeds that HTML as-is in the .md file rather than converting it to
     pipe syntax. So both forms are parsed here: `parse_html_tables` (the
     one PP-StructureV3 actually produces) and `parse_markdown_tables`
     (plain `| a | b |` pipe tables, kept for any other engine/export that
     emits real GFM markdown).

If no table is detected, item extraction falls back to the same line-regex
heuristic used for plain-text OCR engines (LINE_RE) — free extra coverage
for a badly-scanned or unusually laid out bill.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

from services.ai_invoice_parser import (
    GST_PCT_RE,
    empty_invoice,
    parse_text_heuristic,
    to_number,
    to_text,
)

# Canonical item field -> accepted column-header spellings (lower/space-normalized).
# Mirrors the approach in services/excel_import.py's COLUMN_ALIASES — different
# suppliers name their columns differently; a fixed layout would break on the
# next bill.
COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "name": ("item", "item name", "product", "product name", "description",
              "particulars", "goods", "goods description"),
    "hsn": ("hsn", "hsn code", "hsn/sac", "hsn sac", "sac"),
    "quantity": ("qty", "qty.", "quantity", "no of units", "nos"),
    "unit": ("unit", "uom", "units"),
    "rate": ("rate", "price", "unit price", "unit rate", "rate per unit"),
    "discount": ("discount", "disc", "disc.", "discount amount"),
    "taxable_value": ("taxable value", "taxable amt", "taxable amount"),
    "gst_rate": ("gst", "gst%", "gst %", "gst rate", "tax%", "tax %", "tax rate"),
    "cgst": ("cgst", "cgst amt", "cgst amount"),
    "sgst": ("sgst", "sgst amt", "sgst amount"),
    "igst": ("igst", "igst amt", "igst amount"),
    "cess": ("cess", "cess amt", "cess amount"),
    "total": ("amount", "total", "net amount", "value", "line total"),
}


def _normalize_header(text: str) -> str:
    text = str(text or "").strip().lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def map_columns(headers: List[str]) -> Dict[str, int]:
    """canonical field -> column index, best-effort, for one detected table."""
    normalized = [_normalize_header(h) for h in headers]
    mapping: Dict[str, int] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for index, header in enumerate(normalized):
            if header in aliases or header == field.replace("_", " "):
                mapping[field] = index
                break
    return mapping


def parse_markdown_tables(markdown: str) -> List[List[List[str]]]:
    """Extract every markdown table in `markdown` as a list of tables, each a
    list of rows, each row a list of cell strings (header row included)."""
    tables: List[List[List[str]]] = []
    lines = (markdown or "").splitlines()
    current: List[List[str]] = []

    def _flush():
        if len(current) >= 2:  # header + at least one data row
            tables.append(list(current))
        current.clear()

    separator_re = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
    for line in lines:
        stripped = line.strip()
        if "|" not in stripped:
            _flush()
            continue
        if separator_re.match(stripped):
            continue  # the "| --- | --- |" header/body divider
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        current.append(cells)
    _flush()
    return tables


class _HTMLTableParser(HTMLParser):
    """Collects <table>/<tr>/<td|th> text into rows of cell strings. A
    colspan repeats that cell's text across the columns it covers (a
    reasonable approximation so later columns don't shift left); rowspan
    is not expanded — the spanned cell just doesn't repeat on later rows,
    a known limitation documented in table_to_items' caller.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[str]]] = []
        self._in_table = 0
        self._row: Optional[List[str]] = None
        self._rows: List[List[str]] = []
        self._cell_chunks: List[str] = []
        self._in_cell = False
        self._colspan = 1

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self._in_table += 1
            if self._in_table == 1:
                self._rows = []
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._in_table:
            self._in_cell = True
            self._cell_chunks = []
            self._colspan = 1
            for name, value in attrs:
                if name == "colspan" and value and value.strip().isdigit():
                    self._colspan = max(1, int(value.strip()))
        elif tag == "br" and self._in_cell:
            self._cell_chunks.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self._in_table and self._in_cell:
            text = re.sub(r"\s+", " ", "".join(self._cell_chunks)).strip()
            if self._row is not None:
                self._row.extend([text] * self._colspan)
            self._in_cell = False
        elif tag == "tr" and self._in_table and self._row is not None:
            self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._in_table:
            self._in_table -= 1
            if self._in_table == 0 and len(self._rows) >= 2:
                self.tables.append(self._rows)

    def handle_data(self, data):
        if self._in_cell:
            self._cell_chunks.append(data)


def parse_html_tables(markdown: str) -> List[List[List[str]]]:
    """Extract every HTML <table> embedded in `markdown` the same way
    parse_markdown_tables does for pipe tables — this is the form
    PP-StructureV3 actually emits (see module docstring)."""
    if not markdown or "<table" not in markdown.lower():
        return []
    parser = _HTMLTableParser()
    try:
        parser.feed(markdown)
    except Exception:
        return []
    return parser.tables


def _cell(row: List[str], index: Optional[int]) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


def table_to_items(table: List[List[str]]) -> List[dict]:
    if len(table) < 2:
        return []
    mapping = map_columns(table[0])
    if "name" not in mapping:
        return []  # doesn't look like an item table at all

    items = []
    for row in table[1:]:
        name = to_text(_cell(row, mapping.get("name")), 250)
        if not name or name.lower() in ("total", "grand total", "subtotal"):
            continue
        quantity = to_number(_cell(row, mapping.get("quantity")))
        rate = to_number(_cell(row, mapping.get("rate")))
        discount = to_number(_cell(row, mapping.get("discount")))
        taxable_value = to_number(_cell(row, mapping.get("taxable_value")))
        if not taxable_value and quantity and rate:
            taxable_value = round(quantity * rate - discount, 2)
        total = to_number(_cell(row, mapping.get("total")))
        gst_cell = _cell(row, mapping.get("gst_rate"))
        gst_match = GST_PCT_RE.search(gst_cell) or GST_PCT_RE.search(name)
        items.append({
            "raw_name": name,
            "normalized_name": name,
            "hsn": to_text(_cell(row, mapping.get("hsn")), 20),
            "barcode": "",
            "unit": to_text(_cell(row, mapping.get("unit")), 20) or "PCS",
            "quantity": quantity,
            "rate": rate,
            "discount": discount,
            "taxable_value": taxable_value,
            "gst_rate": to_number(gst_match.group(1)) if gst_match else to_number(gst_cell),
            "cgst": to_number(_cell(row, mapping.get("cgst"))),
            "sgst": to_number(_cell(row, mapping.get("sgst"))),
            "igst": to_number(_cell(row, mapping.get("igst"))),
            "cess": to_number(_cell(row, mapping.get("cess"))),
            "total": total,
            "confidence": 0.75,  # from a genuinely detected table, not a text guess
        })
    return items


def markdown_to_invoice(markdown: str) -> Tuple[dict, List[str]]:
    """Full page(s) of PP-StructureV3 markdown -> the standard invoice dict.

    Returns (data, notes) — notes are user-facing strings about what this
    function had to fall back on, shown on the review screen like the
    existing "Auto-filled" notes.
    """
    notes: List[str] = []
    html_tables = parse_html_tables(markdown)
    tables = html_tables + parse_markdown_tables(markdown)

    # Header fields (supplier/GSTIN/invoice no/date/grand total/summary tax):
    # strip the table blocks out first so stray numbers in a table don't get
    # mistaken for the printed grand total by the header regexes.
    text_only = re.sub(r"<table\b.*?</table>", "", markdown, flags=re.IGNORECASE | re.DOTALL)
    for table in tables:
        for row in table:
            joined = "|".join(row)
            if joined:
                text_only = text_only.replace(joined, "")
    header_data = parse_text_heuristic(text_only)

    items: List[dict] = []
    for table in tables:
        found = table_to_items(table)
        if found:
            items.extend(found)
    if not items and header_data["items"]:
        # No table detected at all (unusual bill) — fall back to the same
        # line-regex heuristic plain-text OCR engines use.
        items = header_data["items"]
        notes.append(
            "No table layout was detected on this page; line items were "
            "read with the same rule-based text parser used for Tesseract/"
            "PaddleOCR — please check every line."
        )
    elif not items:
        notes.append("No line items could be identified on this page.")

    data = empty_invoice()
    data["supplier"] = header_data["supplier"]
    data["invoice"] = header_data["invoice"]
    data["items"] = items
    for key in ("subtotal", "discount_total", "tax_total", "cgst_total",
                "sgst_total", "igst_total", "cess_total", "round_off", "grand_total"):
        data[key] = header_data[key]

    if items:
        # A genuinely detected table earns more trust than flat-text-regex
        # guessing, but this is still not a vision model reading the bill
        # in context — kept well under the auto-confident threshold so the
        # review screen still asks for a human check.
        data["overall_confidence"] = 0.75 if len(tables) else 0.5
    else:
        data["overall_confidence"] = 0.1

    return data, notes
