"""Voucher numbering: start number, prefix/suffix, no gaps on failure."""
from __future__ import annotations

from services import voucher_numbering as vn
from services.tally_connector import (
    LedgerVoucherPayload,
    MockTallyConnector,
    TallyResult,
    VoucherPayload,
    build_ledger_voucher_xml,
    build_purchase_voucher_xml,
    build_sales_voucher_xml,
)


def purchase_payload(**kw):
    return VoucherPayload(company_name="Co", voucher_type="Purchase", supplier_ledger="S",
                          purchase_ledger="Purchase Account", invoice_number="INV-7",
                          invoice_date="2026-10-20", grand_total=100, **kw)


def test_default_is_tally_numbering_and_no_tag(session, company):
    payload = purchase_payload()
    assert vn.assign(session, company.id, payload, "INV-7") == ""
    assert "<VOUCHERNUMBER>" not in build_purchase_voucher_xml(payload)


def test_custom_series_starts_where_chosen(session, company):
    vn.save_config(session, company.id, "purchase",
                   vn.SeriesConfig(mode="custom", prefix="PUR/", next_number=101, digits=4,
                                   suffix="/26-27"))
    payload = purchase_payload()
    assert vn.assign(session, company.id, payload) == "PUR/0101/26-27"
    xml = build_purchase_voucher_xml(payload)
    assert "<VOUCHERNUMBER>PUR/0101/26-27</VOUCHERNUMBER>" in xml
    assert "<REFERENCE>INV-7</REFERENCE>" in xml   # supplier bill no. kept as reference


def test_number_used_only_after_success_and_never_in_test_mode(session, company):
    vn.save_config(session, company.id, "purchase",
                   vn.SeriesConfig(mode="custom", prefix="P-", next_number=5))
    payload = purchase_payload()
    vn.assign(session, company.id, payload)
    vn.consume(session, company.id, payload, TallyResult(False, "rejected"))
    vn.consume(session, company.id, payload, TallyResult(True, "ok", mode="TEST_MODE"))
    assert vn.get_config(session, company.id, "purchase").next_number == 5
    vn.consume(session, company.id, payload, TallyResult(True, "ok", mode="POSTED"))
    assert vn.get_config(session, company.id, "purchase").next_number == 6
    payload2 = purchase_payload()
    assert vn.assign(session, company.id, payload2) == "P-6"


def test_invoice_mode_for_sales(session, company):
    vn.save_config(session, company.id, "sales", vn.SeriesConfig(mode="invoice"))
    payload = purchase_payload(transaction_kind="sales")
    assert vn.assign(session, company.id, payload, "GST/2026/0042") == "GST/2026/0042"
    assert "<VOUCHERNUMBER>GST/2026/0042</VOUCHERNUMBER>" in build_sales_voucher_xml(payload)


def test_series_are_separate_and_per_company(session, company, other_company):
    vn.save_config(session, company.id, "payment",
                   vn.SeriesConfig(mode="custom", prefix="PAY", next_number=1, digits=3))
    pay = LedgerVoucherPayload(company_name="Co", voucher_type="Payment", direction="payment",
                               bank_ledger="HDFC", party_ledger="Rent", amount=10,
                               voucher_date="2026-10-20")
    rec = LedgerVoucherPayload(company_name="Co", voucher_type="Receipt", direction="receipt",
                               bank_ledger="HDFC", party_ledger="X", amount=10,
                               voucher_date="2026-10-20")
    assert vn.assign(session, company.id, pay) == "PAY001"
    assert vn.assign(session, company.id, rec) == ""
    assert vn.assign(session, other_company.id, pay) == ""
    pay.vch_number = "PAY001"
    assert "<VOUCHERNUMBER>PAY001</VOUCHERNUMBER>" in build_ledger_voucher_xml(pay)


def test_purchase_post_applies_and_advances_series(session, company, user):
    from tests.test_workflow import TestPostingAndStock
    from services.purchase_service import post_to_tally

    vn.save_config(session, company.id, "purchase",
                   vn.SeriesConfig(mode="custom", prefix="PUR-", next_number=900))
    invoice, _ = TestPostingAndStock()._confirmed(session, company, user)
    result, _ = post_to_tally(session, company, user, invoice,
                              MockTallyConnector({"test_mode": False}), run_preflight=False)
    assert result.success
    assert "<VOUCHERNUMBER>PUR-900</VOUCHERNUMBER>" in result.request_xml
    assert "Voucher no. PUR-900" in result.message
    assert vn.get_config(session, company.id, "purchase").next_number == 901


def test_every_ledger_used_in_vouchers_is_editable_mapping(session, company):
    from database.database import all_mappings, seed_tally_mappings
    seed_tally_mappings(session, company.id)
    keys = set(all_mappings(session, company.id))
    for key in ("freight_ledger", "sales_freight_ledger", "sales_ledger", "sales_cgst_ledger",
                "bank_payment_voucher_type", "customer_ledger_group", "round_off_ledger"):
        assert key in keys


def test_purchase_and_sales_freight_use_their_own_ledgers(session, company):
    from types import SimpleNamespace
    from database.database import seed_tally_mappings
    from database.models import TallyMapping
    from services.universal_persist import charge_ledger_lines

    seed_tally_mappings(session, company.id)
    row = session.query(TallyMapping).filter_by(company_id=company.id,
                                                mapping_key="sales_freight_ledger").one()
    row.mapping_value = "Cartage Recovered"
    session.commit()
    bill = SimpleNamespace(freight=50.0, packing_charges=0.0, other_charges=0.0)
    assert charge_ledger_lines(session, company.id, bill, "purchase")[0]["ledger"] == "Freight Inward"
    assert charge_ledger_lines(session, company.id, bill, "sales")[0]["ledger"] == "Cartage Recovered"
