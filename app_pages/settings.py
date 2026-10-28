"""Settings: AI provider, thresholds, company profile, users and system info."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from database.database import get_setting, set_setting
from database.models import Role, User
from services import audit_service
from services.ocr_service import PROVIDER_LABELS, get_provider
from utils.config import settings as cfg
from utils.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    mask_secret,
    password_problems,
)
from utils.ui import brand_header, require_login, session_db

PROVIDER_KEY_SETTING = {
    "gemini": ("gemini_api_key", "GEMINI_API_KEY", cfg.GEMINI_MODEL),
    "openai": ("openai_api_key", "OPENAI_API_KEY", cfg.OPENAI_MODEL),
    "claude": ("anthropic_api_key", "ANTHROPIC_API_KEY", cfg.ANTHROPIC_MODEL),
}


def render() -> None:
    user, company = require_login()
    db = session_db()
    brand_header(f"{company.name} · Settings")

    from services import demo_service

    # The Demo tab exists only on a demo company. On a real company there is no
    # tab, no button and no code path that could clear its records.
    names = ["AI / OCR", "Thresholds", "Company", "Users", "Licence"]
    showing_demo = demo_service.is_demo(company)
    if showing_demo:
        names.append("🧪 Demo")
    names.append("System")
    tabs = st.tabs(names)

    tab_ai, tab_thresholds, tab_company, tab_users, tab_licence = tabs[:5]
    tab_demo = tabs[5] if showing_demo else None
    tab_system = tabs[-1]

    with tab_ai:
        _ai_tab(db, company, user)
    with tab_thresholds:
        _thresholds_tab(db, company, user)
    with tab_company:
        _company_tab(db, company, user)
    with tab_users:
        _users_tab(db, company, user)
    with tab_licence:
        _licence_tab(db, company, user)
    if tab_demo is not None:
        with tab_demo:
            _demo_tab(db, company, user)
    with tab_system:
        _system_tab(db, company)


def _ai_tab(db, company, user):
    current = get_setting(db, company.id, "ocr_provider", cfg.OCR_PROVIDER)
    options = list(PROVIDER_LABELS.keys())
    chosen = st.radio(
        "Extraction engine",
        options,
        index=options.index(current) if current in options else 0,
        format_func=lambda p: PROVIDER_LABELS[p],
    )

    st.caption(
        "Vision models (Gemini, OpenAI, Claude) read and structure the bill in one "
        "step and give the best accuracy. Tesseract and PaddleOCR run fully offline "
        "but only produce text, which is then parsed with rule-based logic — expect "
        "to verify every line."
    )

    api_key = ""
    model = ""
    if chosen in PROVIDER_KEY_SETTING:
        setting_key, env_name, default_model = PROVIDER_KEY_SETTING[chosen]
        stored = decrypt_secret(get_setting(db, company.id, setting_key, ""))
        st.markdown(f"**API key** — currently `{mask_secret(stored)}`")
        api_key = st.text_input(
            "New API key (leave blank to keep the current one)",
            type="password",
            key=f"key_{chosen}",
        )
        model = st.text_input(
            "Model",
            get_setting(db, company.id, f"{chosen}_model", default_model),
            key=f"model_{chosen}",
        )
        st.caption(
            f"Keys are encrypted before storage and never sent to the browser. "
            f"You can also set `{env_name}` in your .env file."
        )
    elif chosen == "tesseract":
        model = ""
        tesseract_cmd = st.text_input(
            "Tesseract executable path (Windows)",
            get_setting(db, company.id, "tesseract_cmd", cfg.TESSERACT_CMD),
            placeholder=r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        )
        st.session_state["_tesseract_cmd"] = tesseract_cmd
    elif chosen == "rapidocr":
        model = ""
        st.caption(
            "Fast, fully offline text engine (no API key, no separate program "
            "to install — just `pip install rapidocr`, no first-run download). "
            "Reads about as accurately as Tesseract but noticeably faster and "
            "keeps table columns better aligned. Not full table-structure "
            "recognition like PP-StructureV3 — for a genuinely complex table "
            "layout (e.g. a Bank statement), an AI vision engine still reads "
            "it more reliably."
        )
    elif chosen == "ppstructure":
        model = ""
        st.caption(
            "Reads real tables (not just text) — needs `paddleocr[doc-parser]`, "
            "`paddlex[ocr]`, and `paddlepaddle==3.2.2` installed (newer "
            "paddlepaddle 3.3.x has a CPU bug that breaks this engine), and "
            "Python 3.8-3.12 (not 3.13+). See INSTALL_OFFLINE_OCR.bat. First "
            "bill after starting the app will be slow while the model loads."
        )
    elif chosen == "auto":
        model = ""
        fallback_options = ["gemini", "openai", "claude"]
        current_fallback = get_setting(db, company.id, "auto_fallback_provider", "gemini")
        fallback_choice = st.selectbox(
            "AI vision engine to use when PP-StructureV3's result looks unreliable",
            fallback_options,
            index=fallback_options.index(current_fallback) if current_fallback in fallback_options else 0,
            format_func=lambda p: PROVIDER_LABELS.get(p, p),
        )
        st.session_state["_auto_fallback"] = fallback_choice
        st.caption(
            "Tries PP-StructureV3 first (free, offline). Only calls the AI "
            "engine above — using whatever key you've saved for it under its "
            "own tab — when confidence is low, fields are missing, or the "
            "totals don't add up. Needs the same install as PP-StructureV3."
        )
    elif chosen == "proxy":
        model = ""
        stored_licence = decrypt_secret(get_setting(db, company.id, "proxy_api_key", ""))
        st.markdown(f"**Licence key on file** — currently `{mask_secret(stored_licence)}`")
        api_key = st.text_input(
            "Your SN Softech licence key (the one you activated with — "
            "leave blank to keep the current one)",
            type="password",
            key="key_proxy",
            placeholder="SNST-XXXX-XXXX-XXXX-XXXX",
        )
        st.caption(
            "Best accuracy on complex tables, pay-per-use, no Gemini/OpenAI "
            "account or API key to set up yourself — SN Softech runs the AI "
            "engine on their own server and bills per bill read. You only "
            "enter your licence key, nothing else; it never leaves this "
            "installation except to prove you're a licensed customer. A "
            "small number of bills per day is included — contact SN "
            "Softech Solutions to raise that limit or to enable this engine "
            "for your installation."
        )
    elif chosen == "mock":
        st.warning("The mock engine returns fixed sample data. Use it for demos only.")

    col1, col2 = st.columns(2)
    if col1.button("Save AI settings", type="primary"):
        set_setting(db, company.id, "ocr_provider", chosen)
        if chosen in PROVIDER_KEY_SETTING:
            setting_key, _, _ = PROVIDER_KEY_SETTING[chosen]
            if api_key.strip():
                set_setting(db, company.id, setting_key, encrypt_secret(api_key.strip()), True)
            if model.strip():
                set_setting(db, company.id, f"{chosen}_model", model.strip())
        if chosen == "tesseract":
            set_setting(db, company.id, "tesseract_cmd", st.session_state.get("_tesseract_cmd", ""))
        if chosen == "proxy" and api_key.strip():
            set_setting(db, company.id, "proxy_api_key", encrypt_secret(api_key.strip()), True)
        if chosen == "auto":
            set_setting(
                db, company.id, "auto_fallback_provider",
                st.session_state.get("_auto_fallback", "gemini"),
            )
        audit_service.record(
            db, company.id, "AI_SETTINGS_SAVED", f"Provider set to {chosen}", user=user
        )
        st.success("Saved.")
        st.rerun()

    if col2.button("Test this engine"):
        config = {}
        if chosen in PROVIDER_KEY_SETTING:
            setting_key, _, _ = PROVIDER_KEY_SETTING[chosen]
            config["api_key"] = api_key.strip() or decrypt_secret(
                get_setting(db, company.id, setting_key, "")
            )
            config["model"] = model
        if chosen == "tesseract":
            config["tesseract_cmd"] = st.session_state.get("_tesseract_cmd", "")
        if chosen == "proxy":
            config["api_key"] = api_key.strip() or decrypt_secret(
                get_setting(db, company.id, "proxy_api_key", "")
            )
        if chosen == "auto":
            fallback = st.session_state.get("_auto_fallback", "gemini")
            config["fallback_provider"] = fallback
            fb_setting_key, _, _ = PROVIDER_KEY_SETTING.get(fallback, (None, None, None))
            if fb_setting_key:
                config["api_key"] = decrypt_secret(get_setting(db, company.id, fb_setting_key, ""))
                config["model"] = get_setting(db, company.id, f"{fallback}_model", "")
        ok, message = get_provider(chosen, config).health_check()
        (st.success if ok else st.error)(message)


def _thresholds_tab(db, company, user):
    st.caption(
        "These control when the software insists on a human decision. Raising them "
        "means more manual checks and fewer surprises."
    )
    col1, col2 = st.columns(2)
    high = col1.slider(
        "High confidence (green) at or above",
        0.5, 1.0,
        float(get_setting(db, company.id, "confidence_high", cfg.CONFIDENCE_HIGH)),
        0.01,
    )
    review = col2.slider(
        "Mandatory review below",
        0.1, 0.95,
        float(get_setting(db, company.id, "confidence_review", cfg.CONFIDENCE_REVIEW)),
        0.01,
    )
    col3, col4 = st.columns(2)
    auto_match = col3.slider(
        "Auto-apply a product match at or above",
        0.7, 1.0,
        float(get_setting(db, company.id, "match_auto", cfg.MATCH_AUTO_THRESHOLD)),
        0.01,
    )
    suggest_match = col4.slider(
        "Suggest a possible match at or above",
        0.3, 0.95,
        float(get_setting(db, company.id, "match_suggest", cfg.MATCH_SUGGEST_THRESHOLD)),
        0.01,
    )
    tolerance = st.number_input(
        "Rounding tolerance (₹)",
        0.0, 100.0,
        float(get_setting(db, company.id, "rounding_tolerance", cfg.ROUNDING_TOLERANCE)),
        0.5,
        help="Differences up to this amount are treated as rounding, not as errors.",
    )

    if review >= high:
        st.warning("The mandatory-review threshold should be below the high-confidence one.")
    if suggest_match >= auto_match:
        st.warning("The suggestion threshold should be below the auto-apply one.")

    if st.button("Save thresholds", type="primary"):
        set_setting(db, company.id, "confidence_high", high)
        set_setting(db, company.id, "confidence_review", review)
        set_setting(db, company.id, "match_auto", auto_match)
        set_setting(db, company.id, "match_suggest", suggest_match)
        set_setting(db, company.id, "rounding_tolerance", tolerance)
        audit_service.record(
            db, company.id, "THRESHOLDS_SAVED",
            f"high={high}, review={review}, auto_match={auto_match}, tolerance={tolerance}",
            user=user,
        )
        st.success("Saved.")


def _company_tab(db, company, user):
    col1, col2 = st.columns(2)
    name = col1.text_input("Company name", company.name)
    gstin = col2.text_input(
        "Your GSTIN", company.gstin or "",
        help="Used to work out whether a purchase is intra-state or inter-state",
    )
    col3, col4 = st.columns(2)
    state = col3.text_input("State", company.state or "")
    phone = col4.text_input("Phone", company.phone or "")
    address = st.text_area("Address", company.address or "")
    tally_company = st.text_input(
        "Tally company name", company.tally_company_name or "",
        help="Must match the company name in Tally exactly",
    )
    if st.button("Save company profile", type="primary"):
        company.name = name
        company.gstin = gstin.strip().upper() or None
        company.state = state or None
        company.state_code = (gstin or "")[:2] or None
        company.phone = phone or None
        company.address = address or None
        company.tally_company_name = tally_company or None
        db.commit()
        audit_service.record(db, company.id, "COMPANY_UPDATED", f"Profile saved by {user.email}", user=user)
        st.success("Saved.")


def _users_tab(db, company, user):
    if not user.is_admin:
        st.info("Only an Admin can manage users.")
        return

    users = db.query(User).filter(User.company_id == company.id).order_by(User.full_name).all()
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "ID": u.id, "Name": u.full_name, "Email": u.email, "Role": u.role,
                    "Active": bool(u.is_active),
                    "Last login": u.last_login_at.strftime("%d-%m-%Y %H:%M") if u.last_login_at else "—",
                }
                for u in users
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "Admin: everything. Manager: review, confirm and post. Operator: upload and "
        "review only — cannot confirm or post."
    )

    with st.expander("Add a user"):
        with st.form("add_user"):
            col1, col2 = st.columns(2)
            full_name = col1.text_input("Full name")
            email = col2.text_input("Email")
            col3, col4 = st.columns(2)
            role = col3.selectbox("Role", [r.value for r in Role], index=2)
            password = col4.text_input("Temporary password", type="password")
            if st.form_submit_button("Create user", type="primary"):
                if not full_name.strip() or not email.strip():
                    st.error("Name and email are required.")
                elif password_problems(password):
                    st.error("Password needs " + ", ".join(password_problems(password)) + ".")
                elif db.query(User).filter(User.email == email.strip().lower()).first():
                    st.error("That email is already registered.")
                else:
                    db.add(
                        User(
                            company_id=company.id, full_name=full_name.strip(),
                            email=email.strip().lower(), password_hash=hash_password(password),
                            role=role,
                        )
                    )
                    db.commit()
                    audit_service.record(
                        db, company.id, "USER_CREATED", f"{email} added as {role}", user=user
                    )
                    st.success(f"{full_name} added as {role}.")
                    st.rerun()

    with st.expander("Change a user's role or status"):
        target_id = st.selectbox(
            "User", [u.id for u in users],
            format_func=lambda i: next(f"{u.full_name} ({u.email})" for u in users if u.id == i),
        )
        target = next(u for u in users if u.id == target_id)
        col1, col2 = st.columns(2)
        new_role = col1.selectbox(
            "Role", [r.value for r in Role], index=[r.value for r in Role].index(target.role)
        )
        active = col2.checkbox("Active", value=bool(target.is_active))
        if st.button("Apply"):
            if target.id == user.id and (new_role != Role.ADMIN.value or not active):
                st.error("You cannot remove your own admin access.")
            else:
                target.role = new_role
                target.is_active = active
                db.commit()
                audit_service.record(
                    db, company.id, "USER_UPDATED",
                    f"{target.email} → role={new_role}, active={active}", user=user,
                )
                st.success("Updated.")
                st.rerun()

    with st.expander("Change my password"):
        new_password = st.text_input("New password", type="password", key="own_pw")
        if st.button("Update my password"):
            problems = password_problems(new_password)
            if problems:
                st.error("Password needs " + ", ".join(problems) + ".")
            else:
                user.password_hash = hash_password(new_password)
                db.commit()
                audit_service.record(db, company.id, "PASSWORD_CHANGED", user.email, user=user)
                st.success("Password updated.")


def _licence_tab(db, company, user):
    from services import demo_service, license_service
    from services.license_service import LicenseError

    state = license_service.status(company)
    css = "lic-ok" if state.is_valid and not state.is_expiring_soon else (
        "lic-warn" if state.is_valid else "lic-bad"
    )
    st.markdown(f'<div class="{css}">🔑 {state.message}</div>', unsafe_allow_html=True)
    st.write("")

    cols = st.columns(3)
    cols[0].metric("Plan", (company.license_plan or "—").title())
    cols[1].metric(
        "Expires on",
        company.license_expires_at.strftime("%d %b %Y")
        if company.license_expires_at else "—",
    )
    cols[2].metric("Days remaining", state.days_left if state.is_valid else 0)
    st.caption(f"Key on file: `{company.license_key_display or '—'}`")

    if demo_service.is_demo(company):
        st.warning(
            "This is a DEMO company, for showing the software. Clear it between "
            "prospects from the **Demo** tab. Use a licensed account for real "
            "purchase records.",
            icon="🧪",
        )

    st.divider()
    st.markdown("#### Renew or upgrade")
    st.caption(
        "Renewing early does not waste days — a new key extends from your current "
        "expiry date rather than from today."
    )
    if not user.is_admin:
        st.info("Only an Admin can activate a licence key.")
        return

    new_key = st.text_input(
        "New licence key", placeholder="SNST-XXXX-XXXX-XXXX-XXXX", key="renew_key"
    )
    if st.button("Activate licence", type="primary"):
        try:
            record = license_service.activate(db, company, new_key, activated_by=user.id)
        except LicenseError as exc:
            st.error(str(exc), icon="⚠️")
        else:
            audit_service.record(
                db, company.id, "LICENCE_ACTIVATED",
                f"{record.plan} licence activated, valid to "
                f"{record.expires_at:%d-%m-%Y}",
                user=user,
            )
            st.success(
                f"{record.plan.title()} licence activated. "
                f"Valid until {record.expires_at:%d %b %Y}."
            )
            st.rerun()

    st.divider()
    st.markdown("#### Activation history")
    history = license_service.company_licenses(db, company.id)
    if not history:
        st.caption("No licence has been activated yet.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Key": record.key_display,
                    "Plan": record.plan,
                    "Activated": record.activated_at.strftime("%d-%m-%Y %H:%M")
                    if record.activated_at else "—",
                    "Valid until": record.expires_at.strftime("%d-%m-%Y")
                    if record.expires_at else "—",
                }
                for record in history
            ]
        ),
        hide_index=True,
        width="stretch",
    )



def _demo_tab(db, company, user):
    """Clear the demo between prospects.

    Reached only from a demo company's Settings, and the service refuses a
    non-demo company even if this were somehow called for one.
    """
    from services import demo_service

    st.markdown("#### Demo account")
    st.caption(
        "This account is for showing the software. Upload bills, walk a customer "
        "through them, then clear everything here before the next one."
    )

    summary = demo_service.demo_data_summary(db, company)
    if summary:
        columns = st.columns(len(summary))
        for column, (label, count) in zip(columns, summary.items()):
            column.metric(label.title(), count)
    else:
        st.success("Nothing to clear — the demo is already fresh.", icon="✨")

    st.divider()
    st.markdown("**What clearing removes**")
    st.markdown(
        "- every bill uploaded during demos, with its lines and Tally records\n"
        "- products and suppliers created while demonstrating\n"
        "- the demo's audit trail and stock movements\n"
        "\n**What stays:** this company, its users, its licence, and the "
        "products and suppliers it was set up with — each back at its opening "
        "stock, so the next demo starts exactly like a fresh install."
    )

    if not user.is_admin and not user.can_post_to_tally:
        st.info("Ask an Admin or Manager to clear the demo.")
        return

    if summary:
        st.divider()
        confirmed = st.checkbox(
            "I have finished with this demo and want to clear it",
            key="demo_tab_confirm",
        )
        if st.button(
            "🧹 Clear demo data now", type="primary", disabled=not confirmed,
            key="demo_tab_clear",
        ):
            try:
                result = demo_service.clear_demo_data(db, company, user)
            except demo_service.NotADemoCompany as exc:
                st.error(str(exc), icon="🛑")
            else:
                st.session_state.pop("review_invoice_id", None)
                st.session_state.demo_tab_confirm = False
                st.success(
                    f"Cleared. {result.total_deleted} row(s) removed and "
                    f"{result.stock_reset} product(s) reset to opening stock.",
                    icon="✅",
                )
                st.rerun()

    st.divider()
    _sample_data_section(db, company, user)

    st.divider()
    st.caption(
        "Real customer accounts have no Demo tab and no clear button — their "
        "data is permanent. Demo and real accounts are separate companies and "
        "their records never mix."
    )


def _sample_data_section(db, company, user):
    """The products and supplier the demo shipped with.

    These are not something anyone uploaded, which surprises people the first
    time they meet a supplier they have never heard of in the supplier list.
    They are named here, and removable, for a demo that should start from a
    completely empty master.
    """
    from services import demo_service

    from database.models import Product, Supplier

    ids = demo_service.sample_ids(db, company)
    summary = demo_service.sample_data_summary(db, company)

    st.markdown("**Sample data this demo was set up with**")
    if not summary:
        st.caption("None left — every product and supplier here was entered by you.")
        return

    names = []
    if ids["suppliers"]:
        names += [
            f"{s.name} (supplier)" for s in db.query(Supplier).filter(
                Supplier.company_id == company.id, Supplier.id.in_(ids["suppliers"])
            ).order_by(Supplier.name).all()
        ]
    if ids["products"]:
        products = db.query(Product).filter(
            Product.company_id == company.id, Product.id.in_(ids["products"])
        ).order_by(Product.name).all()
        names += [f"{p.name} — opening stock {p.current_stock:g}" for p in products]

    st.caption(
        "Seeded so product matching has something to match against and stock "
        "figures visibly move when you post a bill. Nobody uploaded these:"
    )
    for name in names:
        st.markdown(f"- {name}")

    if not user.is_admin and not user.can_post_to_tally:
        return

    confirmed = st.checkbox(
        "Remove these too — start the next demo with empty masters",
        key="demo_tab_samples_confirm",
        help="Product matching will then find nothing until you add products. "
             "Anything already used by a bill is kept.",
    )
    if st.button(
        "🗑️ Remove sample data", disabled=not confirmed, key="demo_tab_samples",
    ):
        try:
            removed = demo_service.remove_sample_data(db, company, user)
        except demo_service.NotADemoCompany as exc:
            st.error(str(exc), icon="🛑")
        else:
            st.session_state.demo_tab_samples_confirm = False
            if removed:
                st.success(
                    "Removed: "
                    + ", ".join(f"{n} {label}" for label, n in removed.items()),
                    icon="✅",
                )
            else:
                st.info("Nothing to remove.")
            st.rerun()


def _system_tab(db, company):
    st.markdown("#### Environment")
    st.dataframe(
        pd.DataFrame(
            [
                {"Setting": "App version", "Value": cfg.APP_VERSION},
                {"Setting": "Build date", "Value": cfg.BUILD_DATE},
                {"Setting": "Database", "Value": cfg.DATABASE_URL.split("://")[0]},
                {"Setting": "Data directory", "Value": str(cfg.DATA_DIR)},
                {"Setting": "Tally test mode (default)", "Value": str(cfg.TALLY_TEST_MODE)},
                {"Setting": "Max upload size", "Value": f"{cfg.MAX_UPLOAD_MB} MB"},
                {"Setting": "Allowed file types", "Value": ", ".join(sorted(cfg.ALLOWED_EXTENSIONS))},
                {"Setting": "Session timeout", "Value": f"{cfg.SESSION_TIMEOUT_MINUTES} min"},
                # str() matters: one integer in an otherwise textual column
                # makes the dtype `object`, which Arrow refuses to convert, and
                # the whole table silently fails to render.
                {"Setting": "Company ID (tenant)", "Value": str(company.id)},
                {"Setting": "Account type",
                 "Value": "DEMO — clearable" if company.is_demo else "Real — permanent"},
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        f"{cfg.COMPANY_BRAND} — {cfg.BRAND_TAGLINE}. Support: {cfg.SUPPORT_CONTACT}."
    )

    with st.expander(f"What is in this build (v{cfg.APP_VERSION})"):
        st.caption(
            "If a feature described in the manual is missing from your screen, "
            "check this version against the one you were sent. A missing option "
            "usually means the update was not extracted over the old folder."
        )
        for version, note in cfg.CHANGELOG:
            st.markdown(f"- **v{version}** — {note}")
    st.markdown(
        "**Data isolation:** every table is keyed by company. Queries in the service "
        "layer are filtered by your company ID, so no other customer's bills, "
        "products or ledgers are visible from this account."
    )
