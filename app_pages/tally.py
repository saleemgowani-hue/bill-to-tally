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

    tab_conn, tab_map, tab_numbers, tab_masters, tab_help = st.tabs(
        ["Connection", "Ledger mapping", "Voucher numbers / वाउचर नंबर",
         "Tally masters", "How it works"]
    )

    with tab_conn:
        _connection_tab(db, company, user, mode, is_test)
    with tab_map:
        _mapping_tab(db, company, user, connector)
    with tab_numbers:
        _numbers_tab(db, company, user)
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


MAPPING_SECTIONS = [
    ("Purchase / खरीद", [
        "voucher_type", "purchase_ledger", "cgst_ledger", "sgst_ledger", "igst_ledger",
        "cess_ledger", "freight_ledger", "packing_ledger", "other_charges_ledger",
        "discount_ledger",
    ]),
    ("Sales / बिक्री", [
        "sales_voucher_type", "sales_ledger", "sales_cgst_ledger", "sales_sgst_ledger",
        "sales_igst_ledger", "sales_cess_ledger", "sales_freight_ledger",
        "sales_packing_ledger", "sales_other_charges_ledger",
    ]),
    ("Bank / बैंक", ["bank_payment_voucher_type", "bank_receipt_voucher_type"]),
    ("Common / सामान्य", [
        "round_off_ledger", "supplier_ledger_group", "customer_ledger_group",
        "stock_group", "default_godown", "default_unit",
    ]),
]


def _mapping_input(row, current, ledgers):
    col1, col2 = st.columns([1.2, 2])
    col1.markdown(
        f"**{row.description or row.mapping_key}**  \n<span class='muted'>{row.mapping_key}</span>",
        unsafe_allow_html=True,
    )
    if ledgers and row.mapping_key.endswith("_ledger"):
        choices = [current] + [l for l in ledgers if l != current] if current else list(ledgers)
        return col2.selectbox(row.mapping_key, choices, key=f"map_{row.id}",
                              label_visibility="collapsed")
    return col2.text_input(row.mapping_key, current, key=f"map_{row.id}",
                           label_visibility="collapsed")


def _party_ledgers_section(db, company, user, ledgers):
    """Customers and bank accounts -> Tally ledger, editable here."""
    from database.models import BankAccount, Customer

    st.divider()
    st.markdown("#### Customers & bank accounts / ग्राहक और बैंक खाते")
    st.caption("Leave 'Tally ledger' empty to use the name itself. Suppliers are edited "
               "on the Suppliers page, products on the Products page.")
    col_a, col_b = st.columns(2)
    for col, model, title, label in (
        (col_a, Customer, "Customers → Tally ledgers", "Customer"),
        (col_b, BankAccount, "Bank accounts → Tally ledgers", "Bank account"),
    ):
        with col:
            st.markdown(f"**{title}**")
            records = (db.query(model).filter(model.company_id == company.id)
                       .order_by(model.name).limit(300).all())
            if not records:
                st.caption(f"No {label.lower()}s yet.")
                continue
            frame = pd.DataFrame([{"id": r.id, label: r.name,
                                   "Tally ledger": r.tally_ledger_name or ""} for r in records])
            edited = st.data_editor(
                frame, key=f"edit_{model.__tablename__}", hide_index=True, width="stretch",
                disabled=["id", label] if user.is_admin else True,
                column_config={"id": None}, height=260,
            )
            if user.is_admin and st.button(f"Save {label.lower()} ledgers",
                                           key=f"save_{model.__tablename__}"):
                by_id = {r.id: r for r in records}
                changed = []
                for _, row in edited.iterrows():
                    record = by_id.get(int(row["id"]))
                    value = (row["Tally ledger"] or "").strip() or None
                    if record is not None and (record.tally_ledger_name or None) != value:
                        changed.append(f"{record.name} -> {value or record.name}")
                        record.tally_ledger_name = value
                db.commit()
                if changed:
                    audit_service.record(db, company.id, "TALLY_MAPPING_SAVED",
                                         "; ".join(changed)[:4000], user=user)
                st.success(f"Saved {len(changed)} change(s).")


