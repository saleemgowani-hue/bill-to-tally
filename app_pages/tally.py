"""Tally Connection: configure the connector, test it, and map ledgers."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from database.database import all_mappings, get_setting, set_setting
from database.models import Product, Supplier, TallyMapping
from services import audit_service
from services.tally_connector import CONNECTOR_LABELS, TallyError, get_connector
from utils.config import settings
from utils.ui import brand_header, require_login, session_db, test_mode_banner


def _connector_from_settings(db, company):
    mode = get_setting(db, company.id, "tally_mode", "mock")
    raw_test = get_setting(db, company.id, "tally_test_mode", str(settings.TALLY_TEST_MODE))
    is_test = str(raw_test).lower() in ("1", "true", "yes", "on")
    connector = get_connector(
        mode,
        {
            "host": get_setting(db, company.id, "tally_host", settings.TALLY_HOST),
            "port": get_setting(db, company.id, "tally_port", settings.TALLY_PORT),
            "company": company.tally_company_name or settings.TALLY_COMPANY,
            "test_mode": is_test,
        },
    )
    return connector, mode, is_test


def render() -> None:
    user, company = require_login()
    db = session_db()
    brand_header(f"{company.name} · Tally Connection")

    connector, mode, is_test = _connector_from_settings(db, company)
    test_mode_banner(is_test, f"Connector: {connector.name}")

    tab_conn, tab_map, tab_masters, tab_help = st.tabs(
        ["Connection", "Ledger mapping", "Tally masters", "How it works"]
    )

    with tab_conn:
        _connection_tab(db, company, user, mode, is_test)
    with tab_map:
        _mapping_tab(db, company, user, connector)
    with tab_masters:
        _masters_tab(connector)
    with tab_help:
        _help_tab()


def _connection_tab(db, company, user, mode, is_test):
    st.markdown("#### Connector")
    options = list(CONNECTOR_LABELS.keys())
    chosen = st.radio(
        "Integration method",
        options,
        index=options.index(mode) if mode in options else 0,
        format_func=lambda m: CONNECTOR_LABELS[m],
    )

    if chosen == "excel":
        st.info(
            "**No Tally.** Each confirmed bill is saved as an Excel workbook and "
            "stock is kept by this app — nothing to import anywhere. Download the "
            "full purchase register any time from **Reports → Excel export**.",
            icon="📗",
        )

    col1, col2, col3 = st.columns(3)
    host = col1.text_input(
        "Tally host", get_setting(db, company.id, "tally_host", settings.TALLY_HOST),
        disabled=chosen != "xml",
    )
    port = col2.text_input(
        "Port", str(get_setting(db, company.id, "tally_port", settings.TALLY_PORT)),
        disabled=chosen != "xml",
    )
    tally_company = col3.text_input(
        "Tally company name", company.tally_company_name or settings.TALLY_COMPANY,
        help="Exactly as it appears in Tally's company list",
        disabled=chosen == "excel",
    )

    if chosen == "excel":
        # Nothing is sent anywhere, so there is nothing for test mode to hold
        # back. Showing the toggle here would only invite the question "is it
        # safe yet?" about an action that never leaves this computer.
        test_mode = is_test
        st.caption(
            "Test mode does not apply in Excel mode — no accounting system is "
            "being written to."
        )
    else:
        test_mode = st.toggle(
            "Test mode — show what would be sent, post nothing",
            value=is_test,
            help="Keep this ON until you have verified the voucher format "
                 "against your own Tally setup.",
        )
        if not test_mode:
            st.warning(
                "Test mode is OFF. Confirmed bills will create real vouchers in "
                "the selected Tally company.",
                icon="⚠️",
            )

    if st.button("Save connection settings", type="primary"):
        set_setting(db, company.id, "tally_mode", chosen)
        set_setting(db, company.id, "tally_host", host)
        set_setting(db, company.id, "tally_port", port)
        set_setting(db, company.id, "tally_test_mode", str(test_mode))
        company.tally_company_name = tally_company
        db.commit()
        audit_service.record(
            db, company.id, "TALLY_SETTINGS_SAVED",
            f"mode={chosen}, host={host}:{port}, company='{tally_company}', test_mode={test_mode}",
            user=user,
        )
        st.success("Saved.")
        st.rerun()

    st.divider()
    if st.button("🔌 Test connection"):
        connector, _, _ = _connector_from_settings(db, company)
        with st.spinner("Contacting Tally…"):
            try:
                result = connector.test_connection()
            except TallyError as exc:
                st.error(str(exc))
                return
        (st.success if result.success else st.error)(result.message)
        audit_service.record(
            db, company.id, "TALLY_CONNECTION_TEST",
            f"{'success' if result.success else 'failed'}: {result.message[:300]}", user=user,
        )
        if result.success and connector.name == "tally-xml":
            try:
                st.caption(f"Companies open in Tally: {', '.join(connector.list_companies())}")
            except TallyError:
                pass


def _mapping_tab(db, company, user, connector):
    st.caption(
        "Nothing is hard-coded. These names are used verbatim in the voucher XML, "
        "so they must match your Tally ledgers exactly."
    )
    try:
        ledgers = connector.list_ledgers()
    except TallyError:
        ledgers = []

    if ledgers:
        st.success(f"{len(ledgers)} ledger(s) read from Tally — use the dropdowns below.")
    else:
        st.info("Ledger list unavailable (Tally not reachable or file-export mode). Type names manually.")

    mappings = all_mappings(db, company.id)
    rows = (
        db.query(TallyMapping)
        .filter(TallyMapping.company_id == company.id)
        .order_by(TallyMapping.mapping_key)
        .all()
    )

    updates = {}
    for row in rows:
        col1, col2 = st.columns([1.2, 2])
        col1.markdown(f"**{row.mapping_key}**  \n<span class='muted'>{row.description or ''}</span>",
                      unsafe_allow_html=True)
        current = mappings.get(row.mapping_key, "")
        if ledgers and row.mapping_key.endswith("_ledger"):
            choices = [current] + [l for l in ledgers if l != current] if current else ledgers
            updates[row.mapping_key] = col2.selectbox(
                row.mapping_key, choices, key=f"map_{row.id}", label_visibility="collapsed"
            )
        else:
            updates[row.mapping_key] = col2.text_input(
                row.mapping_key, current, key=f"map_{row.id}", label_visibility="collapsed"
            )

    if st.button("Save mapping", type="primary"):
        changed = 0
        for row in rows:
            value = updates.get(row.mapping_key, "")
            if (row.mapping_value or "") != value:
                row.mapping_value = value
                changed += 1
        db.commit()
        audit_service.record(
            db, company.id, "TALLY_MAPPING_SAVED", f"{changed} mapping(s) updated", user=user
        )
        st.success(f"Saved {changed} change(s).")

    st.divider()
    st.markdown("#### Per-record overrides")
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Suppliers → Tally ledgers**")
        suppliers = (
            db.query(Supplier)
            .filter(Supplier.company_id == company.id)
            .order_by(Supplier.name)
            .limit(200)
            .all()
        )
        if suppliers:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Supplier": s.name, "Tally ledger": s.tally_ledger_name or s.name}
                        for s in suppliers
                    ]
                ),
                hide_index=True, width="stretch", height=260,
            )
            st.caption("Edit these on the Suppliers page.")
        else:
            st.caption("No suppliers yet.")
    with col_b:
        st.markdown("**Products → Tally stock items**")
        products = (
            db.query(Product)
            .filter(Product.company_id == company.id)
            .order_by(Product.name)
            .limit(200)
            .all()
        )
        if products:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Product": p.name, "Tally stock item": p.tally_stock_item or p.name}
                        for p in products
                    ]
                ),
                hide_index=True, width="stretch", height=260,
            )
            st.caption("Edit these on the Products page.")
        else:
            st.caption("No products yet.")


def _masters_tab(connector):
    st.caption("Read-only view of what the connector can see in Tally right now.")
    if st.button("Fetch masters from Tally"):
        with st.spinner("Reading companies, ledgers, stock items, units and voucher types…"):
            try:
                data = {
                    "Companies": connector.list_companies(),
                    "Ledgers": connector.list_ledgers(),
                    "Stock items": connector.list_stock_items(),
                    "Units": connector.list_units(),
                    "Voucher types": connector.list_voucher_types(),
                }
            except TallyError as exc:
                st.error(str(exc))
                return
        for label, values in data.items():
            with st.expander(f"{label} ({len(values)})"):
                if values:
                    st.dataframe(
                        pd.DataFrame({label: values}), hide_index=True, width="stretch"
                    )
                else:
                    st.caption("Nothing returned.")


def _help_tab():
    st.markdown(
        """
