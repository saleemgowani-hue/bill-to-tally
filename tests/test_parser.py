"""Tests for the AI extraction layer: JSON handling, coercion, normalisation."""
import json

import pytest

from services.ai_invoice_parser import (
    InvoiceParser,
    ParseError,
    empty_invoice,
    find_json_object,
    loads_strict,
    local_json_repair,
    normalize_date,
    normalize_gstin,
    normalize_invoice,
    normalize_unit,
    parse_text_heuristic,
    to_number,
)
from services.ocr_service import MockOCRProvider


class TestNumberCoercion:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1,234.50", 1234.50),
            ("₹ 2,000", 2000.0),
            ("18 %", 18.0),
            ("(120)", -120.0),
            ("", 0.0),
            (None, 0.0),
            ("abc", 0.0),
            (42, 42.0),
            ("Rs. 99.99/-", 99.99),
        ],
    )
    def test_to_number(self, raw, expected):
        assert to_number(raw) == expected


class TestDates:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("04/09/2026", "2026-09-04"),
            ("04-09-2026", "2026-09-04"),
            ("2026-09-04", "2026-09-04"),
            ("4 Sep 2026", "2026-09-04"),
            ("04.09.26", "2026-09-04"),
            ("garbage", ""),
            ("", ""),
        ],
    )
    def test_normalize_date(self, raw, expected):
        assert normalize_date(raw) == expected

    def test_ambiguous_us_format_is_swapped(self):
        # 13 cannot be a month, so it must be the day
        assert normalize_date("09/13/2026") == "2026-09-13"


class TestUnitsAndGstin:
    def test_units_are_standardised(self):
        assert normalize_unit("pcs") == "PCS"
        assert normalize_unit("Nos.") == "PCS"
        assert normalize_unit("kgs") == "KG"

    def test_gstin_extracted_from_noise(self):
        assert normalize_gstin("GSTIN: 23ABCDE1234F1Z5 ") == "23ABCDE1234F1Z5"


class TestJsonHandling:
    def test_finds_object_inside_prose(self):
        text = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
        assert json.loads(find_json_object(text)) == {"a": 1}

    def test_ignores_braces_inside_strings(self):
        text = '{"note": "a } brace", "n": 2}'
        assert json.loads(find_json_object(text))["n"] == 2

    def test_repairs_trailing_commas(self):
        assert json.loads(local_json_repair('{"a": 1, "b": [1,2,],}'))["b"] == [1, 2]

    def test_rejects_non_json(self):
        with pytest.raises(ParseError):
            loads_strict("no json at all here")


class TestNormalizeInvoice:
    def test_fills_missing_sections(self):
        out = normalize_invoice({})
        assert out["supplier"]["name"] == ""
        assert out["items"] == []
        assert out["grand_total"] == 0.0

    def test_percent_confidence_is_scaled(self):
        out = normalize_invoice(
            {
                "items": [{"raw_name": "X", "quantity": 1, "rate": 10, "confidence": 95}],
                "overall_confidence": 88,
            }
        )
        assert out["items"][0]["confidence"] == 0.95
        assert out["overall_confidence"] == 0.88

    def test_totals_derived_when_absent(self):
        out = normalize_invoice(
            {
                "items": [
                    {"raw_name": "A", "quantity": 2, "rate": 50, "taxable_value": 100,
                     "cgst": 9, "sgst": 9, "confidence": 0.9},
                ]
            }
        )
        assert out["subtotal"] == 100.0
        assert out["tax_total"] == 18.0

    def test_blank_rows_are_dropped(self):
        out = normalize_invoice({"items": [{"raw_name": "", "quantity": 0, "total": 0}]})
        assert out["items"] == []

    def test_currency_strings_are_coerced(self):
        out = normalize_invoice(
            {"items": [{"raw_name": "A", "quantity": "2", "rate": "₹1,250.50"}]}
        )
        assert out["items"][0]["rate"] == 1250.50

    def test_impossible_gst_rate_is_zeroed_not_guessed(self):
        out = normalize_invoice({"items": [{"raw_name": "A", "quantity": 1, "gst_rate": 1800}]})
        assert out["items"][0]["gst_rate"] == 0.0


class TestHeuristicTextParser:
    SAMPLE = """SHREEJI DISTRIBUTORS
GSTIN: 23ABCDE1234F1Z5
Invoice No: SD/2026/1187
Invoice Date: 04/09/2026

Parle G Gold 800G   24   95.00   0.00   2280.00
Tata Salt 1KG       50   22.00   50.00  1050.00

Grand Total: 7,354.50
"""

    def test_extracts_header(self):
        data = parse_text_heuristic(self.SAMPLE)
        assert data["invoice"]["invoice_number"] == "SD/2026/1187"
        assert data["invoice"]["invoice_date"] == "2026-09-04"
        assert data["supplier"]["gstin"] == "23ABCDE1234F1Z5"

    def test_extracts_lines(self):
        data = parse_text_heuristic(self.SAMPLE)
        assert len(data["items"]) == 2
        assert data["items"][0]["quantity"] == 24

    def test_confidence_stays_low(self):
        """Rule-based parsing must never claim high confidence."""
        data = parse_text_heuristic(self.SAMPLE)
        assert data["overall_confidence"] < 0.7
        assert all(i["confidence"] < 0.7 for i in data["items"])

    def test_total_rows_are_skipped(self):
        data = parse_text_heuristic(self.SAMPLE)
        assert not any("total" in i["raw_name"].lower() for i in data["items"])


class TestParserWithMockProvider:
    def test_end_to_end(self):
        parser = InvoiceParser(MockOCRProvider())
        parsed = parser.parse_images(["fake-image"])
        assert parsed.provider == "mock"
        assert len(parsed.items) == 3
        assert parsed.data["supplier"]["name"] == "Shreeji Distributors"

    def test_no_images_raises(self):
        with pytest.raises(ParseError):
            InvoiceParser(MockOCRProvider()).parse_images([])


class TestMalformedProviderOutput:
    class BrokenProvider(MockOCRProvider):
        supports_structured = True

        def extract_structured(self, images, prompt, schema_hint):
            from services.ocr_service import OCRResponse

            return OCRResponse(
                provider="broken",
                raw_response='{"supplier": {"name": "X",}, "items": [],}',
            )

    def test_trailing_commas_are_repaired_locally(self):
        parsed = InvoiceParser(self.BrokenProvider()).parse_images(["img"])
        assert parsed.data["supplier"]["name"] == "X"
