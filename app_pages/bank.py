"""
Bank: import a bank statement (Excel/CSV, or a photo/PDF), match each line
against an open Purchase/Sales invoice or a general ledger, then post a
Payment or Receipt voucher to Tally. No stock is affected by anything here.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import streamlit as st

from app_pages.upload_bill import build_parser, first_load_notice
from database.database import get_setting
from database.models import AuditLog, BankAccount, BankTransaction, Customer, Supplier
from services.bank_service import (
    allocate,
    build_bank_voucher_payload,
    ignore_transaction,
    import_bank_statement,
    open_purchase_invoices,
    open_sales_invoices,
    post_bank_transaction,
    set_manual_match,
)
from services.bank_statement_import import ExcelImportError, parse_bank_statement, sample_statement_bytes
from services.bank_statement_parser import BankStatementParseError, BankStatementParser
from services.ocr_service import PROVIDER_LABELS
from services.tally_connector import build_xml_for_payload, get_connector
from utils.config import settings
from utils.image_tools import to_images
from utils.security import safe_filename, validate_upload
from utils.ui import brand_header, friendly_error, require_login, rupees, session_db, test_mode_banner

OPEN_STATUSES = ["UNMATCHED", "MATCHED"]
ALL_STATUSES = ["UNMATCHED", "MATCHED", "POSTED", "IGNORED"]


def render() -> None:
    user, company = require_login()
    db = session_db()
    brand_header(f"{company.name} · Bank")
    st.caption(
        "Import a bank statement, match each line to a supplier/customer bill "
        "or a general ledger, then post a Payment or Receipt voucher to Tally. "
        "No stock is affected by anything here."
    )

    tab_upload, tab_match, tab_history = st.tabs(
        ["📤 Upload statement", "🔗 Match & Post", "📚 History"]
    )
    with tab_upload:
        _upload_tab(db, company, user)
    with tab_match:
        _match_tab(db, company, user)
    with tab_history:
        _history_tab(db, company)


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
def _account_picker(db, company, key_prefix: str) -> str:
    existing = (
        db.query(BankAccount)
        .filter(BankAccount.company_id == company.id, BankAccount.is_active.is_(True))
        .order_by(BankAccount.name).all()
    )
    names = [a.name for a in existing]
    if names:
        choice = st.selectbox(
            "Bank account", names + ["+ New bank account"], key=f"{key_prefix}_acc_choice"
        )
        return (
            st.text_input("New bank account name", key=f"{key_prefix}_acc_new")
            if choice == "+ New bank account" else choice
        )
    return st.text_input(
        "Bank account name (e.g. 'HDFC Current A/c')", key=f"{key_prefix}_acc_new_only"
    )


def _show_import_result(created, skipped) -> None:
    st.success(
        f"Imported {len(created)} new line(s)"
        + (f", {skipped} already seen and skipped." if skipped else ".")
    )
    if not created:
        return
    matched = sum(1 for t in created if t.match_party_type)
    st.caption(
        f"{matched} of {len(created)} line(s) got a suggested match — "
        "check them in **Match & Post**. Nothing has been posted to Tally yet."
    )
    st.dataframe(pd.DataFrame([
        {
            "Date": t.txn_date, "Narration": t.narration[:60],
            "Debit": t.debit, "Credit": t.credit,
            "Suggested match": t.match_reason or "—",
            "Confidence": f"{(t.match_confidence or 0):.0%}",
        }
        for t in created
    ]), hide_index=True, width="stretch")


def _upload_tab(db, company, user) -> None:
    sub_excel, sub_photo = st.tabs(["📊 Excel / CSV", "📷 Photo / PDF"])
    with sub_excel:
        _excel_upload(db, company, user)
    with sub_photo:
        _photo_upload(db, company, user)


def _excel_upload(db, company, user) -> None:
    st.download_button(
        "⬇️ Download sample statement format",
        data=sample_statement_bytes(),
        file_name="bank_statement_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="bank_template_dl",
    )
    account_name = _account_picker(db, company, key_prefix="bank_excel")
    file = st.file_uploader(
        "Bank statement (XLSX, XLS or CSV)", type=["xlsx", "xls", "csv"], key="bank_stmt_uploader",
    )
    if not file or not account_name.strip():
        st.info("Pick/name a bank account and upload a statement file.")
        return

    raw = file.getvalue()
    filename = safe_filename(file.name)
    st.markdown(f"**{filename}** · {len(raw)/1024:.0f} KB → **{account_name}**")

    if not st.button("Import", type="primary", key="bank_import_btn"):
        return
    try:
        rows = parse_bank_statement(raw, filename)
    except ExcelImportError as exc:
        st.error(f"**Could not read this statement.**\n\n{exc}")
        return
    except Exception as exc:
        friendly_error("Bank statement import", exc)
        return

    created, skipped = import_bank_statement(db, company, user, account_name.strip(), filename, rows)
    _show_import_result(created, skipped)


def _photo_upload(db, company, user) -> None:
    parser_ocr, provider_name = build_parser(db, company)
    parser = BankStatementParser(parser_ocr.provider)
    st.caption(
        f"Extraction engine: **{PROVIDER_LABELS.get(provider_name, provider_name)}** "
        "(same engine as Purchase/Sales — change it under Settings → AI / OCR). "
        "A text-only engine (Tesseract/PaddleOCR) can't read a statement's table "
        "layout — use an AI vision engine, or upload as Excel/CSV instead."
    )
    first_load_notice(provider_name)
    if provider_name == "mock":
        st.warning(
            "The mock engine is active — it returns fixed sample transactions for "
            "testing, not a real reading of your statement.", icon="🧪",
        )

    account_name = _account_picker(db, company, key_prefix="bank_photo")
    files = st.file_uploader(
        "Bank statement (JPG, PNG, WEBP or PDF — multiple pages allowed)",
        type=["jpg", "jpeg", "png", "webp", "pdf"],
        accept_multiple_files=True,
        key="bank_photo_uploader",
    )
    if not files or not account_name.strip():
        st.info("Pick/name a bank account and upload the statement photo(s)/PDF.")
        return

    if st.button("1️⃣ Extract transactions", type="primary", key="bank_photo_extract_btn"):
        all_pages = []
        combined_names = []
        for uploaded in files:
            data = uploaded.getvalue()
            ok, message = validate_upload(uploaded.name, data)
            if not ok:
                st.error(f"{uploaded.name}: {message}")
                continue
            try:
                pages = to_images(uploaded.name, data)
            except Exception as exc:
                st.error(f"{uploaded.name}: could not read the file ({exc}).")
                continue
            all_pages.extend(pages)
            combined_names.append(uploaded.name)

        if not all_pages:
            st.error("No readable pages found.")
            return

        with st.spinner(f"Reading {len(all_pages)} page(s)…"):
            try:
                result = parser.parse_images(all_pages)
            except BankStatementParseError as exc:
                st.error(f"**Could not extract this statement.**\n\n{exc}")
                st.session_state.pop("bank_photo_extracted", None)
                return
            except Exception as exc:
                friendly_error("Bank statement extraction", exc)
                st.session_state.pop("bank_photo_extracted", None)
                return

        if not result.rows:
            st.warning("No transaction rows were found on the uploaded page(s).")
            st.session_state.pop("bank_photo_extracted", None)
            return

        # Stashed in session_state — clicking the Import button below triggers
        # a full script rerun, and this block (guarded by the Extract button)
        # would otherwise not re-run, losing the extracted rows before the
        # import ever happened.
        st.session_state["bank_photo_extracted"] = {
            "rows": result.rows,
            "warnings": list(result.warnings),
            "overall_confidence": result.overall_confidence,
            "duration_ms": result.duration_ms,
            "account_name": account_name.strip(),
            "filename": safe_filename(", ".join(combined_names))[:255],
        }

    extracted = st.session_state.get("bank_photo_extracted")
    if not extracted:
        return

    for warning in extracted["warnings"]:
        st.info(warning)
    st.caption(
        f"Extracted {len(extracted['rows'])} row(s) at "
        f"{extracted['overall_confidence']:.0%} overall confidence in "
        f"{extracted['duration_ms']} ms. Review the rows below, then import them."
    )
    st.dataframe(pd.DataFrame([
        {
            "Date": r.txn_date, "Narration": r.narration[:70],
            "Ref": r.reference_no, "Debit": r.debit, "Credit": r.credit, "Balance": r.balance,
        }
        for r in extracted["rows"]
    ]), hide_index=True, width="stretch")

    if st.button("2️⃣ Import these rows", type="primary", key="bank_photo_confirm_import"):
        created, skipped = import_bank_statement(
            db, company, user, extracted["account_name"], extracted["filename"], extracted["rows"]
        )
        st.session_state.pop("bank_photo_extracted", None)
        _show_import_result(created, skipped)


# --------------------------------------------------------------------------
# Match & Post
# --------------------------------------------------------------------------
def _match_tab(db, company, user) -> None:
    open_txns = (
        db.query(BankTransaction)
        .filter(BankTransaction.company_id == company.id, BankTransaction.status.in_(OPEN_STATUSES))
        .order_by(BankTransaction.txn_date.desc()).all()
    )
    if not open_txns:
        st.info("Nothing waiting to be matched. Upload a statement first.")
        return

    accounts = {a.id: a.name for a in db.query(BankAccount).filter(BankAccount.company_id == company.id).all()}
    ids = [t.id for t in open_txns]
    default_id = st.session_state.get("bank_selected_txn")
    index = ids.index(default_id) if default_id in ids else 0
    chosen_id = st.selectbox(
        "Open a transaction",
        ids, index=index,
        format_func=lambda i: next(
            f"{x.txn_date} · {accounts.get(x.bank_account_id,'')} · "
            f"{'Dr' if x.debit else 'Cr'} {rupees(x.debit or x.credit)} · {x.narration[:40]} ({x.status})"
            for x in open_txns if x.id == i
        ),
        key="bank_txn_select",
    )
    st.session_state.bank_selected_txn = chosen_id
    txn = next(t for t in open_txns if t.id == chosen_id)
    _render_transaction(db, company, user, txn)


def _render_transaction(db, company, user, txn: BankTransaction) -> None:
    is_debit = (txn.debit or 0) > (txn.credit or 0)
    amount = txn.debit if is_debit else txn.credit

    st.markdown(f"### {txn.txn_date} · {'Payment (money out)' if is_debit else 'Receipt (money in)'}")
    cols = st.columns(3)
    cols[0].metric("Amount", rupees(amount))
    cols[1].metric("Reference", txn.reference_no or "—")
    cols[2].metric("Status", txn.status)
    st.text_area("Narration", txn.narration, disabled=True, height=68)

    if txn.match_reason:
        st.info(f"Suggested: {txn.match_reason} (confidence {(txn.match_confidence or 0):.0%})")

    suppliers = db.query(Supplier).filter(Supplier.company_id == company.id).order_by(Supplier.name).all()
    customers = db.query(Customer).filter(Customer.company_id == company.id).order_by(Customer.name).all()

    party_kind = st.radio(
        "Match this to",
        ["Supplier", "Customer", "General ledger (expense/income)"],
        horizontal=True,
        index=(0 if txn.match_party_type == "supplier" else 1 if txn.match_party_type == "customer" else 2),
        key=f"bank_kind_{txn.id}",
    )

    allocations_payload = []
    if party_kind == "Supplier":
        options = {s.name: s.id for s in suppliers}
        if not options:
            st.warning("No suppliers yet — add one in Purchase, or use a general ledger instead.")
            return
        default_name = next((n for n, i in options.items() if i == txn.match_party_id), list(options)[0])
        chosen_name = st.selectbox(
            "Supplier", list(options), index=list(options).index(default_name), key=f"bank_sup_{txn.id}"
        )
        supplier_id = options[chosen_name]
        candidates = open_purchase_invoices(db, company.id, supplier_id)
        allocations_payload = _allocation_editor(txn, candidates, "purchase", key=f"bank_alloc_p_{txn.id}")
        if st.button("💾 Save match", key=f"bank_save_sup_{txn.id}"):
            set_manual_match(db, txn, "supplier", party_id=supplier_id)
            ok, msg = allocate(db, txn, allocations_payload)
            (st.success if ok else st.error)(msg)
            if ok:
                st.rerun()

    elif party_kind == "Customer":
        options = {c.name: c.id for c in customers}
        if not options:
            st.warning("No customers yet — add one in Sales, or use a general ledger instead.")
            return
        default_name = next((n for n, i in options.items() if i == txn.match_party_id), list(options)[0])
        chosen_name = st.selectbox(
            "Customer", list(options), index=list(options).index(default_name), key=f"bank_cus_{txn.id}"
        )
        customer_id = options[chosen_name]
        candidates = open_sales_invoices(db, company.id, customer_id)
        allocations_payload = _allocation_editor(txn, candidates, "sales", key=f"bank_alloc_s_{txn.id}")
        if st.button("💾 Save match", key=f"bank_save_cus_{txn.id}"):
            set_manual_match(db, txn, "customer", party_id=customer_id)
            ok, msg = allocate(db, txn, allocations_payload)
            (st.success if ok else st.error)(msg)
            if ok:
                st.rerun()

    else:
        ledger_name = st.text_input(
            "Ledger name (as it exists in Tally, e.g. 'Bank Charges', 'Salary Account')",
            value=txn.ledger_name or "", key=f"bank_ledger_{txn.id}",
        )
        if st.button("💾 Save match", key=f"bank_save_ledger_{txn.id}", disabled=not ledger_name.strip()):
            set_manual_match(db, txn, "ledger", ledger_name=ledger_name.strip())
            allocate(db, txn, [])
            st.success("Saved.")
            st.rerun()

    st.divider()
    c1, c2 = st.columns([1, 3])
    if c1.button("🚫 Ignore this line", key=f"bank_ignore_{txn.id}"):
        st.session_state[f"bank_confirm_ignore_{txn.id}"] = True
    if st.session_state.get(f"bank_confirm_ignore_{txn.id}"):
        reason = c2.text_input("Reason (e.g. 'internal transfer')", key=f"bank_ignore_reason_{txn.id}")
        if c2.button("Yes, ignore it", key=f"bank_do_ignore_{txn.id}"):
            ignore_transaction(db, company, user, txn, reason or "not a trade transaction")
            st.session_state[f"bank_confirm_ignore_{txn.id}"] = False
            st.rerun()

    if txn.status == "MATCHED":
        st.divider()
        _tally_section(db, company, user, txn)


def _allocation_editor(txn, candidates, invoice_kind: str, key: str) -> list:
    amount = txn.debit or txn.credit or 0.0
    if not candidates:
        st.caption(
            "No open invoices for this party — this will post as an on-account "
            "payment (no bill reference)."
        )
        return []

    frame = pd.DataFrame([
        {
            "invoice_id": invoice.id,
            "Invoice": getattr(invoice, "invoice_number", "") or f"#{invoice.id}",
            "Outstanding": outstanding,
            "Allocate": outstanding if len(candidates) == 1 and abs(outstanding - amount) < 1.0 else 0.0,
        }
        for invoice, outstanding in candidates
    ])
    edited = st.data_editor(
        frame,
        column_config={
            "invoice_id": None,
            "Outstanding": st.column_config.NumberColumn(disabled=True),
        },
        hide_index=True, width="stretch", key=key,
    )
    total = edited["Allocate"].sum()
    st.caption(f"Allocated: {rupees(total)} of {rupees(amount)}")
    return [
        {"invoice_kind": invoice_kind, "invoice_id": int(row["invoice_id"]), "amount": float(row["Allocate"] or 0)}
        for _, row in edited.iterrows() if row["Allocate"]
    ]


def _tally_section(db, company, user, txn: BankTransaction) -> None:
    mode = get_setting(db, company.id, "tally_mode", "mock")
    test_mode = get_setting(db, company.id, "tally_test_mode", str(settings.TALLY_TEST_MODE))
    is_test = str(test_mode).lower() in ("1", "true", "yes", "on")
    connector = get_connector(mode, {
        "host": get_setting(db, company.id, "tally_host", settings.TALLY_HOST),
        "port": get_setting(db, company.id, "tally_port", settings.TALLY_PORT),
        "company": company.tally_company_name or settings.TALLY_COMPANY,
        "test_mode": is_test,
    })

    st.markdown("##### Tally voucher")
    test_mode_banner(is_test, f"Connector: {connector.name}")

    payload = build_bank_voucher_payload(db, company, txn)
    preview = st.columns(4)
    preview[0].metric("Voucher type", payload.voucher_type)
    preview[1].metric("Bank ledger", payload.bank_ledger[:20])
    preview[2].metric("Party ledger", payload.party_ledger[:20])
    preview[3].metric("Amount", rupees(payload.amount))

    with st.expander("Exact XML that will be sent to Tally"):
        st.code(build_xml_for_payload(payload), language="xml")

    cols = st.columns(3)
    if cols[0].button("🔎 Run Tally pre-check", key=f"bank_preflight_{txn.id}"):
        with st.spinner("Checking company and ledgers…"):
            problems = connector.preflight(payload)
        if problems:
            st.error("Pre-check found problems:\n\n- " + "\n- ".join(problems))
        else:
            st.success("All referenced masters exist. Safe to post.")

    if cols[1].button("📄 Generate XML file only", key=f"bank_genxml_{txn.id}"):
        path = connector.export_xml(payload)
        st.success(f"Saved to `{path}`")
        st.download_button(
            "Download XML", data=Path(path).read_bytes(), file_name=Path(path).name,
            mime="application/xml", key=f"bank_dlxml_{txn.id}",
        )

    if cols[2].button(
        "🚀 POST TO TALLY", type="primary", disabled=not user.can_post_to_tally, key=f"bank_post_{txn.id}",
    ):
        with st.spinner("Posting…"):
            result = post_bank_transaction(db, company, user, txn, connector)
        if result.success:
            st.success(result.message)
            for warning in result.warnings:
                st.info(warning)
            st.rerun()
        else:
            st.error(result.message)

    if not user.can_post_to_tally:
        st.caption("Your role can review and match, but not post. Ask a Manager or Admin.")


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------
def _history_tab(db, company) -> None:
    txns = (
        db.query(BankTransaction)
        .filter(BankTransaction.company_id == company.id)
        .order_by(BankTransaction.txn_date.desc()).all()
    )
    if not txns:
        st.info("No bank transactions imported yet.")
        return

    accounts = {a.id: a.name for a in db.query(BankAccount).filter(BankAccount.company_id == company.id).all()}

    with st.expander("Filters", expanded=False):
        col1, col2 = st.columns(2)
        statuses = col1.multiselect("Status", ALL_STATUSES, default=[], key="bank_hist_status")
        search = col2.text_input("Search narration", key="bank_hist_search")

    def keep(t: BankTransaction) -> bool:
        if statuses and t.status not in statuses:
            return False
        if search.strip() and search.strip().lower() not in (t.narration or "").lower():
            return False
        return True

    filtered = [t for t in txns if keep(t)]
    st.caption(f"{len(filtered)} of {len(txns)} transaction(s) match.")

    frame = pd.DataFrame([
        {
            "Date": t.txn_date, "Bank": accounts.get(t.bank_account_id, ""),
            "Narration": (t.narration or "")[:60],
            "Debit": t.debit, "Credit": t.credit, "Status": t.status,
            "Voucher": t.voucher_type or "—", "Mode": t.mode or "—",
        }
        for t in filtered
    ])
    st.dataframe(frame, hide_index=True, width="stretch", height=380)
    st.download_button(
        "⬇️ Export this view (CSV)", data=frame.to_csv(index=False).encode(),
        file_name=f"bank_history_{dt.date.today()}.csv", mime="text/csv", key="bank_hist_csv",
    )

    st.divider()
    st.markdown("#### Recent bank activity")
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.company_id == company.id, AuditLog.action.like("BANK_%"))
        .order_by(AuditLog.created_at.desc()).limit(100).all()
    )
    if logs:
        for entry in logs:
            st.markdown(
                f"- `{entry.created_at:%d-%m-%Y %H:%M}` **{entry.action}** — "
                f"{entry.detail or ''} <span class='muted'>({entry.user_name or 'system'})</span>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("No bank activity logged yet.")
