"""Reports: purchase summaries, GST breakdown, audit log and error log."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import pandas as pd
import streamlit as st

from database.models import (
    ACCEPTED_STATUSES,
    NEXT_STEP,
    RECORDED_STATUSES,
    AuditLog,
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    ProcessingError,
    Product,
)
from utils.ui import brand_header, require_login, rupees, session_db


def render() -> None:
    user, company = require_login()
    db = session_db()
    brand_header(f"{company.name} · Reports")

    (tab_excel, tab_purchase, tab_gst, tab_stock,
     tab_audit, tab_errors) = st.tabs(
        ["📗 Excel export", "Purchase summary", "GST summary",
         "Stock valuation", "Audit log", "Error log"]
    )

    with tab_excel:
        _excel_tab(db, company, user)

    with tab_purchase:
        _purchase_tab(db, company)
    with tab_gst:
        _gst_tab(db, company)
    with tab_stock:
        _stock_tab(db, company)
    with tab_audit:
        _audit_tab(db, company)
    with tab_errors:
        _errors_tab(db, company)


def _recorded_invoices(db, company):
    """Bills that count as entered: posted to Tally, or exported for import."""
    return (
        db.query(Invoice)
        .filter(
            Invoice.company_id == company.id,
            Invoice.status.in_(RECORDED_STATUSES),
        )
        .all()
    )


def _status_counts(db, company) -> dict:
    counts = defaultdict(int)
    for (status,) in db.query(Invoice.status).filter(
        Invoice.company_id == company.id
    ).all():
        counts[status or "Unknown"] += 1
    return dict(counts)


def _waiting_notice(db, company, *, empty: bool) -> None:
    """Say where the missing bills are, instead of just reporting nothing.

    "No posted bills yet" is true and useless: the customer uploaded a bill a
    minute ago and cannot tell whether the app lost it or is waiting for them.
    This names each bill's status and the button that moves it on.
    """
    counts = _status_counts(db, company)
    waiting = {
        status: n for status, n in counts.items()
        if status not in RECORDED_STATUSES and n
    }

    if empty and not counts:
        st.info("No bills uploaded yet. Add one from **Upload bill**.", icon="📭")
        return
    if empty:
        st.warning(
            f"**{sum(waiting.values())} bill(s) uploaded, none of them entered yet.** "
            "A bill joins the reports once it is confirmed and posted.",
            icon="⏳",
        )
    elif not waiting:
        return
    else:
        st.caption(
            f"{sum(waiting.values())} more bill(s) are not in this report yet:"
        )

    for status, count in sorted(waiting.items(), key=lambda kv: -kv[1]):
        step = NEXT_STEP.get(status, "see Purchase history")
        st.markdown(f"- **{count} × {status}** — {step}")



def _excel_tab(db, company, user):
    """Excel export, filtered however the customer files their paperwork.

    Three ways of slicing the same data: everything over a period, one
    supplier's bills, or specific bills. Plus a choice between one combined
    workbook and a separate file per bill.
    """
    from pathlib import Path

    from services.excel_export import build_bill_bundle, build_register_workbook

    st.markdown("#### Download your records as Excel")

    scope = st.radio(
        "What do you want to export?",
        ["Date range", "By supplier", "Pick specific bills"],
        horizontal=True,
        key="xl_scope",
    )

    all_invoices = (
        db.query(Invoice)
        .filter(Invoice.company_id == company.id)
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
        .all()
    )
    if not all_invoices:
        st.info("No bills recorded yet.", icon="📭")
        return

    today = dt.date.today()
    invoices = []

    if scope == "Date range":
        col1, col2, col3 = st.columns([1, 1, 1.4])
        start = col1.date_input("From", today.replace(day=1), key="xl_from")
        end = col2.date_input("To", today, key="xl_to")
        statuses = col3.multiselect(
            "Status", [s.value for s in InvoiceStatus],
            default=list(ACCEPTED_STATUSES),
            help="Bills you have confirmed. Add 'Review Required' to include "
                 "bills you have not checked yet.",
            key="xl_status",
        )
        if start > end:
            st.error("The 'From' date is after the 'To' date.")
            return
        invoices = [
            invoice for invoice in all_invoices
            if _within(invoice, start, end)
            and (not statuses or invoice.status in statuses)
        ]

    elif scope == "By supplier":
        names = sorted({i.supplier_name or "Unknown" for i in all_invoices})
        col1, col2 = st.columns([2, 1.4])
        chosen = col1.multiselect(
            "Supplier(s)", names,
            help="Leave empty for every supplier.",
            key="xl_suppliers",
        )
        statuses = col2.multiselect(
            "Status", [s.value for s in InvoiceStatus],
            default=list(ACCEPTED_STATUSES), key="xl_status_sup",
        )
        whole_period = st.checkbox(
            "All dates", value=True, key="xl_all_dates",
            help="Untick to limit these suppliers to a period.",
        )
        if whole_period:
            start, end = dt.date(1900, 1, 1), dt.date(2999, 12, 31)
        else:
            dcol1, dcol2 = st.columns(2)
            start = dcol1.date_input("From", today.replace(day=1), key="xl_sup_from")
            end = dcol2.date_input("To", today, key="xl_sup_to")
            if start > end:
                st.error("The 'From' date is after the 'To' date.")
                return
        invoices = [
            invoice for invoice in all_invoices
            if (not chosen or (invoice.supplier_name or "Unknown") in chosen)
            and (not statuses or invoice.status in statuses)
            and _within(invoice, start, end)
        ]

    else:  # Pick specific bills
        labels = {
            invoice.id: (
                f"{invoice.invoice_date or '—'} · {invoice.supplier_name or 'Unknown'} "
                f"· {invoice.invoice_number or 'no number'} · "
                f"{rupees(invoice.grand_total or 0)} · {invoice.status}"
            )
            for invoice in all_invoices
        }
        picked = st.multiselect(
            "Bills", list(labels.keys()),
            format_func=lambda key: labels[key],
            key="xl_bills",
        )
        invoices = [invoice for invoice in all_invoices if invoice.id in picked]

    if not invoices:
        st.info("Nothing matches those filters yet.", icon="📭")
        # Say what *is* there, so the customer can see whether to widen the
        # dates or tick another status rather than guessing.
        have = sorted({i.status for i in all_invoices if i.status})
        if have:
            st.caption(
                f"You have {len(all_invoices)} bill(s) in total, at: "
                + ", ".join(f"**{s}**" for s in have)
            )
        return

    metrics = st.columns(3)
    metrics[0].metric("Bills", len(invoices))
    metrics[1].metric(
        "Total value", rupees(sum(i.grand_total or 0 for i in invoices))
    )
    metrics[2].metric(
        "Suppliers", len({i.supplier_name or "Unknown" for i in invoices})
    )

    st.divider()
    layout = st.radio(
        "File layout",
        ["One combined workbook", "A separate file per bill (ZIP)"],
        key="xl_layout",
        help="The combined workbook has a register, item detail, GST and "
             "supplier summaries. The ZIP holds one sheet per bill.",
    )
    include_stock = True
    if layout == "One combined workbook":
        include_stock = st.checkbox(
            "Include the current stock position", value=True, key="xl_stock"
        )

    if st.button("📗 Build the file", type="primary"):
        try:
            if layout == "One combined workbook":
                items = {
                    invoice.id: db.query(InvoiceItem)
                    .filter(InvoiceItem.invoice_id == invoice.id)
                    .all()
                    for invoice in invoices
                }
                products = (
                    db.query(Product)
                    .filter(Product.company_id == company.id,
                            Product.is_active.is_(True))
                    .order_by(Product.name)
                    .all()
                    if include_stock else None
                )
                path = build_register_workbook(company, invoices, items, products)
            else:
                from services.purchase_service import build_voucher_payload

                payloads = [
                    build_voucher_payload(db, company, invoice)
                    for invoice in invoices
                ]
                path = build_bill_bundle(company, payloads)
        except Exception as exc:                        # never lose the page
            st.error(f"Could not build the file: {exc}")
            return
        st.session_state.excel_register_path = path
        st.success(f"Built from {len(invoices)} bill(s).")

    saved = st.session_state.get("excel_register_path")
    if saved and Path(saved).exists():
        is_zip = saved.lower().endswith(".zip")
        st.download_button(
            "⬇️ Download ZIP" if is_zip else "⬇️ Download workbook",
            data=Path(saved).read_bytes(),
            file_name=Path(saved).name,
            mime="application/zip" if is_zip else
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
        st.caption(f"Also saved on this computer at `{saved}`")


def _within(invoice, start, end) -> bool:
    """Date filter that copes with a missing or unparsable invoice date."""
    raw = invoice.invoice_date
    if not raw:
        moment = invoice.created_at
        raw = moment.date() if moment else None
    if isinstance(raw, str):
        try:
            raw = dt.date.fromisoformat(raw)
        except ValueError:
            return False
    if raw is None:
        return False
    return start <= raw <= end


def _purchase_tab(db, company):
    invoices = _recorded_invoices(db, company)
    if not invoices:
        _waiting_notice(db, company, empty=True)
        return

    by_supplier = defaultdict(float)
    by_month = defaultdict(float)
    for invoice in invoices:
        by_supplier[invoice.supplier_name or "Unknown"] += invoice.grand_total or 0
        try:
            month = dt.date.fromisoformat(invoice.invoice_date).strftime("%Y-%m")
        except (ValueError, TypeError):
            month = (invoice.created_at or dt.datetime.utcnow()).strftime("%Y-%m")
        by_month[month] += invoice.grand_total or 0

    cols = st.columns(3)
    cols[0].metric("Bills in this report", len(invoices))
    cols[1].metric("Total purchase value", rupees(sum(by_supplier.values())))
    cols[2].metric(
        "Average bill value", rupees(sum(by_supplier.values()) / max(len(invoices), 1))
    )

    st.markdown("#### Monthly purchase value")
    st.bar_chart(
        pd.DataFrame(sorted(by_month.items()), columns=["Month", "Value"]).set_index("Month"),
        height=260,
    )

    st.markdown("#### Top suppliers")
    supplier_frame = pd.DataFrame(
        sorted(by_supplier.items(), key=lambda kv: -kv[1]), columns=["Supplier", "Value"]
    )
    st.dataframe(supplier_frame, hide_index=True, width="stretch")
    st.download_button(
        "⬇️ Export supplier summary",
        supplier_frame.to_csv(index=False).encode(),
        file_name="supplier_summary.csv",
        mime="text/csv",
    )
    _waiting_notice(db, company, empty=False)


def _gst_tab(db, company):
    invoices = {i.id: i for i in _recorded_invoices(db, company)}
    if not invoices:
        _waiting_notice(db, company, empty=True)
        return
    items = (
        db.query(InvoiceItem)
        .filter(
            InvoiceItem.company_id == company.id,
            InvoiceItem.invoice_id.in_(list(invoices.keys())),
        )
        .all()
    )
    buckets = defaultdict(lambda: {"Taxable": 0.0, "CGST": 0.0, "SGST": 0.0, "IGST": 0.0, "Cess": 0.0})
    for item in items:
        bucket = buckets[f"{item.gst_rate or 0:g}%"]
        bucket["Taxable"] += item.taxable_value or 0
        bucket["CGST"] += item.cgst or 0
        bucket["SGST"] += item.sgst or 0
        bucket["IGST"] += item.igst or 0
        bucket["Cess"] += item.cess or 0

    frame = pd.DataFrame(
        [{"GST rate": rate, **{k: round(v, 2) for k, v in values.items()}}
         for rate, values in sorted(buckets.items())]
    )
    st.dataframe(frame, hide_index=True, width="stretch")
    totals = frame.drop(columns=["GST rate"]).sum() if not frame.empty else None
    if totals is not None:
        cols = st.columns(5)
        for col, (label, value) in zip(cols, totals.items()):
            col.metric(label, rupees(value))
    st.download_button(
        "⬇️ Export GST summary",
        frame.to_csv(index=False).encode(),
        file_name="gst_summary.csv",
        mime="text/csv",
    )
    st.caption(
        "This is a working summary of what the app posted, not a statutory return. "
        "Reconcile against Tally and GSTR-2B before filing."
    )
    _waiting_notice(db, company, empty=False)


def _stock_tab(db, company):
    products = (
        db.query(Product)
        .filter(Product.company_id == company.id, Product.is_active.is_(True))
        .order_by(Product.name)
        .all()
    )
    if not products:
        st.info("Your product master is empty.")
        return
    frame = pd.DataFrame(
        [
            {
                "Product": p.name,
                "Unit": p.unit or "",
                "Stock": p.current_stock or 0,
                "Last purchase rate": p.purchase_rate or 0,
                "Value": round((p.current_stock or 0) * (p.purchase_rate or 0), 2),
            }
            for p in products
        ]
    )
    st.metric("Stock value at last purchase rate", rupees(frame["Value"].sum()))
    st.dataframe(frame, hide_index=True, width="stretch", height=400)
    st.download_button(
        "⬇️ Export stock valuation",
        frame.to_csv(index=False).encode(),
        file_name="stock_valuation.csv",
        mime="text/csv",
    )


def _audit_tab(db, company):
    st.caption("Every action that touches a bill is recorded here.")
    col1, col2 = st.columns(2)
    days = col1.slider("Last N days", 1, 180, 30)
    action_filter = col2.text_input("Filter by action or detail")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

    logs = (
        db.query(AuditLog)
        .filter(AuditLog.company_id == company.id, AuditLog.created_at >= since)
        .order_by(AuditLog.created_at.desc())
        .limit(2000)
        .all()
    )
    if action_filter.strip():
        needle = action_filter.strip().lower()
        logs = [
            l for l in logs
            if needle in (l.action or "").lower() or needle in (l.detail or "").lower()
        ]
    if not logs:
        st.info("No audit entries for this period.")
        return
    frame = pd.DataFrame(
        [
            {
                "When": l.created_at.strftime("%d-%m-%Y %H:%M:%S") if l.created_at else "",
                "User": l.user_name or "system",
                # str(): an int here and "" there makes the column `object`,
                # which Arrow refuses to convert — the table then silently
                # fails to render.
                "Bill": str(l.invoice_id) if l.invoice_id else "",
                "Action": l.action,
                "Detail": l.detail or "",
            }
            for l in logs
        ]
    )
    st.dataframe(frame, hide_index=True, width="stretch", height=450)
    st.download_button(
        "⬇️ Export audit log",
        frame.to_csv(index=False).encode(),
        file_name=f"audit_log_{dt.date.today()}.csv",
        mime="text/csv",
    )


def _errors_tab(db, company):
    errors = (
        db.query(ProcessingError)
        .filter(ProcessingError.company_id == company.id)
        .order_by(ProcessingError.created_at.desc())
        .limit(200)
        .all()
    )
    if not errors:
        st.success("No processing errors recorded.")
        return
    for error in errors:
        with st.expander(
            f"{error.created_at:%d-%m-%Y %H:%M} · {error.stage} · {(error.message or '')[:70]}"
        ):
            st.write(error.message)
            if error.invoice_id:
                st.caption(f"Bill #{error.invoice_id}")
            if error.traceback_text:
                st.code(error.traceback_text[:3000])
            if not error.resolved and st.button("Mark resolved", key=f"res_{error.id}"):
                error.resolved = True
                db.commit()
                st.rerun()
