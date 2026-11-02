"""Tests for services.bank_statement_import and services.bank_service."""
import io

import pandas as pd
import pytest

from database.models import BankTransaction
from services.bank_statement_import import BankRow, ExcelImportError, parse_bank_statement, sample_statement_bytes
from services.bank_service import (
    allocate,
    build_bank_voucher_payload,
    import_bank_statement,
    open_purchase_invoices,
    open_sales_invoices,
    post_bank_transaction,
    purchase_outstanding,
    set_manual_match,
    suggest_match,
)
from services.customer_service import find_or_create_customer
from services.purchase_service import find_or_create_supplier
from services.sales_service import build_sales_voucher_payload  # noqa: F401 (import sanity)
from services.tally_connector import MockTallyConnector
from database.models import Invoice, InvoiceStatus, SalesInvoice


def _statement_bytes(rows):
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    return buf.getvalue()


def test_parse_sample_statement():
    rows = parse_bank_statement(sample_statement_bytes(), "sample.xlsx")
    assert len(rows) == 3
    assert rows[0].debit == 12600.0
    assert rows[1].credit == 3200.5


def test_parse_requires_narration_and_date_columns():
    df = pd.DataFrame([{"Amount": 100}])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    with pytest.raises(ExcelImportError):
        parse_bank_statement(buf.getvalue(), "bad.xlsx")


def test_parse_handles_signed_amount_column():
    rows = [
        {"Date": "01-09-2026", "Narration": "PAID X", "Amount": -500},
        {"Date": "02-09-2026", "Narration": "RECEIVED Y", "Amount": 700},
    ]
    parsed = parse_bank_statement(_statement_bytes(rows), "signed.xlsx")
    assert parsed[0].debit == 500.0 and parsed[0].credit == 0.0
    assert parsed[1].credit == 700.0 and parsed[1].debit == 0.0


def test_import_bank_statement_dedupes_on_reupload(session, company, user):
    rows = parse_bank_statement(sample_statement_bytes(), "sample.xlsx")
    created, skipped = import_bank_statement(session, company, user, "HDFC Current", "sample.xlsx", rows)
    assert len(created) == 3
    assert skipped == 0

    rows_again = parse_bank_statement(sample_statement_bytes(), "sample.xlsx")
    created2, skipped2 = import_bank_statement(session, company, user, "HDFC Current", "sample.xlsx", rows_again)
    assert len(created2) == 0
    assert skipped2 == 3


def test_suggest_match_finds_supplier_by_name_in_narration(session, company, user):
    supplier = find_or_create_supplier(session, company.id, "Shreeji Distributors", "", "")
    session.commit()

    rows = [BankRow(
        txn_date="2026-09-01", narration="NEFT DR-HDFC0000123-SHREEJI DISTRIBUTORS-INV-101",
        reference_no="N1", debit=12600.0, credit=0.0, balance=0.0, row_hash="h1",
    )]
    created, _ = import_bank_statement(session, company, user, "HDFC Current", "s.xlsx", rows)
    txn = created[0]
    assert txn.match_party_type == "supplier"
    assert txn.match_party_id == supplier.id
    assert txn.match_confidence >= 0.5
    assert txn.voucher_type == "Payment"


def test_suggest_match_no_match_leaves_unmatched(session, company, user):
    rows = [BankRow(
        txn_date="2026-09-07", narration="BANK CHARGES-QTR MAINTENANCE",
        reference_no="", debit=236.0, credit=0.0, balance=0.0, row_hash="h2",
    )]
    created, _ = import_bank_statement(session, company, user, "HDFC Current", "s.xlsx", rows)
    txn = created[0]
    assert txn.match_party_type is None
    assert txn.status == "UNMATCHED"


def test_manual_match_to_ledger_and_allocate_and_post(session, company, user):
    rows = [BankRow(
        txn_date="2026-09-07", narration="BANK CHARGES-QTR MAINTENANCE",
        reference_no="", debit=236.0, credit=0.0, balance=0.0, row_hash="h3",
    )]
    created, _ = import_bank_statement(session, company, user, "HDFC Current", "s.xlsx", rows)
    txn = created[0]

    set_manual_match(session, txn, "ledger", ledger_name="Bank Charges")
    ok, msg = allocate(session, txn, [])  # no invoice allocation — pure expense
    assert ok, msg
    assert txn.status == "MATCHED"

    connector = MockTallyConnector({"test_mode": False})
    connector.create_ledger("Bank Charges", "Indirect Expenses")
    connector.create_ledger("HDFC Current", "Bank Accounts")
    result = post_bank_transaction(session, company, user, txn, connector, run_preflight=False)
    assert result.success, result.message
    assert txn.status == "POSTED"


