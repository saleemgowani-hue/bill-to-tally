"""Bills that print GST only in the totals block (line shows a GST %, the
IGST/CGST/SGST amount is underneath) must still reconcile. Numbers below are
from a real bill: Surf Excel 24 x 118, Lifebuoy 100 x 32, IGST 1,085.76,
round off 0.24, grand total 7,118.00."""
import pytest
from PIL import Image

from database.models import InvoiceStatus
from services.ai_invoice_parser import (
    EXTRACTION_PROMPT, SALES_EXTRACTION_PROMPT, SCHEMA_HINT,
    normalize_invoice, parse_text_heuristic,
)
from services.invoice_validator import InvoiceValidator
from services.ocr_service import OCRProvider, OCRResponse
from services.ai_invoice_parser import InvoiceParser


def _bill(**overrides):
    data = {
        "supplier": {"name": "NAGPUR TRADING COMPANY", "gstin": "27LMNOP9012Q1Z3"},
        "invoice": {"invoice_number": "NTC/2026/2291", "invoice_date": "2026-09-01"},
        "items": [
            {"raw_name": "Surf Excel Easy Wash 1 kg", "hsn": "34022010", "quantity": 24,
             "rate": 118, "discount": 0, "taxable_value": 2832, "gst_rate": 18,
             "total": 2832, "confidence": 0.98},
            {"raw_name": "Lifebuoy Total Soap 125 gm", "hsn": "34011110", "quantity": 100,
             "rate": 32, "discount": 0, "taxable_value": 3200, "gst_rate": 18,
             "total": 3200, "confidence": 0.98},
        ],
        "subtotal": 6032, "igst_total": 1085.76, "round_off": 0.24,
        "grand_total": 7118, "overall_confidence": 0.98,
    }
    data.update(overrides)
    return data


def _errors(data):
    return InvoiceValidator().validate(data, buyer_gstin="").errors


def test_igst_only_in_totals_is_spread_over_lines_and_reconciles():
    data = normalize_invoice(_bill())
    assert [i["igst"] for i in data["items"]] == [509.76, 576.00]
    assert sum(i["igst"] for i in data["items"]) == pytest.approx(1085.76)
    assert [i["total"] for i in data["items"]] == [3341.76, 3776.00]
    assert data["tax_total"] == pytest.approx(1085.76)
    assert _errors(data) == []                       # no grand_total mismatch, no line errors
    report = InvoiceValidator().validate(data, buyer_gstin="")
    assert report.tax_scope == "INTER-STATE"         # inferred from the IGST on the bill
    assert report.calculated_total == pytest.approx(7118.00)
    assert not any("no tax amount was read" in i.message for i in report.warnings)


def test_a_note_explains_what_was_filled_in():
    data = normalize_invoice(_bill())
    assert len(data["extraction_notes"]) == 1
    assert data["extraction_notes"][0].startswith("Auto-filled:")
    assert "IGST" in data["extraction_notes"][0]


def test_cgst_sgst_in_totals_are_split_across_lines():
    data = normalize_invoice(_bill(igst_total=0, cgst_total=542.88, sgst_total=542.88))
    assert [i["cgst"] for i in data["items"]] == [254.88, 288.00]
    assert [i["sgst"] for i in data["items"]] == [254.88, 288.00]
    assert all(i["igst"] == 0 for i in data["items"])
    assert _errors(data) == []


def test_totals_that_do_not_agree_with_the_lines_are_left_alone():
    data = normalize_invoice(_bill(igst_total=999.00))
    assert all(i["igst"] == 0 for i in data["items"])       # nothing invented
    assert "extraction_notes" not in data
    assert any(e.field == "grand_total" for e in _errors(data))   # mismatch still surfaced


def test_per_line_tax_already_present_is_never_touched():
    bill = _bill()
    bill["items"][0].update(igst=509.76, total=3341.76)
    bill["items"][1].update(igst=576.00, total=3776.00)
    data = normalize_invoice(bill)
    assert [i["igst"] for i in data["items"]] == [509.76, 576.00]
    assert "extraction_notes" not in data


