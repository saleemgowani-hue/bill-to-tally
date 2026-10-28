import xml.etree.ElementTree as ET
import pytest
from services.tally_connector import LedgerVoucherPayload, build_ledger_voucher_xml, build_xml_for_payload

def test_payment_debits_party_credits_bank():
    payload = LedgerVoucherPayload(
        company_name="X", voucher_type="Payment", direction="payment",
        bank_ledger="HDFC Bank", party_ledger="Shreeji Distributors",
        amount=5000.0, voucher_date="2026-09-10", reference_id="r1",
        bill_allocations=[{"invoice_number": "INV-1", "amount": 5000.0}],
    )
    xml_body = build_ledger_voucher_xml(payload)
    root = ET.fromstring(xml_body)
    voucher = root.find(".//VOUCHER")
    assert voucher.get("VCHTYPE") == "Payment"
    entries = {e.findtext("LEDGERNAME"): e for e in voucher.findall("ALLLEDGERENTRIES.LIST")}
    party = entries["Shreeji Distributors"]
    bank = entries["HDFC Bank"]
    assert party.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert float(party.findtext("AMOUNT")) == pytest.approx(-5000.0)
    assert bank.findtext("ISDEEMEDPOSITIVE") == "No"
    assert float(bank.findtext("AMOUNT")) == pytest.approx(5000.0)
    bill = party.find("BILLALLOCATIONS.LIST")
    assert bill is not None
    assert float(bill.findtext("AMOUNT")) == pytest.approx(-5000.0)

def test_receipt_debits_bank_credits_party():
    payload = LedgerVoucherPayload(
        company_name="X", voucher_type="Receipt", direction="receipt",
        bank_ledger="HDFC Bank", party_ledger="Retail Mart",
        amount=3200.5, voucher_date="2026-09-10", reference_id="r2",
    )
    xml_body = build_ledger_voucher_xml(payload)
    root = ET.fromstring(xml_body)
    voucher = root.find(".//VOUCHER")
    entries = {e.findtext("LEDGERNAME"): e for e in voucher.findall("ALLLEDGERENTRIES.LIST")}
    bank = entries["HDFC Bank"]
    party = entries["Retail Mart"]
    assert bank.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert float(bank.findtext("AMOUNT")) == pytest.approx(-3200.5)
    assert party.findtext("ISDEEMEDPOSITIVE") == "No"
    assert float(party.findtext("AMOUNT")) == pytest.approx(3200.5)

def test_dispatcher_routes_ledger_payload():
    payload = LedgerVoucherPayload(
        company_name="X", voucher_type="Payment", direction="payment",
        bank_ledger="B", party_ledger="P", amount=1, voucher_date="2026-01-01",
        reference_id="fixed",
    )
    assert build_xml_for_payload(payload) == build_ledger_voucher_xml(payload)
