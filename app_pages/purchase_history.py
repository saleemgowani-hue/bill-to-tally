"""Searchable bill history with filters and per-bill audit trail."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import func

from database.models import (
    AuditLog,
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    PurchaseTransaction,
    User,
)
from utils.ui import (
    brand_header,
    require_login,
    rupees,
    session_db,
    status_badge,
)

ALL_STATUSES = [s.value for s in InvoiceStatus]


def render() -> None:
    user, company = require_login()
    db = session_db()
    brand_header(f"{company.name} · Purchase History")

    invoices = (
        db.query(Invoice)
        .filter(Invoice.company_id == company.id)
        .order_by(Invoice.created_at.desc())
        .all()
    )
    if not invoices:
        st.info("No bills processed yet.")
        return

    users = {u.id: u.full_name for u in db.query(User).filter(User.company_id == company.id).all()}
    transactions = {
        t.invoice_id: t
        for t in db.query(PurchaseTransaction)
        .filter(PurchaseTransaction.company_id == company.id)
        .all()
    }

    with st.expander("Filters", expanded=True):
        col1, col2, col3, col4 = st.columns(4)
        statuses = col1.multiselect("Status", ALL_STATUSES, default=[])
        suppliers = sorted({i.supplier_name for i in invoices if i.supplier_name})
        supplier_filter = col2.multiselect("Supplier", suppliers, default=[])
        date_from = col3.date_input(
            "From", value=dt.date.today() - dt.timedelta(days=90), key="hist_from"
        )
        date_to = col4.date_input("To", value=dt.date.today(), key="hist_to")
        col5, col6, col7 = st.columns(3)
        min_amount = col5.number_input("Min amount", 0.0, step=100.0)
        max_amount = col6.number_input("Max amount (0 = no limit)", 0.0, step=100.0)
        search = col7.text_input("Search invoice no. / supplier")

    def keep(invoice: Invoice) -> bool:
        if statuses and invoice.status not in statuses:
            return False
        if supplier_filter and invoice.supplier_name not in supplier_filter:
            return False
        reference = invoice.invoice_date or (
            invoice.created_at.date().isoformat() if invoice.created_at else ""
        )
        try:
            parsed = dt.date.fromisoformat(reference)
            if parsed < date_from or parsed > date_to:
                return False
        except (ValueError, TypeError):
            pass
        total = invoice.grand_total or 0
        if min_amount and total < min_amount:
            return False
        if max_amount and total > max_amount:
            return False
        if search.strip():
            needle = search.strip().lower()
            haystack = f"{invoice.invoice_number or ''} {invoice.supplier_name or ''}".lower()
            if needle not in haystack:
                return False
        return True

    filtered = [i for i in invoices if keep(i)]
    st.caption(f"{len(filtered)} of {len(invoices)} bill(s) match.")

    item_counts = dict(
        db.query(InvoiceItem.invoice_id, func.count(InvoiceItem.id))
        .filter(InvoiceItem.company_id == company.id)
        .group_by(InvoiceItem.invoice_id)
        .all()
    )

    frame = pd.DataFrame(
        [
            {
                "ID": i.id,
                "Date": i.invoice_date or "",
                "Supplier": i.supplier_name or "",
                "Invoice no.": i.invoice_number or "",
                "Items": item_counts.get(i.id, 0),
                "Amount": i.grand_total or 0,
                "Status": i.status,
                "Tally": (transactions[i.id].mode if i.id in transactions else "—"),
                "User": users.get(i.created_by, ""),
                "Created at": i.created_at.strftime("%d-%m-%Y %H:%M") if i.created_at else "",
            }
            for i in filtered
        ]
    )
    if frame.empty:
        st.info("No bills match these filters.")
        return

    st.dataframe(frame, hide_index=True, width="stretch", height=420)
    st.download_button(
        "⬇️ Export this view (CSV)",
        data=frame.to_csv(index=False).encode(),
        file_name=f"purchase_history_{dt.date.today()}.csv",
        mime="text/csv",
    )

    st.divider()
    chosen = st.selectbox(
        "Open a bill",
        [i.id for i in filtered],
        format_func=lambda i: f"#{i} · "
        + next(
            f"{x.supplier_name or 'Unknown'} / {x.invoice_number or 'no number'}"
            for x in filtered
            if x.id == i
        ),
    )
    _detail(db, company, chosen, transactions.get(chosen), users)


def _detail(db, company, invoice_id: int, transaction, users):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).one()
    items = (
        db.query(InvoiceItem)
        .filter(InvoiceItem.invoice_id == invoice.id)
        .order_by(InvoiceItem.id)
        .all()
    )

    st.markdown(
        f"### #{invoice.id} · {invoice.supplier_name or 'Unknown'} "
        f"&nbsp;{status_badge(invoice.status)}",
        unsafe_allow_html=True,
    )
    cols = st.columns(5)
    cols[0].metric("Invoice no.", invoice.invoice_number or "—")
    cols[1].metric("Date", invoice.invoice_date or "—")
    cols[2].metric("Taxable", rupees(invoice.subtotal))
    cols[3].metric("GST", rupees(invoice.tax_total))
    cols[4].metric("Grand total", rupees(invoice.grand_total))

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Product": i.raw_name,
                    "Qty": i.quantity,
                    "Unit": i.unit,
                    "Rate": i.rate,
                    "Discount": i.discount,
                    "GST %": i.gst_rate,
                    "Taxable": i.taxable_value,
                    "Total": i.total,
                    "Match": i.match_status,
                }
                for i in items
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    if transaction:
        st.markdown("#### Tally transaction")
        cols = st.columns(4)
        cols[0].metric("Mode", transaction.mode or "—")
        cols[1].metric("Reference", (transaction.tally_voucher_id or "—")[:18])
        cols[2].metric("Stock applied", "Yes" if transaction.stock_applied else "No")
        cols[3].metric("Posted", transaction.created_at.strftime("%d-%m-%Y %H:%M")
                       if transaction.created_at else "—")
        exported = Path(transaction.export_path) if transaction.export_path else None
        if exported and exported.exists():
            # The file may be XML or a workbook depending on the mode that
            # produced it — label and serve it as what it actually is.
            is_excel = exported.suffix.lower() in (".xlsx", ".xlsm")
            st.download_button(
                "⬇️ Download the Excel workbook" if is_excel
                else "⬇️ Download the voucher XML file",
                data=exported.read_bytes(),
                file_name=exported.name,
                mime=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    if is_excel else "application/xml"
                ),
            )
        elif transaction.request_xml:
            st.download_button(
                "⬇️ Download the voucher XML",
                data=transaction.request_xml.encode("utf-8"),
                file_name=f"voucher_{invoice.invoice_number or invoice.id}.xml".replace("/", "-"),
                mime="application/xml",
            )
        if transaction.request_xml:
            with st.expander("Exact XML sent to Tally"):
                st.code(transaction.request_xml[:8000], language="xml")
        if transaction.tally_response:
            with st.expander("Tally response"):
                st.code(transaction.tally_response[:4000], language="xml")

    st.markdown("#### Export this bill")
    if st.button("📗 Build an Excel copy", key=f"xl_bill_{invoice.id}"):
        from services.excel_export import build_bill_workbook
        from services.purchase_service import build_voucher_payload

        try:
            payload = build_voucher_payload(db, company, invoice)
            path = build_bill_workbook(payload)
        except Exception as exc:
            st.error(f"Could not build the workbook: {exc}")
        else:
            st.session_state[f"xl_path_{invoice.id}"] = path

    built = st.session_state.get(f"xl_path_{invoice.id}")
    if built and Path(built).exists():
        st.download_button(
            "⬇️ Download Excel",
            data=Path(built).read_bytes(),
            file_name=Path(built).name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"xl_dl_{invoice.id}",
        )

    st.markdown("#### Audit trail")
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.company_id == company.id, AuditLog.invoice_id == invoice.id)
        .order_by(AuditLog.created_at.asc())
        .all()
    )
    if logs:
        for log in logs:
            st.markdown(
                f"- `{log.created_at:%d-%m-%Y %H:%M}` **{log.action}** — "
                f"{log.detail or ''} <span class='muted'>({log.user_name or 'system'})</span>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("No audit entries for this bill.")
