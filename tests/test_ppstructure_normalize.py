"""Tests for services.ppstructure_normalize — the part of the PP-StructureV3
integration that needs no actual PaddleOCR install: turning its markdown/
table output into the standard invoice dict."""
import pytest

from services.ai_invoice_parser import normalize_invoice
from services.invoice_validator import InvoiceValidator
from services.ppstructure_normalize import (
    map_columns,
    markdown_to_invoice,
    parse_html_tables,
    parse_markdown_tables,
    table_to_items,
)

REAL_BILL_MARKDOWN = """
NAGPUR TRADING COMPANY
GSTIN: 27LMNOP9012Q1Z3
Invoice No: NTC/2026/2291
Invoice Date: 01/09/2026

| Description | HSN | Qty | Rate | Disc | GST% | Amount |
| --- | --- | --- | --- | --- | --- | --- |
| Surf Excel Easy Wash 1 kg | 34022010 | 24 | 118.00 | 0.00 | 18 | 2832.00 |
| Lifebuoy Total Soap 125 gm | 34011110 | 100 | 32.00 | 0.00 | 18 | 3200.00 |

Taxable Value: 6,032.00
IGST: 1,085.76
Round Off: 0.24
Grand Total: 7,118.00
"""


def test_parse_markdown_tables_finds_the_table():
    tables = parse_markdown_tables(REAL_BILL_MARKDOWN)
    assert len(tables) == 1
    header, *rows = tables[0]
    assert header == ["Description", "HSN", "Qty", "Rate", "Disc", "GST%", "Amount"]
    assert len(rows) == 2


def test_map_columns_handles_alias_headers():
    mapping = map_columns(["Product Name", "HSN Code", "Quantity", "Unit Price", "Net Amount"])
    assert mapping["name"] == 0
    assert mapping["hsn"] == 1
    assert mapping["quantity"] == 2
    assert mapping["rate"] == 3
    assert mapping["total"] == 4


def test_table_to_items_extracts_real_bill_rows():
    tables = parse_markdown_tables(REAL_BILL_MARKDOWN)
    items = table_to_items(tables[0])
    assert len(items) == 2
    assert items[0]["raw_name"] == "Surf Excel Easy Wash 1 kg"
    assert items[0]["quantity"] == 24
    assert items[0]["rate"] == 118.0
    assert items[0]["total"] == 2832.0
    assert items[0]["gst_rate"] == 18.0
    assert items[0]["confidence"] == 0.75


def test_table_with_no_name_column_is_rejected():
    table = [["A", "B", "C"], ["1", "2", "3"]]
    assert table_to_items(table) == []


def test_markdown_to_invoice_extracts_header_and_items():
    data, notes = markdown_to_invoice(REAL_BILL_MARKDOWN)
    assert data["supplier"]["name"] == "NAGPUR TRADING COMPANY"
    assert data["supplier"]["gstin"] == "27LMNOP9012Q1Z3"
    assert data["invoice"]["invoice_number"] == "NTC/2026/2291"
    assert len(data["items"]) == 2
    assert data["grand_total"] == 7118.0
    assert data["igst_total"] == pytest.approx(1085.76)
    assert data["overall_confidence"] == 0.75
    assert notes == []


def test_markdown_to_invoice_reconciles_through_the_full_pipeline():
    """The real point of reusing the existing schema: this output must pass
    straight through normalize_invoice + InvoiceValidator with no mismatch,
    exactly like the earlier Gemini/summary-tax fix."""
    data, _ = markdown_to_invoice(REAL_BILL_MARKDOWN)
    normalized = normalize_invoice(data)
    report = InvoiceValidator().validate(normalized, buyer_gstin="")
    assert report.errors == []
    assert sum(i["igst"] for i in normalized["items"]) == pytest.approx(1085.76)
    assert report.tax_scope == "INTER-STATE"


def test_no_table_falls_back_to_line_heuristic():
    text = (
        "ABC Traders\nInvoice No: INV-9\nInvoice Date: 01/09/2026\n"
        "Rice 25kg 10 1200 12000\n"
        "Grand Total: 12000\n"
    )
    data, notes = markdown_to_invoice(text)
    assert len(data["items"]) == 1
    assert data["items"][0]["raw_name"] == "Rice 25kg"
    assert any("No table layout" in n for n in notes)
    assert data["overall_confidence"] == 0.5


def test_completely_empty_page_reports_no_items_found():
    data, notes = markdown_to_invoice("Just some unrelated text.\nNothing billable here.")
    assert data["items"] == []
    assert any("No line items" in n for n in notes)
    assert data["overall_confidence"] == 0.1


