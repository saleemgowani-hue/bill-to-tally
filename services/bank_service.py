"""
Bank reconciliation.

  UNMATCHED -> (suggest/manual match) -> MATCHED -> POSTED
                                            |
                                            +-> IGNORED (non-trade lines the
                                                user chooses not to post, e.g.
                                                internal transfers)

Each bank statement line becomes one BankTransaction. A debit is money
leaving the bank (normally a Payment to a supplier or an expense); a credit
is money arriving (normally a Receipt from a customer or other income). No
stock is affected by anything here — that is Purchase/Sales territory.

Matching is a best-effort suggestion, never an autopilot: suggest_match only
proposes a party and, where the amount lines up with exactly one open
invoice, a specific allocation. Nothing is posted to Tally until the user
confirms the match and allocation on the review screen.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from database.database import get_mapping
from database.models import (
    BankAccount,
    BankAllocation,
    BankTransaction,
    Company,
    Customer,
    Invoice,
    InvoiceStatus,
    SalesInvoice,
    Supplier,
    User,
)
from services import audit_service
from services.bank_statement_import import BankRow
from services.tally_connector import LedgerVoucherPayload, TallyConnector, TallyResult
from utils.config import settings
from utils.logging import get_logger

log = get_logger("bank")

CLOSED_STATUSES = (InvoiceStatus.CANCELLED.value, InvoiceStatus.DUPLICATE.value)


class BankError(RuntimeError):
    pass


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def find_or_create_bank_account(session: Session, company_id: int, name: str) -> BankAccount:
    name = (name or "Bank Account").strip()
    account = (
        session.query(BankAccount)
        .filter(BankAccount.company_id == company_id, BankAccount.name.ilike(name))
        .one_or_none()
    )
    if account:
        return account
    account = BankAccount(company_id=company_id, name=name[:160], tally_ledger_name=name[:200])
    session.add(account)
    session.flush()
    return account


# --------------------------------------------------------------------------
# Outstanding balances
# --------------------------------------------------------------------------
def purchase_outstanding(session: Session, invoice: Invoice) -> float:
    allocated = (
        session.query(BankAllocation)
        .filter(BankAllocation.invoice_id == invoice.id)
        .with_entities(BankAllocation.amount).all()
    )
    return round((invoice.grand_total or 0) - sum(a[0] or 0 for a in allocated), 2)


def sales_outstanding(session: Session, invoice: SalesInvoice) -> float:
    allocated = (
        session.query(BankAllocation)
        .filter(BankAllocation.sales_invoice_id == invoice.id)
        .with_entities(BankAllocation.amount).all()
    )
    return round((invoice.grand_total or 0) - sum(a[0] or 0 for a in allocated), 2)


def open_purchase_invoices(
    session: Session, company_id: int, supplier_id: Optional[int] = None
) -> List[Tuple[Invoice, float]]:
    query = session.query(Invoice).filter(
        Invoice.company_id == company_id, Invoice.status.notin_(CLOSED_STATUSES)
    )
    if supplier_id:
        query = query.filter(Invoice.supplier_id == supplier_id)
    result = []
    for invoice in query.all():
        outstanding = purchase_outstanding(session, invoice)
        if outstanding > 0.5:
            result.append((invoice, outstanding))
    return result


def open_sales_invoices(
    session: Session, company_id: int, customer_id: Optional[int] = None
) -> List[Tuple[SalesInvoice, float]]:
    query = session.query(SalesInvoice).filter(
        SalesInvoice.company_id == company_id, SalesInvoice.status.notin_(CLOSED_STATUSES)
    )
    if customer_id:
        query = query.filter(SalesInvoice.customer_id == customer_id)
    result = []
    for invoice in query.all():
        outstanding = sales_outstanding(session, invoice)
        if outstanding > 0.5:
            result.append((invoice, outstanding))
    return result


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
def import_bank_statement(
    session: Session, company: Company, user: User,
    bank_account_name: str, filename: str, rows: List[BankRow],
) -> Tuple[List[BankTransaction], int]:
    """Persist parsed rows as BankTransaction, skipping ones already imported
    (same bank account + row_hash). Returns (new_transactions, skipped_count)."""
    account = find_or_create_bank_account(session, company.id, bank_account_name)

    existing_hashes = {
        h for (h,) in session.query(BankTransaction.row_hash)
        .filter(BankTransaction.bank_account_id == account.id).all()
    }

    created: List[BankTransaction] = []
    skipped = 0
    for row in rows:
        if row.row_hash in existing_hashes:
            skipped += 1
            continue
        transaction = BankTransaction(
            company_id=company.id, bank_account_id=account.id,
            txn_date=row.txn_date, narration=row.narration[:2000],
            reference_no=row.reference_no[:80], debit=row.debit, credit=row.credit,
            balance_after=row.balance, status="UNMATCHED", row_hash=row.row_hash,
            source_file=filename[:255], created_by=user.id,
        )
        suggest_match(session, company.id, transaction)
        session.add(transaction)
        created.append(transaction)
        existing_hashes.add(row.row_hash)

    session.flush()
    audit_service.record(
        session, company.id, "BANK_STATEMENT_IMPORTED",
        f"Imported {len(created)} new line(s) from '{filename}' into {account.name} "
        f"({skipped} already-seen line(s) skipped)",
        user=user,
    )
    return created, skipped


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def _name_score(narration_norm: str, name: str) -> float:
    name_norm = _normalize(name)
    if not name_norm:
        return 0.0
    if name_norm in narration_norm:
        return 0.9
    best = 0.0
    for token in name_norm.split():
        if len(token) < 4:
            continue
        for chunk in narration_norm.split():
            best = max(best, SequenceMatcher(None, token, chunk).ratio())
    return best * 0.75 if best > 0.8 else 0.0


def suggest_match(session: Session, company_id: int, transaction: BankTransaction) -> None:
    """Best-effort suggestion only — never final. Sets match_* fields and
    status; the review screen lets the user accept, change, or reject it."""
    narration_norm = _normalize(transaction.narration)
    is_debit = (transaction.debit or 0) > (transaction.credit or 0)
    amount = transaction.debit if is_debit else transaction.credit

    if is_debit:
        parties = session.query(Supplier).filter(Supplier.company_id == company_id).all()
        party_type = "supplier"
    else:
        parties = session.query(Customer).filter(Customer.company_id == company_id).all()
        party_type = "customer"

    best_party, best_score = None, 0.0
    for party in parties:
        score = _name_score(narration_norm, party.name or "")
        if score > best_score:
            best_party, best_score = party, score

    if not best_party or best_score < 0.5:
        transaction.match_confidence = 0.0
        transaction.match_reason = "No confident match — pick a party or ledger manually."
        return

    transaction.match_party_type = party_type
    transaction.match_party_id = best_party.id
    transaction.match_confidence = round(best_score, 2)
    transaction.match_reason = f"Narration mentions '{best_party.name}'."
    transaction.voucher_type = "Payment" if is_debit else "Receipt"

    if is_debit:
        candidates = open_purchase_invoices(session, company_id, best_party.id)
    else:
        candidates = open_sales_invoices(session, company_id, best_party.id)
    close = [c for c in candidates if abs(c[1] - amount) < 1.0]
    if len(close) == 1:
        invoice, outstanding = close[0]
        number = getattr(invoice, "invoice_number", "") or f"#{invoice.id}"
        transaction.match_confidence = min(0.97, transaction.match_confidence + 0.1)
        transaction.match_reason += f" Amount matches open invoice {number} exactly."


# --------------------------------------------------------------------------
# Manual match / allocation
# --------------------------------------------------------------------------
def set_manual_match(
    session: Session, transaction: BankTransaction,
    party_type: str, party_id: Optional[int] = None, ledger_name: str = "",
) -> None:
    if party_type not in ("supplier", "customer", "ledger"):
        raise BankError("party_type must be 'supplier', 'customer', or 'ledger'.")
    transaction.match_party_type = party_type
    transaction.match_party_id = party_id
    transaction.ledger_name = ledger_name[:200] if party_type == "ledger" else None
    is_debit = (transaction.debit or 0) > (transaction.credit or 0)
    transaction.voucher_type = "Payment" if is_debit else "Receipt"
    transaction.match_confidence = 1.0
    transaction.match_reason = "Manually matched by user."
    session.commit()


def allocate(
    session: Session, transaction: BankTransaction, allocations: List[dict],
) -> Tuple[bool, str]:
    """allocations: [{"invoice_kind": "purchase"|"sales", "invoice_id": int, "amount": float}]"""
    amount = transaction.debit or transaction.credit or 0.0
    total_alloc = sum(float(a.get("amount") or 0) for a in allocations)
    if total_alloc - amount > 0.5:
        return False, f"Allocated Rs.{total_alloc:,.2f} exceeds the transaction amount Rs.{amount:,.2f}."

    session.query(BankAllocation).filter(
        BankAllocation.bank_transaction_id == transaction.id
    ).delete()
    for alloc in allocations:
        amt = float(alloc.get("amount") or 0)
        if amt <= 0:
            continue
        row = BankAllocation(bank_transaction_id=transaction.id, amount=amt)
        if alloc.get("invoice_kind") == "purchase":
            row.invoice_id = alloc.get("invoice_id")
        else:
            row.sales_invoice_id = alloc.get("invoice_id")
        session.add(row)

    transaction.status = "MATCHED"
    session.commit()
    return True, "Allocation saved."


def ignore_transaction(
    session: Session, company: Company, user: User, transaction: BankTransaction, reason: str
) -> None:
    transaction.status = "IGNORED"
    session.commit()
    audit_service.record(
        session, company.id, "BANK_TXN_IGNORED",
        f"{transaction.txn_date} - {transaction.narration[:80]} - {reason}",
        user=user,
    )


# --------------------------------------------------------------------------
# Tally payload + posting
# --------------------------------------------------------------------------
def bank_idempotency_key(company_id: int, transaction: BankTransaction) -> str:
    return hashlib.sha256(
        f"bank|{company_id}|{transaction.row_hash}".encode()
    ).hexdigest()[:40]


def _party_ledger_name(session: Session, transaction: BankTransaction) -> str:
    if transaction.match_party_type == "supplier" and transaction.match_party_id:
        supplier = session.query(Supplier).filter(Supplier.id == transaction.match_party_id).one_or_none()
        if supplier:
            return supplier.tally_ledger_name or supplier.name
    if transaction.match_party_type == "customer" and transaction.match_party_id:
        customer = session.query(Customer).filter(Customer.id == transaction.match_party_id).one_or_none()
        if customer:
            return customer.tally_ledger_name or customer.name
    return transaction.ledger_name or "Suspense Account"


def build_bank_voucher_payload(
    session: Session, company: Company, transaction: BankTransaction
) -> LedgerVoucherPayload:
    account = session.query(BankAccount).filter(BankAccount.id == transaction.bank_account_id).one()
    is_payment = (transaction.debit or 0) > (transaction.credit or 0)
    amount = transaction.debit if is_payment else transaction.credit
    party_ledger = _party_ledger_name(session, transaction)

    bill_allocations = []
    for alloc in transaction.allocations:
        if alloc.invoice_id:
            invoice = session.query(Invoice).filter(Invoice.id == alloc.invoice_id).one_or_none()
            number = invoice.invoice_number if invoice else f"#{alloc.invoice_id}"
        elif alloc.sales_invoice_id:
            invoice = session.query(SalesInvoice).filter(SalesInvoice.id == alloc.sales_invoice_id).one_or_none()
            number = invoice.invoice_number if invoice else f"#{alloc.sales_invoice_id}"
        else:
            continue
        bill_allocations.append({"invoice_number": number or "", "amount": alloc.amount or 0})

    return LedgerVoucherPayload(
        company_name=company.tally_company_name or settings.TALLY_COMPANY or company.name,
        voucher_type=get_mapping(
            session, company.id,
            "bank_payment_voucher_type" if is_payment else "bank_receipt_voucher_type",
            "Payment" if is_payment else "Receipt",
        ),
        direction="payment" if is_payment else "receipt",
        bank_ledger=account.tally_ledger_name or account.name,
        party_ledger=party_ledger,
        amount=amount or 0.0,
        voucher_date=transaction.txn_date or dt.date.today().isoformat(),
        narration=(
            f"Auto-prepared by {settings.APP_NAME} from bank statement: {transaction.narration}"
        )[:500],
        reference_id=bank_idempotency_key(company.id, transaction),
        voucher_number=transaction.reference_no or "",
        bill_allocations=bill_allocations,
    )


def post_bank_transaction(
    session: Session, company: Company, user: User, transaction: BankTransaction,
    connector: TallyConnector, run_preflight: bool = True,
) -> TallyResult:
    if not user.can_post_to_tally:
        return TallyResult(False, "Your role does not permit posting to Tally.")
    if transaction.status == "POSTED":
        return TallyResult(False, "This transaction has already been posted.", mode=transaction.mode or "")
    if transaction.status not in ("MATCHED",):
        return TallyResult(False, f"Transaction is '{transaction.status}'. Match and allocate it first.")

    payload = build_bank_voucher_payload(session, company, transaction)

    if run_preflight:
        problems = connector.preflight(payload)
        if problems:
            audit_service.record(
                session, company.id, "BANK_TALLY_PREFLIGHT_FAILED",
                "; ".join(problems)[:2000], user=user,
            )
            return TallyResult(
                False,
                "Tally pre-check failed - nothing was posted:\n- " + "\n- ".join(problems),
            )

    audit_service.record(
        session, company.id, "BANK_TALLY_POST_ATTEMPTED",
        f"{payload.voucher_type} voucher for {transaction.txn_date} - "
        f"{transaction.narration[:80]} via {connector.name}",
        user=user,
    )

    try:
        result = connector.post_voucher(payload)
    except Exception as exc:
        audit_service.record_error(session, company.id, "tally", f"Posting raised: {exc}", exc)
        return TallyResult(False, f"Tally posting failed: {exc}")

    if not result.success:
        audit_service.record(
            session, company.id, "BANK_TALLY_POST_FAILED", result.message[:2000], user=user,
        )
        return result

    transaction.mode = result.mode
    transaction.tally_voucher_id = result.voucher_id or ""
    transaction.request_xml = (result.request_xml or "")[:200000]
    transaction.tally_response = (result.response_xml or result.message)[:20000]
    transaction.export_path = result.export_path or ""
    transaction.status = "POSTED" if result.mode in ("POSTED", "MOCK", "EXCEL") else "MATCHED"
    if transaction.status == "POSTED":
        transaction.posted_at = dt.datetime.now(dt.timezone.utc)
    session.commit()

    audit_service.record(
        session, company.id, "BANK_TALLY_POST_SUCCESS",
        f"{result.message} | mode={result.mode}", user=user,
    )
    if result.mode == "XML_EXPORT":
        result.warnings.append(
            "Import this file in Tally to complete the entry. This transaction stays "
            "'Matched' (not Posted) until it is confirmed as imported."
        )
    return result