def test_no_printed_summary_tax_means_no_derivation():
    data = normalize_invoice(_bill(igst_total=0))
    assert all(i["igst"] == 0 for i in data["items"])
    assert "extraction_notes" not in data


def test_a_printed_line_total_that_is_not_pre_tax_is_kept():
    bill = _bill()
    bill["items"][0]["total"] = 3341.76           # bill already showed tax-inclusive line total
    data = normalize_invoice(bill)
    assert data["items"][0]["total"] == 3341.76


def test_tax_total_is_taken_from_the_summary_heads_when_missing():
    data = normalize_invoice(_bill(igst_total=999.0))
    assert data["tax_total"] == pytest.approx(999.0)


def test_prompts_and_schema_ask_for_the_summary_tax_amounts():
    for text in (SCHEMA_HINT,):
        assert "cgst_total" in text and "sgst_total" in text and "igst_total" in text
    for prompt in (EXTRACTION_PROMPT, SALES_EXTRACTION_PROMPT):
        assert "igst_total" in prompt and "totals section" in prompt


def test_text_only_engines_read_summary_tax_lines():
    text = (
        "NAGPUR TRADING COMPANY\nInvoice No: NTC/2026/2291\nInvoice Date: 01/09/2026\n"
        "Surf Excel Easy Wash 24 118.00 18% 2832.00\n"
        "Lifebuoy Soap 100 32.00 18% 3200.00\n"
        "Taxable Value: 6,032.00\nIGST: 1,085.76\nGrand Total: 7,118.00\n"
    )
    raw = parse_text_heuristic(text)
    assert raw["igst_total"] == pytest.approx(1085.76)
    data = normalize_invoice(raw)
    assert sum(i["igst"] for i in data["items"]) == pytest.approx(1085.76)


class _SummaryOnlyProvider(OCRProvider):
    """Behaves like a vision model reading the bill above."""
    name = "fake-vision"
    supports_structured = True

    def extract_structured(self, images, prompt, schema_hint):
        return OCRResponse(provider=self.name, model="fake-1", structured=_bill(), duration_ms=1)

    def extract_text(self, images):
        raise NotImplementedError


def test_end_to_end_ingest_gives_a_clean_purchase_invoice_and_a_taxed_voucher(session, company, user):
    from services.purchase_service import build_voucher_payload, ingest_bill

    result = ingest_bill(
        session, company, user, "bill.jpg", b"bytes", [Image.new("RGB", (10, 10))],
        InvoiceParser(_SummaryOnlyProvider()), InvoiceValidator(), store_image=False,
    )
    invoice = result.invoice
    assert not result.report.errors
    assert invoice.tax_scope == "INTER-STATE"
    assert invoice.grand_total == pytest.approx(7118.0)
    assert (invoice.notes or "").startswith("Auto-filled:")
    assert [round(i.igst, 2) for i in invoice.items] == [509.76, 576.00]

    payload = build_voucher_payload(session, company, invoice)
    assert sum(t["amount"] for t in payload.tax_lines) == pytest.approx(1085.76)


def test_end_to_end_sales_ingest_also_gets_the_tax(session, company, user):
    from services.sales_service import ingest_sales_bill

    result = ingest_sales_bill(
        session, company, user, "sale.jpg", b"bytes2", [Image.new("RGB", (10, 10))],
        InvoiceParser(_SummaryOnlyProvider()), InvoiceValidator(), store_image=False,
    )
    assert not result.report.errors
    assert sum(i.igst for i in result.invoice.items) == pytest.approx(1085.76)
    assert (result.invoice.notes or "").startswith("Auto-filled:")