#### What this integration is, exactly

Tally Prime and Tally.ERP 9 can act as an **HTTP server** on the machine where they
are running. Requests and responses are XML envelopes. That is the documented,
supported integration surface, and it is the only one this software uses. There is
no cloud API for Tally, and this product does not pretend otherwise.

**To enable it in Tally**

1. Open Tally on the computer that holds the company data.
2. `F1: Help → Settings → Connectivity → Client/Server configuration`.
3. Set *Tally acts as* = **Both** (or Server), port **9000**.
4. Load the company you want to post into.
5. Leave Tally open.

**What this means in practice**

| Situation | What works |
|---|---|
| App and Tally on the same PC | Direct posting (`localhost:9000`) |
| App on your LAN, Tally on the accounts PC | Direct posting (use that PC's IP) |
| App hosted in the cloud, Tally on a desktop | No direct route — use XML file export |

**XML file export** is always available and needs nothing enabled in Tally.
The app writes a voucher XML file; you import it with
`Gateway of Tally → Import → Vouchers`.

#### Before your first live post

Keep **test mode ON** and post a few bills. Test mode writes the exact XML to disk
without sending it. Import one of those files manually into a *test company* in
Tally and check the voucher, the ledgers hit and the stock movement. Only after
that should you switch test mode off.
"""
    )
