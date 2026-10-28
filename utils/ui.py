"""Shared Streamlit presentation helpers."""
from __future__ import annotations

import datetime as dt
from typing import Optional

import streamlit as st

from database.database import get_session
from database.models import Company, InvoiceStatus, MatchStatus, User
from utils.config import settings

CSS = """
<style>
  .block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1400px;}
  .kpi {
      background: linear-gradient(135deg, #ffffff 0%, #f4f7fb 100%);
      border: 1px solid #e3e8ef; border-radius: 14px; padding: 16px 18px;
      box-shadow: 0 1px 3px rgba(16,24,40,.06); height: 100%;
  }
  .kpi .label {font-size: .78rem; color:#667085; text-transform:uppercase;
      letter-spacing:.04em; font-weight:600; margin-bottom:6px;}
  .kpi .value {font-size: 1.7rem; font-weight:700; color:#101828; line-height:1.1;}
  .kpi .sub {font-size:.75rem; color:#98a2b3; margin-top:4px;}
  .kpi.accent-green {border-left:4px solid #12b76a;}
  .kpi.accent-amber {border-left:4px solid #f79009;}
  .kpi.accent-red   {border-left:4px solid #f04438;}
  .kpi.accent-blue  {border-left:4px solid #2e90fa;}
  .badge {display:inline-block; padding:3px 10px; border-radius:999px;
      font-size:.72rem; font-weight:700; letter-spacing:.02em;}
  .badge-green {background:#d1fadf; color:#027a48;}
  .badge-amber {background:#fef0c7; color:#b54708;}
  .badge-red   {background:#fee4e2; color:#b42318;}
  .badge-blue  {background:#d1e9ff; color:#175cd3;}
  .badge-grey  {background:#eaecf0; color:#475467;}
  .brandbar {border-radius:12px; padding:14px 18px; margin-bottom:14px;
      background:linear-gradient(90deg,#0b4a6f 0%, #1570ef 100%); color:#fff;}
  .brandbar h3 {margin:0; font-size:1.05rem; color:#fff;}
  .brandbar p {margin:2px 0 0; font-size:.78rem; opacity:.85;}
  .testmode {background:#fef0c7; border:1px solid #fdb022; color:#93370d;
      padding:8px 12px; border-radius:10px; font-size:.82rem; font-weight:600;
      margin-bottom:10px;}
  .muted {color:#667085; font-size:.82rem;}
  div[data-testid="stMetricValue"] {font-size:1.5rem;}

  /* ---------- coloured sidebar navigation ---------- */
  section[data-testid="stSidebar"] {
      background: linear-gradient(180deg, #0b2e4a 0%, #103d63 55%, #0b4a6f 100%);
  }
  section[data-testid="stSidebar"] * { color: #e8f1f8; }
  section[data-testid="stSidebar"] hr { border-color: rgba(255,255,255,.16); }

  /* The blanket rule above paints every descendant near-white, which is right
     for plain text on a dark sidebar and wrong for anything carrying its own
     pale background: the licence chip, an inline `code` span and the Sign out
     button all ended up light-on-light, i.e. invisible. Each one gets its
     colour back below. */
  section[data-testid="stSidebar"] .lic-ok,
  section[data-testid="stSidebar"] .lic-ok * { color:#05603a; }
  section[data-testid="stSidebar"] .lic-warn,
  section[data-testid="stSidebar"] .lic-warn * { color:#93370d; }
  section[data-testid="stSidebar"] .lic-bad,
  section[data-testid="stSidebar"] .lic-bad * { color:#912018; }

  /* Inline code reads as a chip on the dark ground rather than borrowing
     Streamlit's light-theme colours. */
  section[data-testid="stSidebar"] code {
      background: rgba(255,255,255,.14) !important;
      color: #ffd88a !important;
      border-radius: 6px; padding: 1px 6px; font-size: .78rem;
  }

  /* Sign out was a white button carrying white text. It is keyed by name
     rather than by `.stButton`, so it cannot collide with the coloured nav
     buttons, which set their own background with !important. An outlined
     ghost also stops it shouting louder than the navigation above it. */
  section[data-testid="stSidebar"] div[class*="st-key-signout"] button {
      background: rgba(255,255,255,.10) !important;
      border: 1px solid rgba(255,255,255,.32) !important;
      color: #ffffff !important;
      border-radius: 10px; font-weight: 650;
      box-shadow: none;
  }
  section[data-testid="stSidebar"] div[class*="st-key-signout"] button p,
  section[data-testid="stSidebar"] div[class*="st-key-signout"] button span,
  section[data-testid="stSidebar"] div[class*="st-key-signout"] button div {
      color: #ffffff !important;
  }
  section[data-testid="stSidebar"] div[class*="st-key-signout"] button:hover {
      background: rgba(255,255,255,.22) !important;
      border-color: rgba(255,255,255,.55) !important;
  }
  .sidebrand {
      padding: 6px 2px 10px; border-bottom: 1px solid rgba(255,255,255,.14);
      margin-bottom: 10px;
  }
  .sidebrand .t { font-size: 1.02rem; font-weight: 800; letter-spacing:.01em; }
  .sidebrand .s { font-size: .72rem; opacity:.75; }
  .sidecard {
      background: rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.14);
      border-radius: 10px; padding: 9px 11px; margin-bottom: 12px;
  }
  .sidecard .c { font-weight: 700; font-size: .88rem; }
  .sidecard .u { font-size: .74rem; opacity: .8; }

  /* every nav button shares this shape; colour comes from the st-key rules */
  section[data-testid="stSidebar"] div[class*="st-key-nav_"] button {
      width: 100%; justify-content: flex-start; text-align: left;
      border: none; border-radius: 10px; padding: .5rem .75rem;
      margin: 3px 0; font-weight: 650; font-size: .875rem;
      color: #ffffff !important; transition: transform .08s ease, filter .12s ease;
      box-shadow: 0 1px 2px rgba(0,0,0,.18);
  }
  section[data-testid="stSidebar"] div[class*="st-key-nav_"] button:hover {
      transform: translateX(2px); filter: brightness(1.12);
  }
  section[data-testid="stSidebar"] div[class*="st-key-nav_"] button p {
      color: #ffffff !important; font-weight: 650;
  }
  /* the active page gets a bright rail on the left */
  section[data-testid="stSidebar"] div[class*="st-key-nav_"] button[kind="primary"] {
      box-shadow: inset 4px 0 0 #ffffff, 0 2px 6px rgba(0,0,0,.28);
      filter: brightness(1.14);
  }

  /* ---------- dashboard ---------- */
  .hero {
      border-radius: 16px; padding: 20px 24px; margin-bottom: 18px; color:#fff;
      background: linear-gradient(115deg,#0b2e4a 0%,#1570ef 60%,#2e90fa 100%);
      box-shadow: 0 6px 20px rgba(11,46,74,.22);
  }
  .hero h2 {margin:0; font-size:1.35rem; color:#fff; font-weight:800;}
  .hero p {margin:6px 0 0; font-size:.85rem; opacity:.9;}
  .hero .pill {
      display:inline-block; background:rgba(255,255,255,.16); border-radius:999px;
      padding:4px 12px; font-size:.74rem; font-weight:700; margin-top:10px;
      border:1px solid rgba(255,255,255,.25);
  }
  .tile {
      border-radius:14px; padding:16px 18px; color:#fff; height:100%;
      box-shadow:0 4px 14px rgba(16,24,40,.14); position:relative; overflow:hidden;
  }
  .tile .label {font-size:.72rem; text-transform:uppercase; letter-spacing:.05em;
      font-weight:700; opacity:.92;}
  .tile .value {font-size:1.85rem; font-weight:800; line-height:1.15; margin-top:4px;}
  .tile .sub {font-size:.73rem; opacity:.88; margin-top:3px;}
  .tile .glyph {position:absolute; right:12px; top:10px; font-size:1.5rem; opacity:.28;}
  .t-blue   {background:linear-gradient(135deg,#1570ef,#2e90fa);}
  .t-green  {background:linear-gradient(135deg,#039855,#32d583);}
  .t-amber  {background:linear-gradient(135deg,#dc6803,#fdb022);}
  .t-red    {background:linear-gradient(135deg,#d92d20,#f97066);}
  .t-purple {background:linear-gradient(135deg,#6941c6,#9e77ed);}
  .t-teal   {background:linear-gradient(135deg,#0e7090,#22ccee);}
  .t-slate  {background:linear-gradient(135deg,#344054,#667085);}
  .t-pink   {background:linear-gradient(135deg,#c11574,#ee46bc);}
  .panel {
      background:#fff; border:1px solid #e3e8ef; border-radius:14px;
      padding:16px 18px; box-shadow:0 1px 3px rgba(16,24,40,.06); height:100%;
  }
  .panel h4 {margin:0 0 10px; font-size:.95rem; color:#101828; font-weight:700;}
  .lic-ok   {background:#d1fadf; color:#027a48; border:1px solid #6ce9a6;
      padding:8px 12px; border-radius:10px; font-size:.8rem; font-weight:600;}
  .lic-warn {background:#fef0c7; color:#b54708; border:1px solid #fdb022;
      padding:8px 12px; border-radius:10px; font-size:.8rem; font-weight:600;}
  .lic-bad  {background:#fee4e2; color:#b42318; border:1px solid #fda29b;
      padding:8px 12px; border-radius:10px; font-size:.8rem; font-weight:600;}
  .demobar {
      background:linear-gradient(90deg,#6941c6,#9e77ed); color:#fff;
      padding:9px 14px; border-radius:10px; font-size:.8rem; font-weight:600;
      margin-bottom:12px;
  }
</style>
"""


