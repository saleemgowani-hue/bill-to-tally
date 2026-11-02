"""API Settings: Universal AI (Gemini) engine, key mode, model, usage.

Hindi/English labels on purpose — most users are shop owners and
accountants, not IT people.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from database.database import get_setting, set_setting
from services import api_manager, audit_service
from services.gemini_engine import mask_key, validate_key
from utils.config import settings
from utils.ui import brand_header, kpi, require_login, session_db

FALLBACKS = {
    "rapidocr": "RapidOCR — offline, fast (recommended)",
    "tesseract": "Tesseract — offline",
    "none": "No fallback — stop if Gemini is unavailable",
}


def render() -> None:
    user, company = require_login()
    db = session_db()
    brand_header(f"{company.name} · API Settings / एपीआई सेटिंग")

    current_engine = get_setting(db, company.id, "ocr_provider", settings.OCR_PROVIDER)
    if current_engine != "universal":
        st.info(
            "The Universal AI engine (Google Gemini) reads every kind of bill — any "
            "industry, any layout, multi-page PDFs — and falls back to offline OCR when "
            "there is no internet. / यूनिवर्सल AI इंजन हर तरह का बिल पढ़ता है।"
        )
        if user.is_admin and st.button("✅ Use Universal AI engine", type="primary"):
            set_setting(db, company.id, "ocr_provider", "universal")
            audit_service.record(db, company.id, "SETTINGS_CHANGED",
                                 "Extraction engine set to Universal AI", user=user)
            st.rerun()
    else:
        st.success("Universal AI engine is active / यूनिवर्सल AI इंजन चालू है")

    tab_key, tab_usage, tab_options = st.tabs(
        ["🔑 API Key / चाबी", "📈 Usage / उपयोग", "⚙️ Options / विकल्प"]
    )
    with tab_key:
        _key_tab(db, company, user)
    with tab_usage:
        _usage_tab(db, company)
    with tab_options:
        _options_tab(db, company, user)


def _central_available() -> bool:
    return bool(settings.GEMINI_API_KEY or settings.OCR_PROXY_URL)


def _key_tab(db, company, user):
    mode = api_manager.key_mode(db, company.id)
    modes = list(api_manager.KEY_MODES)
    if not _central_available():
        st.caption(
            "SN Softech central key is not set up on this installation — use your "
            "own Gemini key, or contact SN Softech Solutions."
        )
    chosen = st.radio(
        "Which Gemini key should be used? / कौन सी चाबी?",
        modes,
        index=modes.index(mode),
        format_func=lambda m: api_manager.KEY_MODES[m],
        disabled=not user.is_admin,
    )

    stored = api_manager.own_key(db, company.id)
    if chosen == "customer":
        st.markdown(f"**Saved key / सेव की गई चाबी:** `{mask_key(stored) or 'none'}`")
        new_key = st.text_input(
            "New Gemini API key (leave blank to keep) / नई चाबी",
            type="password", disabled=not user.is_admin,
            help="Create one free at aistudio.google.com → Get API key.",
        )
        col1, col2 = st.columns(2)
        if col1.button("🔍 Check key / चाबी जांचें", disabled=not (new_key or stored)):
            with st.spinner("Asking Google…"):
                ok, message, models = validate_key(new_key or stored)
            (st.success if ok else st.error)(message)
            if ok:
                st.session_state["gemini_models"] = models
        if col2.button("💾 Save / सेव करें", type="primary", disabled=not user.is_admin):
            if new_key:
                ok, message, models = validate_key(new_key)
                if not ok:
                    st.error(f"Not saved — {message}")
                    return
                api_manager.save_own_key(db, company.id, new_key)
                st.session_state["gemini_models"] = models
            set_setting(db, company.id, "gemini_key_mode", "customer")
            audit_service.record(db, company.id, "API_KEY_UPDATED",
                                 "Own Gemini key mode saved (key not logged)", user=user)
            st.success("Saved. / सेव हो गया।")
    else:
        if user.is_admin and mode != "central" and st.button("💾 Use central key", type="primary"):
            set_setting(db, company.id, "gemini_key_mode", "central")
            audit_service.record(db, company.id, "API_KEY_UPDATED",
                                 "Switched to SN Softech central key", user=user)
            st.rerun()
        limit = api_manager.monthly_limit(db, company.id)
        st.caption(
            "Monthly limit on the central key: "
            + (f"{limit} requests" if limit else "no limit set")
        )

    st.divider()
    models = st.session_state.get("gemini_models") or []
    current_model = get_setting(db, company.id, "universal_model", "") or settings.GEMINI_MODEL
    if chosen == "customer" and models:
        options = models if current_model in models else [current_model] + models
        model = st.selectbox("Gemini model (live list from Google)", options,
                             index=options.index(current_model), disabled=not user.is_admin)
    else:
        model = st.text_input(
            "Gemini model", current_model, disabled=not user.is_admin,
            help="'gemini-flash-latest' always points to Google's current Flash model. "
                 "Use 'Check key' to see every model your key can use.",
        )
    if user.is_admin and model.strip() != current_model and st.button("Save model"):
        set_setting(db, company.id, "universal_model", model.strip())
        st.success(f"Model set to {model.strip()}")


def _usage_tab(db, company):
    month = api_manager.usage_summary(db, company.id, api_manager.month_start())
    total = api_manager.usage_summary(db, company.id)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        kpi("Requests this month", month.calls,
            f"limit {month.limit}" if month.limit else "no limit")
    with c2:
        kpi("Pages this month", month.pages)
    with c3:
        kpi("Tokens this month", f"{month.prompt_tokens + month.output_tokens:,}")
    with c4:
        kpi("Est. cost this month", f"₹{month.cost_inr:,.2f}", f"all time ₹{total.cost_inr:,.2f}")
    st.caption(
        "Cost is an estimate from Google's token counts and the rates set by "
        "SN Softech; your Google bill is the final figure for your own key."
    )
    rows = api_manager.recent_usage(db, company.id, limit=100)
    if rows:
        st.dataframe(pd.DataFrame([{
            "When": r.created_at, "Document": r.document_name, "Model": r.model,
            "Key": r.key_mode, "Pages": r.pages,
            "Tokens": (r.prompt_tokens or 0) + (r.output_tokens or 0),
            "Est. ₹": round(r.cost_inr or 0, 3), "Seconds": round((r.duration_ms or 0) / 1000, 1),
        } for r in rows]), use_container_width=True, hide_index=True)
    else:
        st.info("No AI requests yet.")


def _options_tab(db, company, user):
    fallback = get_setting(db, company.id, "offline_fallback", "rapidocr") or "rapidocr"
    keys = list(FALLBACKS)
    new_fallback = st.selectbox(
        "When Gemini cannot be reached / इंटरनेट न हो तो",
        keys, index=keys.index(fallback) if fallback in keys else 0,
        format_func=lambda k: FALLBACKS[k], disabled=not user.is_admin,
    )
    pages = st.number_input(
        "Pages per AI request (long PDFs are read in parts)", 1, 8,
        int(get_setting(db, company.id, "pages_per_call", "") or settings.GEMINI_PAGES_PER_CALL),
        disabled=not user.is_admin,
    )
    auto_create = st.toggle(
        "Create missing ledgers, units and stock items in Tally automatically "
        "before posting / टैली में न हो तो अपने आप बनाएं",
        value=str(get_setting(db, company.id, "auto_create_masters", "1")) != "0",
        disabled=not user.is_admin,
        help="Everything created is listed in the audit log.",
    )
    auto_products = st.toggle(
        "Add products that are not in Products automatically when a bill is read "
        "/ नया प्रोडक्ट अपने आप जोड़ें",
        value=str(get_setting(db, company.id, "auto_create_products", "1")) != "0",
        disabled=not user.is_admin,
        help="Only items with no similar product are added. Items that look like an "
             "existing product are shown for you to pick, so no duplicates are made.",
    )
    if user.is_admin and st.button("💾 Save options", type="primary"):
        set_setting(db, company.id, "auto_create_products", "1" if auto_products else "0")
        set_setting(db, company.id, "offline_fallback", new_fallback)
        set_setting(db, company.id, "pages_per_call", str(int(pages)))
        set_setting(db, company.id, "auto_create_masters", "1" if auto_create else "0")
        audit_service.record(
            db, company.id, "SETTINGS_CHANGED",
            f"fallback={new_fallback}, pages_per_call={pages}, auto_create={auto_create}",
            user=user,
        )
        st.success("Saved. / सेव हो गया।")