def test_allocate_rejects_over_allocation(session, company, user):
    rows = [BankRow(
        txn_date="2026-09-01", narration="PAYMENT TO ABC", reference_no="",
        debit=1000.0, credit=0.0, balance=0.0, row_hash="h4",
    )]
    created, _ = import_bank_statement(session, company, user, "HDFC Current", "s.xlsx", rows)
    txn = created[0]
    set_manual_match(session, txn, "ledger", ledger_name="Misc Expense")

    invoice = Invoice(company_id=company.id, status=InvoiceStatus.CONFIRMED.value, grand_total=1000.0)
    session.add(invoice)
    session.commit()

    ok, msg = allocate(session, txn, [
        {"invoice_kind": "purchase", "invoice_id": invoice.id, "amount": 5000.0}
    ])
    assert not ok
    assert "exceeds" in msg.lower()


def test_purchase_outstanding_reduces_after_allocation(session, company, user):
    invoice = Invoice(company_id=company.id, status=InvoiceStatus.CONFIRMED.value, grand_total=1000.0)
    session.add(invoice)
    session.commit()
    assert purchase_outstanding(session, invoice) == 1000.0

    rows = [BankRow(
        txn_date="2026-09-01", narration="PAYMENT", reference_no="",
        debit=600.0, credit=0.0, balance=0.0, row_hash="h5",
    )]
    created, _ = import_bank_statement(session, company, user, "HDFC Current", "s.xlsx", rows)
    txn = created[0]
    set_manual_match(session, txn, "ledger", ledger_name="Sundry Creditors")
    allocate(session, txn, [{"invoice_kind": "purchase", "invoice_id": invoice.id, "amount": 600.0}])

    assert purchase_outstanding(session, invoice) == pytest.approx(400.0)


def test_build_bank_voucher_payload_payment_direction(session, company, user):
    rows = [BankRow(
        txn_date="2026-09-01", narration="PAYMENT", reference_no="REF1",
        debit=500.0, credit=0.0, balance=0.0, row_hash="h6",
    )]
    created, _ = import_bank_statement(session, company, user, "HDFC Current", "s.xlsx", rows)
    txn = created[0]
    set_manual_match(session, txn, "ledger", ledger_name="Misc Expense")
    allocate(session, txn, [])

    payload = build_bank_voucher_payload(session, company, txn)
    assert payload.direction == "payment"
    assert payload.amount == 500.0
    assert payload.bank_ledger == "HDFC Current"
    assert payload.party_ledger == "Misc Expense"


def _matched(session, company, user, narration, debit, credit, party_type, **kw):
    rows = [BankRow(txn_date="2026-09-08", narration=narration, reference_no="",
                    debit=debit, credit=credit, balance=0.0, row_hash=narration)]
    created, _ = import_bank_statement(session, company, user, "Axis Current", "s.xlsx", rows)
    txn = created[0]
    set_manual_match(session, txn, party_type, **kw)
    ok, msg = allocate(session, txn, [])
    assert ok, msg
    return txn


def test_bank_post_auto_creates_missing_ledgers(session, company, user):
    from database.models import AuditLog

    txn = _matched(session, company, user, "OFFICE RENT OCT", 15000.0, 0.0,
                   "ledger", ledger_name="Office Rent")
    connector = MockTallyConnector({"test_mode": False})
    assert "Office Rent" not in connector.list_ledgers()

    result = post_bank_transaction(session, company, user, txn, connector, run_preflight=True)
    assert result.success, result.message
    assert "Office Rent" in connector.list_ledgers()
    assert "Axis Current" in connector.list_ledgers()
    log = session.query(AuditLog).filter(
        AuditLog.company_id == company.id, AuditLog.action == "TALLY_MASTERS_AUTO_CREATED"
    ).all()[-1].detail
    assert "Ledger 'Office Rent' under Indirect Expenses" in log
    assert "Ledger 'Axis Current' under Bank Accounts" in log


def test_bank_receipt_ledger_goes_under_income_and_parties_under_debtors(session, company, user):
    txn = _matched(session, company, user, "INTEREST CREDIT", 0.0, 120.0,
                   "ledger", ledger_name="Bank Interest")
    assert build_bank_voucher_payload(session, company, txn).party_group == "Indirect Incomes"

    customer = find_or_create_customer(session, company.id, "Ravi Traders", "", "")
    session.commit()
    txn2 = _matched(session, company, user, "NEFT CR RAVI TRADERS", 0.0, 5000.0,
                    "customer", party_id=customer.id)
    assert build_bank_voucher_payload(session, company, txn2).party_group == "Sundry Debtors"


def test_bank_auto_create_can_be_switched_off(session, company, user):
    from database.database import set_setting

    set_setting(session, company.id, "auto_create_masters", "0")
    txn = _matched(session, company, user, "ELECTRICITY BILL", 900.0, 0.0,
                   "ledger", ledger_name="Electricity Charges")
    result = post_bank_transaction(session, company, user, txn, MockTallyConnector({"test_mode": False}),
                                   run_preflight=True)
    assert not result.success
    assert "does not exist in Tally" in result.message