def page_setup(title: str, icon: str = "🧾") -> None:
    st.set_page_config(
        page_title=f"{title} · {settings.APP_NAME}",
        page_icon=icon,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(CSS, unsafe_allow_html=True)


def brand_header(subtitle: str = "") -> None:
    st.markdown(
        f"""<div class="brandbar">
              <h3>{settings.APP_NAME}</h3>
              <p>{settings.COMPANY_BRAND} — {settings.BRAND_TAGLINE}
              {' · ' + subtitle if subtitle else ''}</p>
            </div>""",
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Session / auth
# --------------------------------------------------------------------------
def session_db():
    """One SQLAlchemy session per Streamlit script run."""
    if "db" not in st.session_state or st.session_state.db is None:
        st.session_state.db = get_session()
    return st.session_state.db


def current_user() -> Optional[User]:
    user_id = st.session_state.get("user_id")
    if not user_id:
        return None
    expires = st.session_state.get("session_expires")
    if expires and dt.datetime.utcnow() > expires:
        logout()
        return None
    return session_db().query(User).filter(User.id == user_id).one_or_none()


def current_company() -> Optional[Company]:
    user = current_user()
    if not user:
        return None
    return session_db().query(Company).filter(Company.id == user.company_id).one_or_none()


def login_user(user: User) -> None:
    st.session_state.user_id = user.id
    st.session_state.company_id = user.company_id
    st.session_state.session_expires = dt.datetime.utcnow() + dt.timedelta(
        minutes=settings.SESSION_TIMEOUT_MINUTES
    )


def logout() -> None:
    for key in list(st.session_state.keys()):
        if key != "db":
            del st.session_state[key]
    st.session_state.db = None


def require_login() -> tuple[User, Company]:
    user = current_user()
    if user is None:
        st.warning("Please sign in to continue.")
        st.stop()
    company = current_company()
    if company is None:
        st.error("Your company record could not be loaded.")
        st.stop()
    return user, company


def require_role(user: User, *roles: str) -> None:
    if user.role not in roles:
        st.error(
            f"This action needs the {' or '.join(roles)} role. "
            f"You are signed in as {user.role}."
        )
        st.stop()


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------
def kpi(label: str, value, sub: str = "", accent: str = "blue") -> None:
    st.markdown(
        f"""<div class="kpi accent-{accent}">
              <div class="label">{label}</div>
              <div class="value">{value}</div>
              <div class="sub">{sub}&nbsp;</div>
            </div>""",
        unsafe_allow_html=True,
    )


def badge(text: str, tone: str = "grey") -> str:
    return f'<span class="badge badge-{tone}">{text}</span>'


STATUS_TONE = {
    InvoiceStatus.PROCESSING.value: "blue",
    InvoiceStatus.REVIEW_REQUIRED.value: "amber",
    InvoiceStatus.CONFIRMED.value: "blue",
    InvoiceStatus.READY.value: "blue",
    InvoiceStatus.EXPORTED.value: "amber",   # waiting on a manual Tally import
    InvoiceStatus.POSTED.value: "green",
    InvoiceStatus.FAILED.value: "red",
    InvoiceStatus.DUPLICATE.value: "amber",
    InvoiceStatus.CANCELLED.value: "grey",
}

MATCH_TONE = {
    MatchStatus.MATCHED.value: "green",
    MatchStatus.POSSIBLE.value: "amber",
    MatchStatus.NEW.value: "red",
    MatchStatus.NEW_BARCODE.value: "red",
}


def status_badge(status: str) -> str:
    return badge(status, STATUS_TONE.get(status, "grey"))


def match_badge(status: str) -> str:
    return badge(status, MATCH_TONE.get(status, "grey"))


def confidence_tone(score: float) -> str:
    if score >= settings.CONFIDENCE_HIGH:
        return "green"
    if score >= settings.CONFIDENCE_REVIEW:
        return "amber"
    return "red"


def confidence_light(score: float) -> str:
    return {"green": "🟢", "amber": "🟡", "red": "🔴"}[confidence_tone(score or 0)]


def rupees(value) -> str:
    try:
        return f"₹{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "₹0.00"


def test_mode_banner(is_test: bool, connector_label: str = "") -> None:
    if is_test:
        st.markdown(
            f'<div class="testmode">🧪 TALLY TEST MODE IS ON — vouchers are '
            f"previewed and exported, never posted to a live company. "
            f"{connector_label}</div>",
            unsafe_allow_html=True,
        )


def show_issues(report) -> None:
    """Render a ValidationReport."""
    if report is None:
        return
    for issue in report.errors:
        st.error(f"**{issue.field}** — {issue.message}")
    for issue in report.warnings:
        st.warning(f"**{issue.field}** — {issue.message}")
    infos = [i for i in report.issues if i.severity == "info"]
    for issue in infos:
        st.info(f"{issue.message}")
    if not report.issues:
        st.success("All arithmetic and GST checks passed.")


def friendly_error(stage: str, exc: BaseException) -> None:
    st.error(
        f"**{stage} could not be completed.**\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        "Nothing was saved to your accounts. Use Retry, edit manually, or save a draft."
    )


# --------------------------------------------------------------------------
# Dashboard tiles
# --------------------------------------------------------------------------
TILE_COLORS = (
    "t-blue", "t-green", "t-amber", "t-red",
    "t-purple", "t-teal", "t-slate", "t-pink",
)


def tile(label: str, value, sub: str = "", color: str = "t-blue", glyph: str = "") -> str:
    """A coloured KPI tile. Returns HTML so callers can place it in any column."""
    return (
        f'<div class="tile {color}">'
        f'<div class="glyph">{glyph}</div>'
        f'<div class="label">{label}</div>'
        f'<div class="value">{value}</div>'
        f'<div class="sub">{sub}</div>'
        f"</div>"
    )


def panel_open(title: str) -> str:
    return f'<div class="panel"><h4>{title}</h4>'


def compact(number: float) -> str:
    """1234567 -> 12.3L, in the Indian numbering people actually use."""
    try:
        number = float(number)
    except (TypeError, ValueError):
        return "0"
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 1_00_00_000:
        return f"{sign}{number / 1_00_00_000:.2f}Cr"
    if number >= 1_00_000:
        return f"{sign}{number / 1_00_000:.2f}L"
    if number >= 1_000:
        return f"{sign}{number / 1_000:.1f}K"
    return f"{sign}{number:,.0f}"