# A discounted line whose printed "Amount" is qty x rate BEFORE its own Disc
# column, with the extraction copying that printed figure verbatim into
# taxable_value/total — from a real bill (MAA SHARDA AGENCIES): Amul Butter
# 12 x 265 with a 180 discount, printed Amount 3,180 (not the discounted
# 3,000), plus three undiscounted lines. CGST/SGST are printed only in the
# totals (551.52 each) and, before this fix, the un-discounted Amul base
# (3,180) threw the whole-bill reconciliation off by exactly 180 x 12% =
# 21.6, so no line ever got its tax and both taxable_value and grand_total
# failed validation.
def _discounted_bill(**overrides):
    data = {
        "supplier": {"name": "MAA SHARDA AGENCIES", "gstin": ""},
        "invoice": {"invoice_number": "MSA/26-27/0138", "invoice_date": "2026-09-01"},
        "items": [
            {"raw_name": "Amul Butter 500 gm", "hsn": "04051000", "quantity": 12,
             "rate": 265, "discount": 180, "taxable_value": 3180, "gst_rate": 12,
             "total": 3180, "confidence": 0.95},
            {"raw_name": "Britannia Marie Gold 250 gm", "hsn": "19053100", "quantity": 40,
             "rate": 30, "discount": 0, "taxable_value": 1200, "gst_rate": 18,
             "total": 1200, "confidence": 0.95},
            {"raw_name": "Colgate Strong Teeth 100 gm", "hsn": "33061020", "quantity": 36,
             "rate": 52, "discount": 0, "taxable_value": 1872, "gst_rate": 18,
             "total": 1872, "confidence": 0.95},
            {"raw_name": "Surf Excel Bar 250 gm", "hsn": "34011190", "quantity": 48,
             "rate": 22, "discount": 0, "taxable_value": 1056, "gst_rate": 18,
             "total": 1056, "confidence": 0.95},
        ],
        "subtotal": 7128, "discount_total": 180, "cgst_total": 551.52, "sgst_total": 551.52,
        "round_off": -0.04, "grand_total": 8231.00, "overall_confidence": 0.95,
    }
    data.update(overrides)
    return data


def test_discounted_line_amount_is_corrected_before_reconciling():
    data = normalize_invoice(_discounted_bill())
    amul = data["items"][0]
    assert amul["taxable_value"] == 3000.0          # 12 x 265 - 180, not the raw 3,180
    assert amul["total"] == pytest.approx(3360.0)   # 3000 taxable + 360 (12% of 3000)
    assert sum(i["cgst"] for i in data["items"]) == pytest.approx(551.52)
    assert sum(i["sgst"] for i in data["items"]) == pytest.approx(551.52)
    assert _errors(data) == []
    report = InvoiceValidator().validate(data, buyer_gstin="")
    assert report.calculated_total == pytest.approx(8231.00)


def test_discounted_line_fix_is_noted_for_the_reviewer():
    data = normalize_invoice(_discounted_bill())
    assert len(data["extraction_notes"]) == 2
    assert data["extraction_notes"][0].startswith("Auto-filled:")
    assert data["extraction_notes"][1].startswith("Auto-corrected:")
    assert "1 line" in data["extraction_notes"][1]


def test_undiscounted_lines_are_not_touched_by_the_discount_fix():
    data = normalize_invoice(_discounted_bill())
    # Only the Amul line had discount > 0 and an un-adjusted printed amount;
    # the other three lines' taxable_value must be exactly what was printed.
    assert [i["taxable_value"] for i in data["items"][1:]] == [1200.0, 1872.0, 1056.0]


def test_a_real_post_discount_taxable_value_is_left_alone():
    """If the bill (or extraction) already printed the discounted figure,
    nothing should change — only the exact gross-echo case is corrected."""
    bill = _discounted_bill()
    bill["items"][0]["taxable_value"] = 3000   # already net of discount
    bill["items"][0]["total"] = 3000
    data = normalize_invoice(bill)
    assert data["items"][0]["taxable_value"] == 3000.0
    assert len(data["extraction_notes"]) == 1   # no "Auto-corrected" note — nothing was wrong
