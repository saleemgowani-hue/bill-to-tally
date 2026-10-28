"""Dashboard: headline KPIs, trends and what needs attention."""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict

import pandas as pd
import streamlit as st

from database.models import AuditLog, Invoice, InvoiceStatus, Product
from services import license_service
from services.purchase_service import dashboard_metrics
from utils.config import settings
from utils.ui import (
    compact,
    require_login,
    rupees,
    session_db,
    status_badge,
    test_mode_banner,
    tile,
)


def _greeting() -> str:
    hour = dt.datetime.now().hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def _hero(user, company, metrics) -> None:
    state = license_service.status(company)
    pending = int(metrics["pending"])
    line = (
        f"{pending} bill{'s' if pending != 1 else ''} waiting for your verification."
        if pending
        else "Nothing is waiting for verification. You are all caught up."
    )
    pill = (
        f"🔑 {state.plan.title()} licence · {state.days_left} days left"
        if state.is_valid
        else "🔑 Licence expired"
    )
    first_name = (user.full_name or "there").split(" ")[0]
    st.markdown(
        f"""<div class="hero">
              <h2>{_greeting()}, {first_name} 👋</h2>
              <p>{company.name} · {line}</p>
              <p>Purchases posted to Tally so far: <b>{rupees(metrics['purchase_value'])}</b></p>
              <span class="pill">{pill}</span>
            </div>""",
        unsafe_allow_html=True,
    )


def _kpi_grid(metrics) -> None:
    cards = [
        ("Bills processed", f"{int(metrics['processed']):,}", "All time", "t-blue", "📄"),
        ("Pending verification", f"{int(metrics['pending']):,}",
         "Waiting for a human", "t-amber", "🔍"),
        ("Posted to Tally", f"{int(metrics['posted']):,}", "Stock updated", "t-green", "✅"),
        ("Purchase value", f"₹{compact(metrics['purchase_value'])}",
         "Posted bills only", "t-purple", "💰"),
        ("Ready to post", f"{int(metrics['confirmed']):,}",
         "Confirmed, awaiting Tally", "t-teal", "🚀"),
        ("Awaiting Tally import", f"{int(metrics.get('exported', 0)):,}",
         "XML made, stock not yet updated", "t-amber", "📥"),
        ("Low confidence", f"{int(metrics['low_confidence']):,}",
         "AI was unsure — check these", "t-red", "⚠️"),
        ("Unmatched products", f"{int(metrics['new_products']):,}",
         "Need linking to your master", "t-slate", "📦"),
    ]
    for start in (0, 4):
        columns = st.columns(4)
        for col, (label, value, sub, colour, glyph) in zip(columns, cards[start:start + 4]):
            col.markdown(tile(label, value, sub, colour, glyph), unsafe_allow_html=True)
        st.write("")


def _trend(db, company) -> None:
    invoices = (
        db.query(Invoice)
        .filter(
            Invoice.company_id == company.id,
            Invoice.status == InvoiceStatus.POSTED.value,
        )
        .all()
    )
    if not invoices:
        st.info("Post your first bill to see the purchase trend here.", icon="📈")
        return

    monthly: dict[str, float] = defaultdict(float)
    for invoice in invoices:
        when = invoice.invoice_date or invoice.created_at
        if isinstance(when, str):
            try:
                when = dt.date.fromisoformat(when)
            except ValueError:
                continue
        if when is None:
            continue
        monthly[when.strftime("%Y-%m")] += invoice.grand_total or 0.0

    frame = (
        pd.DataFrame({"Month": list(monthly), "Purchase value": list(monthly.values())})
        .sort_values("Month")
        .tail(12)
        .set_index("Month")
    )
    st.bar_chart(frame, height=260, color="#1570ef")


def _status_mix(db, company) -> None:
    counts = Counter(
        row[0]
        for row in db.query(Invoice.status).filter(Invoice.company_id == company.id).all()
    )
    if not counts:
        st.caption("No bills yet.")
        return
    frame = pd.DataFrame(
        {"Status": list(counts.keys()), "Bills": list(counts.values())}
    ).sort_values("Bills", ascending=False)
    st.dataframe(frame, hide_index=True, width="stretch", height=250)


