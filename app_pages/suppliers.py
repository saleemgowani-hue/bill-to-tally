"""Supplier master and their Tally ledger mapping."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from database.models import RECORDED_STATUSES, Invoice, Supplier
from services import audit_service, demo_service
from utils.ui import brand_header, require_login, rupees, session_db


def render() -> None:
    user, company = require_login()
    db = session_db()
    brand_header(f"{company.name} · Suppliers")

    suppliers = (
        db.query(Supplier)
        .filter(Supplier.company_id == company.id)
        .order_by(Supplier.name)
        .all()
    )

    tab_list, tab_add = st.tabs(["Supplier list", "Add supplier"])

    with tab_list:
        if not suppliers:
            st.info("No suppliers yet. They are created automatically from the bills you upload.")
        else:
            totals = {}
            for invoice in db.query(Invoice).filter(Invoice.company_id == company.id).all():
                if invoice.supplier_id and invoice.status in RECORDED_STATUSES:
                    totals[invoice.supplier_id] = totals.get(invoice.supplier_id, 0) + (
                        invoice.grand_total or 0
                    )

            # On a demo, say which rows came with the software rather than
            # from a bill — otherwise a supplier nobody uploaded looks like
            # a bug.
            samples = demo_service.sample_ids(db, company)["suppliers"]

            frame = pd.DataFrame(
                [
                    {
                        "ID": s.id, "Name": s.name,
                        "Source": "Sample" if s.id in samples else "Entered",
                        "GSTIN": s.gstin or "",
                        "State": s.state or "", "Phone": s.phone or "",
                        "Tally ledger": s.tally_ledger_name or "",
                        "Purchased": totals.get(s.id, 0.0), "Active": bool(s.is_active),
                    }
                    for s in suppliers
                ]
            )
            st.caption(
                "The Tally ledger column is what the voucher will use. Leave it blank "
                "to use the supplier name exactly as printed on the bill."
            )
            if samples:
                st.caption(
                    "**Sample** rows were seeded with the demo, not uploaded. "
                    "Remove them from Settings → Demo."
                )
            edited = st.data_editor(
                frame, hide_index=True, width="stretch",
                disabled=["ID", "Source", "Purchased"], key="supplier_editor",
            )
            if st.button("Save changes", type="primary"):
                lookup = {s.id: s for s in suppliers}
                changed = 0
                for _, row in edited.iterrows():
                    supplier = lookup.get(int(row["ID"]))
                    if supplier is None:
                        continue
                    for field, value in (
                        ("name", row["Name"]), ("gstin", row["GSTIN"] or None),
                        ("state", row["State"] or None), ("phone", row["Phone"] or None),
                        ("tally_ledger_name", row["Tally ledger"] or None),
                        ("is_active", bool(row["Active"])),
                    ):
                        if getattr(supplier, field) != value:
                            setattr(supplier, field, value)
                            changed += 1
                db.commit()
                audit_service.record(
                    db, company.id, "SUPPLIERS_EDITED", f"{changed} field(s) updated", user=user
                )
                st.success(f"Saved {changed} change(s).")
                st.rerun()

            st.markdown("#### Purchase value by supplier (entered bills)")
            if totals:
                chart = pd.DataFrame(
                    [
                        {"Supplier": next(s.name for s in suppliers if s.id == sid), "Value": value}
                        for sid, value in sorted(totals.items(), key=lambda kv: -kv[1])[:10]
                    ]
                ).set_index("Supplier")
                st.bar_chart(chart, height=260)
            else:
                st.caption("Nothing entered yet — confirm and post a bill to see this.")

    with tab_add:
        with st.form("add_supplier"):
            col1, col2 = st.columns(2)
            name = col1.text_input("Supplier name *")
            gstin = col2.text_input("GSTIN")
            col3, col4 = st.columns(2)
            phone = col3.text_input("Phone")
            email = col4.text_input("Email")
            address = st.text_area("Address")
            ledger = st.text_input(
                "Tally ledger name", help="Must match the ledger in Tally exactly, if it differs from the name above"
            )
            if st.form_submit_button("Add supplier", type="primary"):
                if not name.strip():
                    st.error("Supplier name is required.")
                else:
                    supplier = Supplier(
                        company_id=company.id, name=name.strip()[:200],
                        gstin=gstin.strip().upper() or None, phone=phone or None,
                        email=email or None, address=address or None,
                        state_code=(gstin or "")[:2] or None,
                        tally_ledger_name=ledger or None,
                    )
                    db.add(supplier)
                    db.commit()
                    audit_service.record(
                        db, company.id, "SUPPLIER_CREATED", f"'{supplier.name}' added", user=user
                    )
                    st.success(f"'{supplier.name}' added.")
