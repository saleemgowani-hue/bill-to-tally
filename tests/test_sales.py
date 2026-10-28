"""Tests for the Sales pipeline: ingestion, duplicate check, and the
Sales voucher XML (polarity mirrors Purchase, flipped)."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET

import pandas as pd
import pytest

from database.models import InvoiceStatus, SalesInvoice
from services.sales_service import (
    build_sales_voucher_payload,
    check_duplicate_sales,
    confirm_sales_invoice,
    ingest_sales_excel,
    post_sales_to_tally,
)
from services.tally_connector import (
    MockTallyConnector,
    VoucherPayload,
    build_sales_voucher_xml,
)

ROWS = [
    {
        "Customer": "Retail Mart", "Invoice No": "SI-1", "Invoice Date": "05-09-2026",
        "Item": "Rice 25kg", "Qty": 2, "Rate": 1500, "GST%": 5,
    },
    {
        "Customer": "Retail Mart", "Invoice No": "SI-1", "Invoice Date": "05-09-2026",
        "Item": "Oil 15L", "Qty": 1, "Rate": 2000, "GST%": 5,
    },
]


def _xlsx_bytes(rows):
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    return buf.getvalue()


@pytest.fixture
def sales_payload():
    return VoucherPayload(
        company_name="SN Demo Traders",
        voucher_type="Sales",
        supplier_ledger="Retail Mart",       # customer, reused field
        purchase_ledger="Sales Account",     # trading ledger, reused field
        invoice_number="SI-1001",
        invoice_date="2026-09-05",
        narration="Test sales voucher",
        godown="Main Location",
        round_off_ledger="Round Off",
        round_off_amount=0.5,
        tax_lines=[
            {"ledger": "CGST Output", "amount": 100.0},
            {"ledger": "SGST Output", "amount": 100.0},
        ],
        items=[
            {
                "stock_item": "Rice 25kg", "quantity": 2, "unit": "BAG",
                "rate": 1500.0, "taxable_value": 3000.0, "hsn": "1006",
                "godown": "Main Location",
            }
        ],
        grand_total=3200.5,
        reference_id="test-sales-ref-001",
        transaction_kind="sales",
    )


class TestSalesVoucherXML:
    def test_is_well_formed(self, sales_payload):
        root = ET.fromstring(build_sales_voucher_xml(sales_payload))
        assert root.tag == "ENVELOPE"

    def test_voucher_type_is_sales(self, sales_payload):
        xml_body = build_sales_voucher_xml(sales_payload)
        assert 'VCHTYPE="Sales"' in xml_body

    def test_customer_ledger_is_debited(self, sales_payload):
        """Party (customer) entry: negative amount + ISDEEMEDPOSITIVE=Yes."""
        xml_body = build_sales_voucher_xml(sales_payload)
        root = ET.fromstring(xml_body)
        voucher = root.find(".//VOUCHER")
        party_entries = [
            e for e in voucher.findall("ALLLEDGERENTRIES.LIST")
            if e.findtext("LEDGERNAME") == "Retail Mart"
        ]
        assert len(party_entries) == 1
        entry = party_entries[0]
        assert entry.findtext("ISDEEMEDPOSITIVE") == "Yes"
        assert float(entry.findtext("AMOUNT")) == pytest.approx(-3200.5)

    def test_sales_and_tax_ledgers_are_credited(self, sales_payload):
        """Trading + tax entries: positive amount + ISDEEMEDPOSITIVE=No."""
        xml_body = build_sales_voucher_xml(sales_payload)
        root = ET.fromstring(xml_body)
        voucher = root.find(".//VOUCHER")

        tax_entries = [
            e for e in voucher.findall("ALLLEDGERENTRIES.LIST")
            if e.findtext("LEDGERNAME") in ("CGST Output", "SGST Output")
        ]
        assert len(tax_entries) == 2
        for entry in tax_entries:
            assert entry.findtext("ISDEEMEDPOSITIVE") == "No"
            assert float(entry.findtext("AMOUNT")) == pytest.approx(100.0)

        inv_entry = voucher.find(".//ALLINVENTORYENTRIES.LIST")
        acct = inv_entry.find("ACCOUNTINGALLOCATIONS.LIST")
        assert inv_entry.findtext("ISDEEMEDPOSITIVE") == "No"
        assert acct.findtext("LEDGERNAME") == "Sales Account"
        assert acct.findtext("ISDEEMEDPOSITIVE") == "No"
        assert float(acct.findtext("AMOUNT")) == pytest.approx(3000.0)

    def test_dispatcher_picks_sales_builder(self, sales_payload):
        from services.tally_connector import build_voucher_xml
        assert build_voucher_xml(sales_payload) == build_sales_voucher_xml(sales_payload)

    def test_dispatcher_picks_purchase_builder_by_default(self):
        from services.tally_connector import build_purchase_voucher_xml, build_voucher_xml
        purchase_payload = VoucherPayload(
            company_name="X", voucher_type="Purchase", supplier_ledger="V",
            purchase_ledger="Purchase Account", invoice_number="1", invoice_date="2026-01-01",
            reference_id="fixed-ref",
        )
        assert build_voucher_xml(purchase_payload) == build_purchase_voucher_xml(purchase_payload)


def test_ingest_sales_excel_groups_and_matches(session, company, user):
    from tests.conftest import make_product

    make_product(session, company, "Rice 25kg", gst_rate=5.0)

    raw = _xlsx_bytes(ROWS)
    results = ingest_sales_excel(session, company, user, "sales.xlsx", raw)

    assert len(results) == 1
    invoice = results[0].invoice
    assert invoice.customer_name == "Retail Mart"
    assert invoice.invoice_number == "SI-1"
    assert len(invoice.items) == 2
    assert invoice.status == InvoiceStatus.REVIEW_REQUIRED.value
    assert invoice.grand_total > 0


def test_ingest_sales_excel_detects_duplicate_on_reupload(session, company, user):
    raw = _xlsx_bytes(ROWS)
    ingest_sales_excel(session, company, user, "sales.xlsx", raw)
    again = ingest_sales_excel(session, company, user, "sales.xlsx", raw)
    assert again[0].invoice.status == InvoiceStatus.DUPLICATE.value


def test_check_duplicate_sales_no_match_on_empty_db(session, company):
    result = check_duplicate_sales(session, company.id, "Nobody", "X-1", "2026-01-01", 100.0)
    assert result.is_duplicate is False


def test_confirm_and_post_sales_decrements_stock(session, company, user):
    from tests.conftest import make_product

    product = make_product(session, company, "Rice 25kg", gst_rate=5.0)
    product.current_stock = 50.0
    session.commit()

    raw = _xlsx_bytes([ROWS[0]])  # single line, qty 2
    results = ingest_sales_excel(session, company, user, "sales_one.xlsx", raw)
    invoice = results[0].invoice

    ok, message = confirm_sales_invoice(session, company, user, invoice)
    assert ok, message
    assert invoice.status == InvoiceStatus.CONFIRMED.value

    connector = MockTallyConnector({"test_mode": False})
    result, transaction = post_sales_to_tally(
        session, company, user, invoice, connector, run_preflight=False
    )
    assert result.success, result.message
    assert transaction.stock_applied is True

    session.refresh(product)
    assert product.current_stock == pytest.approx(48.0)  # 50 - 2
    assert invoice.status == InvoiceStatus.POSTED.value


def test_build_sales_voucher_payload_uses_customer_and_sales_ledger(session, company, user):
    from tests.conftest import make_product

    make_product(session, company, "Rice 25kg", gst_rate=5.0)
    raw = _xlsx_bytes([ROWS[0]])
    invoice = ingest_sales_excel(session, company, user, "sales_one.xlsx", raw)[0].invoice

    payload = build_sales_voucher_payload(session, company, invoice)
    assert payload.transaction_kind == "sales"
    assert payload.supplier_ledger == "Retail Mart"
    assert payload.purchase_ledger == "Sales Account"
    assert payload.voucher_type == "Sales"