def _attention(db, company) -> None:
    waiting = (
        db.query(Invoice)
        .filter(
            Invoice.company_id == company.id,
            Invoice.status.in_(
                [
                    InvoiceStatus.REVIEW_REQUIRED.value,
                    InvoiceStatus.DUPLICATE.value,
                    InvoiceStatus.EXPORTED.value,
                    InvoiceStatus.FAILED.value,
                ]
            ),
        )
        .order_by(Invoice.created_at.desc())
        .limit(6)
        .all()
    )
    if not waiting:
        st.success("Nothing needs attention right now.", icon="🎉")
        return

    for invoice in waiting:
        cols = st.columns([3, 2, 2, 1.4])
        cols[0].markdown(
            f"**{invoice.supplier_name or 'Unknown supplier'}**  \n"
            f"<span class='muted'>{invoice.invoice_number or 'no number'} · "
            f"{invoice.invoice_date or '—'}</span>",
            unsafe_allow_html=True,
        )
        cols[1].markdown(status_badge(invoice.status), unsafe_allow_html=True)
        cols[2].markdown(f"**{rupees(invoice.grand_total or 0)}**")
        if cols[3].button("Open", key=f"dash_open_{invoice.id}", width="stretch"):
            st.session_state.review_invoice_id = invoice.id
            st.session_state.nav = "Review Bills"
            st.rerun()


def _top_suppliers(db, company) -> None:
    rows = (
        db.query(Invoice.supplier_name, Invoice.grand_total)
        .filter(
            Invoice.company_id == company.id,
            Invoice.status == InvoiceStatus.POSTED.value,
        )
        .all()
    )
    if not rows:
        st.caption("No posted bills yet.")
        return
    totals: dict[str, float] = defaultdict(float)
    for name, amount in rows:
        totals[name or "Unknown"] += amount or 0.0
    frame = (
        pd.DataFrame({"Supplier": list(totals), "Value": list(totals.values())})
        .sort_values("Value", ascending=False)
        .head(5)
        .set_index("Supplier")
    )
    st.bar_chart(frame, height=250, color="#6941c6")


def _low_stock(db, company) -> None:
    products = (
        db.query(Product)
        .filter(Product.company_id == company.id, Product.is_active.is_(True))
        .order_by(Product.current_stock.asc())
        .limit(8)
        .all()
    )
    if not products:
        st.caption("Your product master is empty.")
        return
    frame = pd.DataFrame(
        [
            {"Product": p.name, "Stock": p.current_stock or 0, "Unit": p.unit or ""}
            for p in products
        ]
    )
    st.dataframe(frame, hide_index=True, width="stretch", height=250)


def _recent_activity(db, company) -> None:
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.company_id == company.id)
        .order_by(AuditLog.created_at.desc())
        .limit(7)
        .all()
    )
    if not logs:
        st.caption("No activity yet.")
        return
    for entry in logs:
        when = entry.created_at.strftime("%d %b %H:%M") if entry.created_at else "—"
        st.markdown(
            "<div style='padding:5px 0;border-bottom:1px solid #f2f4f7;'>"
            f"<b>{(entry.action or '').replace('_', ' ').title()}</b>"
            f"<span class='muted'> · {when}</span><br>"
            f"<span class='muted'>{(entry.detail or '')[:80]}</span></div>",
            unsafe_allow_html=True,
        )


def render() -> None:
    user, company = require_login()
    db = session_db()

    test_mode_banner(settings.TALLY_TEST_MODE)

    metrics = dashboard_metrics(db, company.id)
    _hero(user, company, metrics)
    _kpi_grid(metrics)

    left, right = st.columns([2, 1])
    with left:
        st.markdown("#### 📈 Purchase value by month")
        _trend(db, company)
    with right:
        st.markdown("#### 🧭 Bills by status")
        _status_mix(db, company)

    st.write("")
    st.markdown("#### 🔔 Needs your attention")
    _attention(db, company)

    st.write("")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("#### 🏭 Top suppliers")
        _top_suppliers(db, company)
    with col2:
        st.markdown("#### 📦 Lowest stock")
        _low_stock(db, company)
    with col3:
        st.markdown("#### 🕒 Recent activity")
        _recent_activity(db, company)
