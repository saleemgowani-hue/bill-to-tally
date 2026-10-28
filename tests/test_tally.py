"""Tests for the Tally connector, XML generation and safety of test mode."""
import xml.etree.ElementTree as ET

import pytest

from services.tally_connector import (
    FileExportConnector,
    MockTallyConnector,
    TallyXMLConnector,
    VoucherPayload,
    build_purchase_voucher_xml,
    get_connector,
    parse_import_response,
    parse_names,
    tally_date,
)


@pytest.fixture
def payload():
    return VoucherPayload(
        company_name="SN Demo Traders",
        voucher_type="Purchase",
        supplier_ledger="Shreeji Distributors",
        purchase_ledger="Purchase Account",
        invoice_number="SD/2026/1187",
        invoice_date="2026-09-04",
        narration="Test voucher",
        godown="Main Location",
        round_off_ledger="Round Off",
        round_off_amount=0.5,
        tax_lines=[
            {"ledger": "CGST Input", "amount": 205.20},
            {"ledger": "SGST Input", "amount": 205.20},
        ],
        items=[
            {
                "stock_item": "Parle-G Gold Biscuit 800 gm", "quantity": 24, "unit": "PCS",
                "rate": 95.0, "taxable_value": 2280.0, "hsn": "19053100",
                "godown": "Main Location",
            }
        ],
        grand_total=2690.90,
        reference_id="test-ref-001",
    )


class TestDateFormat:
    def test_iso_to_tally(self):
        assert tally_date("2026-09-04") == "20260904"

    def test_invalid_date_falls_back_to_today(self):
        assert len(tally_date("not-a-date")) == 8


class TestVoucherXML:
    def test_is_well_formed(self, payload):
        root = ET.fromstring(build_purchase_voucher_xml(payload))
        assert root.tag == "ENVELOPE"

    def test_carries_the_key_fields(self, payload):
        xml = build_purchase_voucher_xml(payload)
        root = ET.fromstring(xml)
        voucher = root.find(".//VOUCHER")
        assert voucher.get("VCHTYPE") == "Purchase"
        assert voucher.get("REMOTEID") == "test-ref-001"
        assert voucher.find("DATE").text == "20260904"
        assert voucher.find("REFERENCE").text == "SD/2026/1187"
        assert voucher.find("PARTYLEDGERNAME").text == "Shreeji Distributors"

    def test_party_is_credited_with_the_full_total(self, payload):
        root = ET.fromstring(build_purchase_voucher_xml(payload))
        party = root.find(".//ALLLEDGERENTRIES.LIST")
        assert party.find("LEDGERNAME").text == "Shreeji Distributors"
        assert party.find("ISDEEMEDPOSITIVE").text == "No"
        assert float(party.find("AMOUNT").text) == pytest.approx(2690.90)

    def test_inventory_entry_present(self, payload):
        root = ET.fromstring(build_purchase_voucher_xml(payload))
        entry = root.find(".//ALLINVENTORYENTRIES.LIST")
        assert entry.find("STOCKITEMNAME").text == "Parle-G Gold Biscuit 800 gm"
        assert entry.find("ACTUALQTY").text == "24 PCS"
        assert entry.find("HSNCODE").text == "19053100"

    def test_tax_ledgers_included(self, payload):
        xml = build_purchase_voucher_xml(payload)
        assert "CGST Input" in xml and "SGST Input" in xml

    def test_zero_tax_lines_are_omitted(self, payload):
        payload.tax_lines = [{"ledger": "IGST Input", "amount": 0.0}]
        assert "IGST Input" not in build_purchase_voucher_xml(payload)

    def test_special_characters_are_escaped(self, payload):
        payload.supplier_ledger = "Ram & Sons <Traders>"
        xml = build_purchase_voucher_xml(payload)
        ET.fromstring(xml)  # must still parse
        assert "&amp;" in xml and "&lt;" in xml


class TestResponseParsing:
    def test_parse_names_from_attributes(self):
        xml = '<X><LEDGER NAME="Cash"/><LEDGER NAME="Bank"/></X>'
        assert parse_names(xml, "LEDGER") == ["Cash", "Bank"]

    def test_parse_import_counts(self):
        xml = "<RESPONSE><CREATED>1</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS></RESPONSE>"
        stats = parse_import_response(xml)
        assert stats["created"] == 1 and stats["errors"] == 0


class TestMockConnector:
    def test_never_claims_a_real_post(self, payload):
        result = MockTallyConnector({"test_mode": False}).post_voucher(payload)
        assert result.success
        assert "MOCK" in result.message.upper()
        assert result.voucher_id.startswith("MOCK-")

    def test_preflight_catches_missing_ledger(self, payload):
        payload.supplier_ledger = "Nonexistent Supplier Ltd"
        problems = MockTallyConnector().preflight(payload)
        assert any("Nonexistent Supplier Ltd" in p for p in problems)

    def test_preflight_passes_for_known_masters(self, payload):
        connector = MockTallyConnector({"company": "SN Demo Traders (2026-27)"})
        payload.company_name = "SN Demo Traders (2026-27)"
        payload.supplier_ledger = "Shreeji Distributors"
        payload.items[0]["stock_item"] = "Parle-G Gold Biscuit 800 gm"
        assert connector.preflight(payload) == []


class TestTestMode:
    def test_test_mode_posts_nothing(self, payload, tmp_path):
        connector = TallyXMLConnector({"test_mode": True, "host": "127.0.0.1", "port": 1})
        result = connector.post_voucher(payload)
        assert result.success
        assert result.mode == "TEST_MODE"
        assert "TEST MODE" in result.message
        assert result.request_xml  # the XML is shown
        assert any("No accounting entry" in w for w in result.warnings)

    def test_live_mode_fails_cleanly_when_tally_is_closed(self, payload):
        connector = TallyXMLConnector(
            {"test_mode": False, "host": "127.0.0.1", "port": 1, "timeout": 1}
        )
        result = connector.post_voucher(payload)
        assert not result.success
        assert "Tally" in result.message


class TestFileExportConnector:
    def test_writes_importable_file(self, payload, tmp_path):
        connector = FileExportConnector()
        result = connector.post_voucher(payload)
        assert result.success
        assert result.mode == "XML_EXPORT"
        from pathlib import Path

        assert Path(result.export_path).exists()
        ET.parse(result.export_path)


class TestRegistry:
    def test_unknown_mode_falls_back_to_mock(self):
        assert isinstance(get_connector("nonsense"), MockTallyConnector)

    def test_known_modes(self):
        assert isinstance(get_connector("xml"), TallyXMLConnector)
        assert isinstance(get_connector("file"), FileExportConnector)