def test_multiple_tables_on_one_page_are_all_collected():
    extra = "\n\n| Item | Qty | Rate | Amount |\n| --- | --- | --- | --- |\n| Extra Item | 1 | 50.00 | 50.00 |\n"
    data, _ = markdown_to_invoice(REAL_BILL_MARKDOWN + extra)
    names = [i["raw_name"] for i in data["items"]]
    assert "Surf Excel Easy Wash 1 kg" in names
    assert "Extra Item" in names


# PaddleX's table-recognition module returns tables as raw HTML
# (`pred_html`), and PP-StructureV3's save_to_markdown() embeds that HTML
# verbatim in the .md file rather than converting it to pipe syntax — this
# is the real shape the app gets, confirmed against a real user bill
# (GKB/0977, Gupta Kirana Bhandar) where the pipe-only parser found nothing
# and the review screen showed 10% confidence / "No line items extracted".
REAL_HTML_TABLE_BILL = """
GUPTA KIRANA BHANDAR
Invoice no: GKB/0977
Invoice date: 2026-09-01

<html><body><table><tbody>
<tr><td>Description</td><td>HSN</td><td>Qty</td><td>Rate</td><td>Disc</td><td>GST%</td><td>Amount</td></tr>
<tr><td>Tata Salt 1 kg</td><td>25010020</td><td>30</td><td>22.00</td><td>0.00</td><td>5</td><td>660.00</td></tr>
<tr><td>Aashirvaad Atta 5 kg</td><td>11010000</td><td>20</td><td>255.00</td><td>0.00</td><td>5</td><td>5,100.00</td></tr>
<tr><td>Maggi Noodles 70 gm</td><td>19023010</td><td>96</td><td>14.00</td><td>0.00</td><td>18</td><td>1,344.00</td></tr>
<tr><td>Sunfeast Dark Fantasy 300 gm</td><td>19053100</td><td>24</td><td>110.00</td><td>0.00</td><td>18</td><td>2,640.00</td></tr>
<tr><td>Red Label Tea 500 gm</td><td>09024010</td><td>16</td><td>265.00</td><td>0.00</td><td>5</td><td>4,240.00</td></tr>
</tbody></table></body></html>

Taxable Value: 13,984.00
CGST: 608.56
SGST: 608.56
Round Off: -0.12
GRAND TOTAL: 15,201.00
"""


def test_parse_html_tables_finds_the_paddlex_html_table():
    tables = parse_html_tables(REAL_HTML_TABLE_BILL)
    assert len(tables) == 1
    header, *rows = tables[0]
    assert header == ["Description", "HSN", "Qty", "Rate", "Disc", "GST%", "Amount"]
    assert len(rows) == 5


def test_parse_html_tables_ignores_plain_markdown():
    assert parse_html_tables(REAL_BILL_MARKDOWN) == []


def test_parse_html_tables_expands_colspan():
    html = "<table><tr><td colspan=\"2\">Header</td></tr><tr><td>a</td><td>b</td></tr></table>"
    tables = parse_html_tables(html)
    assert tables == [[["Header", "Header"], ["a", "b"]]]


def test_markdown_to_invoice_reads_paddlex_html_table_bill():
    """This is the exact bug the real GKB/0977 bill surfaced: before
    parse_html_tables existed, this returned 0 items and 10% confidence
    because the table isn't pipe-delimited markdown."""
    data, notes = markdown_to_invoice(REAL_HTML_TABLE_BILL)
    assert len(data["items"]) == 5
    names = [i["raw_name"] for i in data["items"]]
    assert "Tata Salt 1 kg" in names
    assert "Aashirvaad Atta 5 kg" in names
    maggi = next(i for i in data["items"] if i["raw_name"] == "Maggi Noodles 70 gm")
    assert maggi["quantity"] == 96
    assert maggi["rate"] == 14.0
    assert maggi["total"] == 1344.0
    assert maggi["gst_rate"] == 18.0
    assert data["invoice"]["invoice_number"] == "GKB/0977"
    assert data["grand_total"] == 15201.0
    assert data["overall_confidence"] == 0.75
    assert notes == []


def test_markdown_to_invoice_reconciles_html_table_bill_through_full_pipeline():
    data, _ = markdown_to_invoice(REAL_HTML_TABLE_BILL)
    normalized = normalize_invoice(data)
    report = InvoiceValidator().validate(normalized, buyer_gstin="")
    assert report.errors == []
    assert report.tax_scope == "INTRA-STATE"
