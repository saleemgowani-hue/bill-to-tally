"""
AI Bill-to-Tally Stock Update Agent
SN Softech Solutions — Smart Software Solutions for Growing Businesses

Entry point: authentication, navigation and routing.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import datetime as dt

import streamlit as st

from database.database import (
    create_company_with_admin,
    init_db,
    seed_tally_mappings,
)
from database.models import Company, Role, User
from services import audit_service, demo_service, license_service
from services.license_service import LicenseError
from utils.config import settings
from utils.logging import get_logger
from utils.security import (
    generate_reset_token,
    hash_password,
    password_problems,
    verify_password,
)
from utils.ui import (
    brand_header,
    current_company,
    current_user,
    login_user,
    logout,
    page_setup,
    session_db,
)

log = get_logger("app")

page_setup("Home")
init_db()


# --------------------------------------------------------------------------
# Authentication screens
# --------------------------------------------------------------------------
def sign_in_tab(db) -> None:
    st.subheader("Sign in")
    # A form guarantees every field is submitted together with the button.
    # Outside a form, a value the browser filled in (autofill) or a field the
    # user never left can still be empty on the server, which looks to the
    # user like the app ignoring what is plainly on screen.
    with st.form("sign_in_form"):
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_password")
        submitted = st.form_submit_button("Sign in", type="primary", width="stretch")

    if not submitted:
        return

    email = (email or "").strip().lower()
    missing = [
        label for label, value in (("Email", email), ("Password", password))
        if not value
    ]
    if missing:
        st.error("Please fill in: " + ", ".join(missing) + ".")
        return

    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        st.error("Email or password is incorrect.")
        return
    if not user.is_active:
        st.error("This account has been deactivated. Contact your administrator.")
        return

    # No licence key is asked for here — the key is a sign-up/renewal step.
    # An expired licence is still caught after login by license_blocked_screen(),
    # which lets the user enter a new key without losing any data.
    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    login_user(user)
    audit_service.record(db, user.company_id, "USER_LOGIN", f"{user.email} signed in", user=user)
    st.rerun()


def sign_up_tab(db) -> None:
    st.subheader("Create a company account")
    st.caption("The first user of a company becomes its Admin.")

    with st.form("sign_up_form"):
        st.markdown("**Licence key**")
        license_key = st.text_input(
            "Licence key",
            placeholder="SNST-XXXX-XXXX-XXXX-XXXX",
            label_visibility="collapsed",
            key="signup_key",
            help=(
                "Monthly or yearly key supplied by SN Softech Solutions. "
                "Dashes and spaces are optional."
            ),
        )
        st.caption(
            f"No key? Contact {settings.COMPANY_BRAND} on {settings.SUPPORT_CONTACT}."
        )

        st.divider()
        company_name = st.text_input("Company / business name", key="signup_company")
        col1, col2 = st.columns(2)
        gstin = col1.text_input(
            "GSTIN (optional)", key="signup_gstin",
            help="Used to detect intra vs inter-state purchases",
        )
        state = col2.text_input("State (optional)", key="signup_state")
        full_name = st.text_input("Your full name", key="signup_name")
        email = st.text_input("Work email", key="signup_email")
        col3, col4 = st.columns(2)
        password = col3.text_input("Password", type="password", key="signup_pw")
        confirm = col4.text_input(
            "Confirm password", type="password", key="signup_pw2"
        )
        submitted = st.form_submit_button(
            "Create account", type="primary", width="stretch"
        )

    if not submitted:
        return

    company_name = (company_name or "").strip()
    full_name = (full_name or "").strip()
    email = (email or "").strip().lower()

    # Name the field that is actually missing. "Some fields are required" makes
    # the user hunt, and is genuinely confusing when the browser has autofilled
    # a value that never reached us.
    missing = [
        label
        for label, value in (
            ("Company / business name", company_name),
            ("Your full name", full_name),
            ("Work email", email),
        )
        if not value
    ]
    if missing:
        st.error("Please fill in: " + ", ".join(missing) + ".")
        if len(missing) < 3:
            st.caption(
                "If the field looks filled on screen, click into it, retype the "
                "last character, then press Create account again — some browsers "
                "autofill without telling the app."
            )
        return

    if "@" not in email or "." not in email.split("@")[-1]:
        st.error("That does not look like a valid email address.")
        return

    key_info = None
    if settings.LICENSE_REQUIRED:
        try:
            key_info = license_service.check_available(db, license_key)
        except LicenseError as exc:
            st.error(str(exc), icon="⚠️")
            return

    problems = password_problems(password)
    if problems:
        st.error("Password needs " + ", ".join(problems) + ".")
        return
    if password != confirm:
        st.error("The two passwords do not match.")
        return
    if db.query(User).filter(User.email == email).first():
        st.error("An account with this email already exists.")
        return

    user = create_company_with_admin(
        db, company_name, full_name, email, password,
        (gstin or "").strip().upper(), (state or "").strip(),
    )
    if key_info is not None:
        company = db.query(Company).filter(Company.id == user.company_id).one()
        try:
            license_service.activate(db, company, license_key, activated_by=user.id)
        except LicenseError as exc:
            st.error(str(exc), icon="⚠️")
            return
        audit_service.record(
            db, user.company_id, "LICENCE_ACTIVATED",
            f"{key_info.plan} licence activated at sign up", user=user,
        )
    login_user(user)
    st.success("Account created. Welcome!")
    st.rerun()


def forgot_password_tab(db) -> None:
    st.subheader("Forgot password")
    st.caption(
        "This build issues a reset token on screen. Wire it to your email/SMS "
        "gateway before going live."
    )
    email = st.text_input("Account email", key="reset_email").strip().lower()
    if st.button("Generate reset token"):
        user = db.query(User).filter(User.email == email).one_or_none()
        if user is None:
            st.info("If that account exists, a reset token has been generated.")
            return
        user.reset_token = generate_reset_token()
        user.reset_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
        db.commit()
        audit_service.record(db, user.company_id, "PASSWORD_RESET_REQUESTED", email, user=user)
        st.code(user.reset_token, language="text")
        st.caption("Valid for 2 hours.")

    st.divider()
    token = st.text_input("Reset token")
    new_password = st.text_input("New password", type="password", key="reset_pw")
    if st.button("Set new password"):
        user = db.query(User).filter(User.reset_token == token.strip()).one_or_none()
        expiry = user.reset_expires_at if user else None
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=dt.timezone.utc)
        if user is None or not token.strip() or (expiry and expiry < dt.datetime.now(dt.timezone.utc)):
            st.error("That token is not valid or has expired.")
            return
        problems = password_problems(new_password)
        if problems:
            st.error("Password needs " + ", ".join(problems) + ".")
            return
        user.password_hash = hash_password(new_password)
        user.reset_token = None
        user.reset_expires_at = None
        db.commit()
        audit_service.record(db, user.company_id, "PASSWORD_RESET_COMPLETED", user.email, user=user)
        st.success("Password updated. You can sign in now.")


def auth_screen(db) -> None:
    left, mid, right = st.columns([1, 2, 1])
    with mid:
        brand_header("Take a photo of your purchase bill and let AI prepare your Tally entry.")
        tab1, tab2, tab3 = st.tabs(["Sign in", "Sign up", "Forgot password"])
        with tab1:
            sign_in_tab(db)
        with tab2:
            sign_up_tab(db)
        with tab3:
            forgot_password_tab(db)
        st.caption(
            f"{settings.COMPANY_BRAND} · support {settings.SUPPORT_CONTACT} · "
            f"v{settings.APP_VERSION} ({settings.BUILD_DATE})"
        )


# --------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------
# name -> (icon, module, button colour)
PAGES = {
    "Dashboard":        ("📊", "dashboard",        "#1570ef"),
    "Upload Bill":      ("📤", "upload_bill",      "#039855"),
    "Review Bills":     ("🔍", "review_bills",     "#dc6803"),
    "Products":         ("📦", "products",         "#6941c6"),
    "Suppliers":        ("🏭", "suppliers",        "#0e7090"),
    "Tally Connection": ("🔗", "tally",            "#c11574"),
    "Purchase History": ("🧾", "purchase_history", "#475467"),
    "Reports":          ("📈", "reports",          "#b54708"),
    "Settings":         ("⚙️", "settings",         "#344054"),
}


def _nav_css() -> str:
    """One CSS rule per nav button, keyed to its Streamlit container class."""
    rules = []
    for index, (_, _, colour) in enumerate(PAGES.values()):
        rules.append(
            f'section[data-testid="stSidebar"] div.st-key-nav_{index} button '
            f"{{background:{colour} !important;}}"
        )
    return "<style>" + "".join(rules) + "</style>"


def sidebar(user: User) -> str:
    company = current_company()
    names = list(PAGES.keys())
    if "nav" not in st.session_state or st.session_state.nav not in PAGES:
        st.session_state.nav = names[0]

    with st.sidebar:
        st.markdown(_nav_css(), unsafe_allow_html=True)
        st.markdown(
            f'<div class="sidebrand"><div class="t">🧾 {settings.APP_NAME}</div>'
            f'<div class="s">{settings.COMPANY_BRAND}</div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="sidecard"><div class="c">{company.name if company else "—"}</div>'
            f'<div class="u">{user.full_name} · {user.role}</div></div>',
            unsafe_allow_html=True,
        )

        for index, (name, (icon, _, _)) in enumerate(PAGES.items()):
            active = st.session_state.nav == name
            with st.container(key=f"nav_{index}"):
                if st.button(
                    f"{icon}  {name}",
                    key=f"navbtn_{index}",
                    type="primary" if active else "secondary",
                    width="stretch",
                ):
                    st.session_state.nav = name
                    st.rerun()

        st.divider()

        if company is not None:
            state = license_service.status(company)
            css = "lic-ok" if state.is_valid and not state.is_expiring_soon else (
                "lic-warn" if state.is_valid else "lic-bad"
            )
            label = (
                f"{state.plan.title()} · {state.days_left}d left"
                if state.is_valid else "Licence expired"
            )
            st.markdown(f'<div class="{css}">🔑 {label}</div>', unsafe_allow_html=True)

        if settings.TALLY_TEST_MODE:
            st.warning("Tally test mode ON", icon="🧪")
        st.caption(
            f"OCR provider: `{st.session_state.get('ocr_provider', settings.OCR_PROVIDER)}`"
        )
        if st.button("🚪  Sign out", width="stretch", key="signout"):
            logout()
            st.rerun()
        st.caption(
            f"v{settings.APP_VERSION} · build {settings.BUILD_DATE} · "
            f"support {settings.SUPPORT_CONTACT}"
        )
    return st.session_state.nav


def route(page_name: str) -> None:
    module_name = PAGES[page_name][1]
    module = __import__(f"app_pages.{module_name}", fromlist=["render"])
    try:
        module.render()
    except ModuleNotFoundError as exc:
        # An install that is missing a package needs an instruction, not a
        # stack trace. "No module named 'openpyxl'" means nothing to a shop
        # owner, and the fix is one line they can copy.
        log.exception("Page '%s' failed: missing package", page_name)
        missing = (exc.name or "a required package").split(".")[0]
        st.error(
            f"**This feature needs a package that is not installed: "
            f"`{missing}`.**",
            icon="📦",
        )
        st.markdown(
            "**To fix it**, close the app, then open a Command Prompt in the "
            "application folder and run:\n\n"
            "```\n.venv\\Scripts\\python.exe -m pip install -r requirements.txt\n```\n\n"
            "Then start the app again. Everything else keeps working in the "
            "meantime — your data is untouched."
        )
        st.caption(
            f"If that does not help, contact {settings.COMPANY_BRAND} on "
            f"{settings.SUPPORT_CONTACT}."
        )
    except Exception as exc:  # a page must never take the whole app down
        log.exception("Page '%s' failed", page_name)
        st.error(f"Something went wrong on this page: {type(exc).__name__}: {exc}")
        st.caption(
            "Your data is safe — no accounting entry is created when a page fails. "
            "Try again, or report this to support."
        )
        with st.expander("Technical detail"):
            st.exception(exc)


def license_blocked_screen(db, user: User, company: Company) -> None:
    """Shown instead of the app when the licence has run out."""
    left, mid, right = st.columns([1, 2, 1])
    with mid:
        brand_header("Licence renewal required")
        state = license_service.status(company)
        st.error(state.message, icon="🔒")
        st.markdown(
            f"**{company.name}** — your data is safe and untouched. Enter a new "
            "licence key below to carry on where you left off."
        )
        new_key = st.text_input("New licence key", placeholder="SNST-XXXX-XXXX-XXXX-XXXX")
        if st.button("Activate licence", type="primary", width="stretch"):
            try:
                license_service.activate(db, company, new_key, activated_by=user.id)
            except LicenseError as exc:
                st.error(str(exc), icon="⚠️")
            else:
                audit_service.record(
                    db, company.id, "LICENCE_RENEWED", "Licence renewed", user=user
                )
                st.success("Licence activated. Welcome back!")
                st.rerun()
        st.divider()
        st.caption(
            f"To buy or renew, contact {settings.COMPANY_BRAND} on "
            f"{settings.SUPPORT_CONTACT}."
        )
        if st.button("Sign out", width="stretch"):
            logout()
            st.rerun()


def demo_banner(db, company: Company) -> None:
    """The demo strip, with the clear-between-prospects button.

    Only ever drawn for a company flagged as a demo. A real company shows no
    banner and no button — there is nothing here to press by accident.
    """
    if not demo_service.is_demo(company):
        return

    remaining = demo_service.minutes_until_wipe(db, company)
    tail = (
        f" Clears automatically in about {remaining} minute(s)."
        if remaining is not None else ""
    )
    st.markdown(
        f'<div class="demobar">🧪 <b>DEMO ACCOUNT</b> — for showing the software. '
        f"Anything entered here can be cleared with one button."
        f"{tail} Do not use this account for real purchase records.</div>",
        unsafe_allow_html=True,
    )

    summary = demo_service.demo_data_summary(db, company)
    label = ", ".join(f"{count} {name}" for name, count in summary.items())

    left, right = st.columns([3, 1.4])
    with left:
        st.caption(
            f"Demo currently holds: **{label}**." if label
            else "Demo is clean — nothing has been entered yet."
        )
    with right:
        if st.session_state.get("confirm_clear_demo"):
            if st.button("⚠️ Yes, clear it all", key="do_clear_demo",
                         type="primary", width="stretch"):
                try:
                    result = demo_service.clear_demo_data(db, company, current_user())
                except demo_service.NotADemoCompany as exc:
                    st.error(str(exc))
                else:
                    st.session_state.confirm_clear_demo = False
                    st.session_state.pop("review_invoice_id", None)
                    st.success(
                        f"Demo cleared — {result.total_deleted} row(s) removed, "
                        f"{result.stock_reset} product(s) back to opening stock."
                    )
                    st.rerun()
            if st.button("Cancel", key="cancel_clear_demo", width="stretch"):
                st.session_state.confirm_clear_demo = False
                st.rerun()
        elif summary:
            if st.button("🧹 Clear demo data", key="ask_clear_demo", width="stretch"):
                st.session_state.confirm_clear_demo = True
                st.rerun()


def setup_required_screen(problem: str) -> None:
    """Shown when licensing cannot run because the secret is not configured.

    Better a plain instruction than a stack trace on the sign-up screen — and
    far better than quietly accepting any key.
    """
    left, mid, right = st.columns([1, 2, 1])
    with mid:
        brand_header("Setup needed")
        st.error(problem, icon="🔧")
        st.markdown(
            "**To finish setup:**\n\n"
            "1. Open the `.env` file in the application folder\n"
            "2. Find the line beginning `LICENSE_SIGNING_SECRET=`\n"
            "3. Put the value supplied with your licence after the `=`\n"
            "4. Save the file and start the app again"
        )
        st.caption(
            f"If you do not have this value, contact {settings.COMPANY_BRAND} "
            f"on {settings.SUPPORT_CONTACT}. Your data is untouched — the app "
            "simply will not check licences until this is set."
        )


def main() -> None:
    db = session_db()

    if settings.LICENSE_REQUIRED:
        problem = license_service.secret_problem()
        if problem:
            setup_required_screen(problem)
            return

    user = current_user()
    if user is None:
        auth_screen(db)
        return

    company = current_company()

    # Housekeeping for demo companies. Throttled internally, and a failure
    # here must never stop someone using the app.
    try:
        demo_service.sweep_all(db)
    except Exception:
        log.exception("Demo sweep failed")
        db.rollback()

    if settings.LICENSE_REQUIRED and company is not None:
        state = license_service.status(company)
        if not state.is_valid:
            license_blocked_screen(db, user, company)
            return

    seed_tally_mappings(db, user.company_id)
    page = sidebar(user)
    demo_banner(db, company)
    route(page)


main()