def _mapping_tab(db, company, user, connector):
    st.markdown(
        "**Kaun si entry kis ledger mein jaayegi** — choose the Tally ledger for every "
        "part of a voucher. These names go into the voucher exactly as written."
    )
    st.caption(
        "If a name here does not exist in Tally yet and auto-create is on (API Settings → "
        "Options), it is created before posting; otherwise posting stops and tells you."
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

    by_key = {row.mapping_key: row for row in rows}
    updates = {}
    shown = set()
    for title, keys in MAPPING_SECTIONS:
        present = [k for k in keys if k in by_key]
        if not present:
            continue
        st.markdown(f"#### {title}")
        for key in present:
            shown.add(key)
            updates[key] = _mapping_input(by_key[key], mappings.get(key, ""), ledgers)
    leftover = [r for r in rows if r.mapping_key not in shown]
    if leftover:
        st.markdown("#### Other / अन्य")
        for row in leftover:
            updates[row.mapping_key] = _mapping_input(row, mappings.get(row.mapping_key, ""), ledgers)

    if st.button("💾 Save mapping / सेव करें", type="primary", disabled=not user.is_admin):
        changed = []
        for row in rows:
            value = (updates.get(row.mapping_key, "") or "").strip()
            if (row.mapping_value or "") != value:
                changed.append(f"{row.mapping_key}: '{row.mapping_value or ''}' -> '{value}'")
                row.mapping_value = value
        db.commit()
        audit_service.record(
            db, company.id, "TALLY_MAPPING_SAVED",
            (f"{len(changed)} mapping(s) updated: " + "; ".join(changed))[:4000], user=user
        )
        st.success(f"Saved {len(changed)} change(s).")

    _party_ledgers_section(db, company, user, ledgers)

    st.divider()
    st.markdown("#### Suppliers & products / सप्लायर और प्रोडक्ट")
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


def _numbers_tab(db, company, user):
    from services import voucher_numbering as vn

    st.markdown(
        "Choose the number each type of Tally voucher should start from. "
        "/ हर वाउचर किस नंबर से शुरू हो, यहाँ चुनें।"
    )
    st.info(
        "Tally keeps the number sent from here only if that voucher type's "
        "**Method of Voucher Numbering** is **Manual** or **Automatic (Manual "
        "Override)** — in Tally: Alter → Voucher Type → Purchase (Sales, Payment …). "
        "With plain *Automatic*, Tally gives its own number. A number is used up only "
        "after Tally accepts the voucher; a failed post re-uses it. Test Mode never "
        "uses a number. To restart on 1st April, set the start number back to 1."
    )
    configs = vn.all_configs(db, company.id)
    modes = list(vn.MODES)
    edited = {}
    for series, label in vn.SERIES.items():
        cfg = configs[series]
        with st.expander(
            f"{label} — "
            + (cfg.format() if cfg.mode == "custom"
               else vn.MODES[cfg.mode]),
            expanded=cfg.mode == "custom",
        ):
            mode = st.radio(
                "Numbering", modes, index=modes.index(cfg.mode),
                format_func=lambda m: vn.MODES[m], horizontal=True,
                key=f"vn_mode_{series}", disabled=not user.is_admin,
            )
            new = vn.SeriesConfig(mode=mode, prefix=cfg.prefix, next_number=cfg.next_number,
                                  digits=cfg.digits, suffix=cfg.suffix)
            if mode == "custom":
                c1, c2, c3, c4 = st.columns(4)
                new.prefix = c1.text_input("Prefix", cfg.prefix, key=f"vn_pre_{series}",
                                           disabled=not user.is_admin)
                new.next_number = int(c2.number_input(
                    "Next / start number", min_value=0, value=int(cfg.next_number), step=1,
                    key=f"vn_num_{series}", disabled=not user.is_admin))
                new.digits = int(c3.number_input(
                    "Digits (0001 = 4)", min_value=1, max_value=10, value=int(cfg.digits),
                    key=f"vn_dig_{series}", disabled=not user.is_admin))
                new.suffix = c4.text_input("Suffix", cfg.suffix, key=f"vn_suf_{series}",
                                           disabled=not user.is_admin)
                st.markdown(
                    f"Next three vouchers: **{new.format()}**, "
                    f"**{new.format(new.next_number + 1)}**, **{new.format(new.next_number + 2)}**"
                )
            elif mode == "invoice":
                st.caption("The voucher number will be the bill / invoice number (for bank "
                           "entries: the cheque / UTR reference).")
            edited[series] = new
    if user.is_admin and st.button("💾 Save voucher numbers", type="primary"):
        changes = []
        for series, new in edited.items():
            old = configs[series]
            if (new.mode, new.prefix, new.next_number, new.digits, new.suffix) != (
                old.mode, old.prefix, old.next_number, old.digits, old.suffix
            ):
                vn.save_config(db, company.id, series, new)
                changes.append(
                    f"{series}: {vn.MODES[new.mode]}"
                    + (f" from {new.format()}" if new.mode == "custom" else "")
                )
        if changes:
            audit_service.record(db, company.id, "VOUCHER_NUMBERING_CHANGED",
                                 "; ".join(changes), user=user)
            st.success("Saved. / सेव हो गया।")
            st.rerun()
        else:
            st.info("Nothing changed.")


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
