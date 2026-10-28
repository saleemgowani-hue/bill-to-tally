"""
Central configuration.

Everything is read from environment variables (optionally loaded from a .env
file) so that no secret is ever hard-coded in the source tree.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader (avoids a hard dependency on python-dotenv)."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        os.environ.setdefault(key, _clean_value(value))


def _clean_value(value: str) -> str:
    """Strip quotes and any trailing inline comment.

    Without this, `LICENSE_REQUIRED=true   # false disables the check` reads as
    the whole string, which is not "true" — so the setting silently flips to
    False and licence enforcement turns itself off. A '#' inside quotes, or one
    with no space before it, is kept: it may be part of a password.
    """
    value = value.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    marker = value.find(" #")
    if marker >= 0:
        value = value[:marker]
    marker = value.find("\t#")
    if marker >= 0:
        value = value[:marker]
    return value.strip()


_load_dotenv()


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Settings:
    """Application settings snapshot."""

    APP_NAME = os.getenv("APP_NAME", "AI Bill-to-Tally Agent")
    COMPANY_BRAND = os.getenv("COMPANY_BRAND", "SN Softech Solutions")
    BRAND_TAGLINE = os.getenv(
        "BRAND_TAGLINE", "Smart Software Solutions for Growing Businesses"
    )
    SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "9993199719")
    # Bump both on every build you send out. They are shown in the sidebar and
    # in Settings → System so that "the new option is missing" can be answered
    # by looking, instead of by guessing whether the update was extracted.
    APP_VERSION = "1.3.3"
    BUILD_DATE = "2026-10-09"
    # One line per release, newest first, shown in Settings → System.
    CHANGELOG = [
        ("1.3.3", "CREATE_DESKTOP_SHORTCUT.bat: one double-click puts an "
                  "app icon on the Desktop"),
        ("1.3.2", "Manifest to Excel: dates are shown as DD/MM/YYYY (real "
                  "Excel dates, so they sort and filter)"),
        ("1.3.1", "Manifest to Excel: S.No now continues from one bill to the "
                  "next in the combined sheet; From/To keep just the station "
                  "name (no street address); dates written as 05.10.26 read "
                  "as 2026-10-05"),
        ("1.3.0", "New Manifest to Excel page: photo/PDF of a handwritten "
                  "transport manifest to Excel. Add several bills at once - "
                  "they are read one after another into ONE workbook "
                  "(combined sheet, summary, a sheet per bill)"),
        ("1.2.2", "Clear demo data no longer fails when the demo created a "
                  "product, a learned mapping, a manual stock adjustment, a "
                  "duplicate bill or a failed upload"),
        ("1.2.1", "Sidebar readability: the licence chip, the OCR provider chip "
                  "and the Sign out button were light text on a light fill"),
        ("1.2.0", "Excel-only option in Tally Connection; Excel export tab in "
                  "Reports; reports now include exported bills and say where "
                  "the rest are waiting; demo sample data labelled and removable"),
        ("1.1.0", "Demo account with a one-button clear; automatic dependency "
                  "install on every launch"),
        ("1.0.0", "First release"),
    ]

    # ---- licensing ----
    # Keys are signed with this secret. Change it and every key you have
    # already issued stops validating, so set it once and keep it safe.
    # No default on purpose. A secret baked into the source is no secret: anyone
    # holding the code could mint unlimited valid keys. It must come from .env,
    # and the app refuses to validate licences until it does.
    LICENSE_SIGNING_SECRET = os.getenv("LICENSE_SIGNING_SECRET", "").strip()
    LICENSE_SECRET_MIN_LENGTH = 16
    LICENSE_WARN_DAYS = _i("LICENSE_WARN_DAYS", 10)
    LICENSE_REQUIRED = _b("LICENSE_REQUIRED", True)

    # ---- demo accounts ----
    # 0 = never clear on a timer. Clearing a demo is a deliberate act between
    # prospects; a countdown that empties the screen mid-presentation is worse
    # than no housekeeping at all. Set a value only for an unattended demo.
    DEMO_RETENTION_MINUTES = _i("DEMO_RETENTION_MINUTES", 0)
    DEMO_SWEEP_INTERVAL_SECONDS = _i("DEMO_SWEEP_INTERVAL_SECONDS", 120)


    # ---- storage -------------------------------------------------------
    DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
    UPLOAD_DIR = DATA_DIR / "uploads"
    EXPORT_DIR = DATA_DIR / "exports"
    LOG_DIR = DATA_DIR / "logs"

    # SQLite for development, PostgreSQL for SaaS production.
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'billtotally.db'}")

    # ---- OCR / AI ------------------------------------------------------
    # mock | tesseract | paddle | gemini | openai | claude
    OCR_PROVIDER = os.getenv("OCR_PROVIDER", "mock").strip().lower()
    OCR_TIMEOUT_SECONDS = _i("OCR_TIMEOUT_SECONDS", 120)

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    TESSERACT_CMD = os.getenv("TESSERACT_CMD", "")
    # The vendor's own OCR relay (see ocr_proxy/). Baked into every customer
    # build by make_customer_build.py - a customer never sets this
    # themselves, only enters their licence key against it in Settings.
    OCR_PROXY_URL = os.getenv("OCR_PROXY_URL", "")

    # ---- confidence thresholds ----------------------------------------
    CONFIDENCE_HIGH = _f("CONFIDENCE_HIGH", 0.90)
    CONFIDENCE_REVIEW = _f("CONFIDENCE_REVIEW", 0.70)

    # ---- product matching ---------------------------------------------
    MATCH_AUTO_THRESHOLD = _f("MATCH_AUTO_THRESHOLD", 0.92)
    MATCH_SUGGEST_THRESHOLD = _f("MATCH_SUGGEST_THRESHOLD", 0.60)

    # ---- validation ----------------------------------------------------
    ROUNDING_TOLERANCE = _f("ROUNDING_TOLERANCE", 1.0)  # rupees

    # ---- Tally ---------------------------------------------------------
    TALLY_TEST_MODE = _b("TALLY_TEST_MODE", True)
    TALLY_HOST = os.getenv("TALLY_HOST", "localhost")
    TALLY_PORT = _i("TALLY_PORT", 9000)
    TALLY_COMPANY = os.getenv("TALLY_COMPANY", "")
    TALLY_TIMEOUT_SECONDS = _i("TALLY_TIMEOUT_SECONDS", 30)

    # ---- security ------------------------------------------------------
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-insecure-key-change-me")
    SESSION_TIMEOUT_MINUTES = _i("SESSION_TIMEOUT_MINUTES", 480)
    MAX_UPLOAD_MB = _i("MAX_UPLOAD_MB", 15)
    ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "pdf"}

    @classmethod
    def ensure_dirs(cls) -> None:
        for path in (cls.DATA_DIR, cls.UPLOAD_DIR, cls.EXPORT_DIR, cls.LOG_DIR):
            Path(path).mkdir(parents=True, exist_ok=True)

    @classmethod
    def is_production_db(cls) -> bool:
        return cls.DATABASE_URL.startswith("postgres")


settings = Settings()
settings.ensure_dirs()
