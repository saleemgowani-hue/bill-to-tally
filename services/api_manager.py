"""
API manager: which Gemini key a company uses, how much it has used, and
whether it may make another call.

Key modes (setting "gemini_key_mode", per company):
  customer — the company's own key (setting "gemini_api_key", encrypted).
  central  — SN Softech's key:
               * SaaS server: settings.GEMINI_API_KEY (server environment,
                 never stored in the database, never shown to customers);
               * local/offline install: the SN Softech Cloud AI proxy, using
                 the company's licence key (setting "proxy_api_key").
Default: customer if the company has saved its own key, otherwise central.

Usage limits apply to the CENTRAL key only (a customer's own key is billed
to the customer by Google). Limit = setting "api_monthly_limit" (calls per
calendar month, 0 = unlimited), defaulting to settings.CENTRAL_API_MONTHLY_LIMIT.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.database import get_setting, set_setting
from database.models import ApiUsage, Company
from services.gemini_engine import estimate_cost_inr
from services.ocr_service import OCRConfigError, get_provider
from utils.config import settings
from utils.logging import get_logger
from utils.security import decrypt_secret, encrypt_secret

log = get_logger("api_manager")

KEY_MODES = {
    "central": "SN Softech central key (no key needed)",
    "customer": "My own Gemini API key",
}


@dataclass
class UsageSummary:
    calls: int = 0
    pages: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    cost_inr: float = 0.0
    failures: int = 0
    limit: int = 0

    @property
    def remaining(self) -> Optional[int]:
        return None if not self.limit else max(self.limit - self.calls, 0)


def month_start(now: Optional[dt.datetime] = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def own_key(session: Session, company_id: int) -> str:
    return decrypt_secret(get_setting(session, company_id, "gemini_api_key", "") or "")


def save_own_key(session: Session, company_id: int, key: str) -> None:
    set_setting(session, company_id, "gemini_api_key", encrypt_secret(key.strip()), True)


def key_mode(session: Session, company_id: int) -> str:
    mode = get_setting(session, company_id, "gemini_key_mode", "") or ""
    if mode in KEY_MODES:
        return mode
    return "customer" if own_key(session, company_id) else "central"


def monthly_limit(session: Session, company_id: int) -> int:
    raw = get_setting(session, company_id, "api_monthly_limit", "")
    try:
        return int(raw) if str(raw).strip() != "" else int(settings.CENTRAL_API_MONTHLY_LIMIT)
    except (TypeError, ValueError):
        return int(settings.CENTRAL_API_MONTHLY_LIMIT)


def usage_summary(session: Session, company_id: Optional[int] = None,
                  since: Optional[dt.datetime] = None,
                  key_mode_filter: Optional[str] = None) -> UsageSummary:
    query = session.query(
        func.count(ApiUsage.id),
        func.coalesce(func.sum(ApiUsage.pages), 0),
        func.coalesce(func.sum(ApiUsage.prompt_tokens), 0),
        func.coalesce(func.sum(ApiUsage.output_tokens), 0),
        func.coalesce(func.sum(ApiUsage.cost_inr), 0.0),
    )
    fail_query = session.query(func.count(ApiUsage.id)).filter(ApiUsage.success.is_(False))
    if company_id is not None:
        query = query.filter(ApiUsage.company_id == company_id)
        fail_query = fail_query.filter(ApiUsage.company_id == company_id)
    if since is not None:
        query = query.filter(ApiUsage.created_at >= since)
        fail_query = fail_query.filter(ApiUsage.created_at >= since)
    if key_mode_filter:
        query = query.filter(ApiUsage.key_mode == key_mode_filter)
    calls, pages, ptok, otok, cost = query.one()
    summary = UsageSummary(
        calls=int(calls or 0), pages=int(pages or 0), prompt_tokens=int(ptok or 0),
        output_tokens=int(otok or 0), cost_inr=round(float(cost or 0), 2),
        failures=int(fail_query.scalar() or 0),
    )
    if company_id is not None:
        summary.limit = monthly_limit(session, company_id)
    return summary


def check_limit(session: Session, company_id: int) -> None:
    """Raise OCRConfigError when the central-key monthly limit is used up."""
    if key_mode(session, company_id) != "central":
        return
    limit = monthly_limit(session, company_id)
    if not limit:
        return
    used = usage_summary(session, company_id, month_start(), key_mode_filter="central").calls
    if used >= limit:
        raise OCRConfigError(
            f"This month's AI processing limit ({limit} requests) is used up. "
            "Contact SN Softech Solutions to raise it, or add your own Gemini key "
            "in API Settings."
        )


def record_usage(session: Session, company_id: int, record: Dict,
                 user_id: Optional[int] = None, document_name: str = "") -> None:
    try:
        row = ApiUsage(
            company_id=company_id,
            user_id=user_id,
            provider=str(record.get("provider", ""))[:40],
            model=str(record.get("model", ""))[:80],
            key_mode=str(record.get("key_mode", ""))[:20],
            pages=int(record.get("pages") or 0),
            prompt_tokens=int(record.get("prompt_tokens") or 0),
            output_tokens=int(record.get("output_tokens") or 0),
            total_tokens=int(record.get("total_tokens") or 0),
            cost_inr=estimate_cost_inr(
                int(record.get("prompt_tokens") or 0), int(record.get("output_tokens") or 0)
            ),
            duration_ms=int(record.get("duration_ms") or 0),
            success=bool(record.get("success", True)),
            error=(record.get("error") or "")[:2000] or None,
            document_name=document_name[:260],
        )
        session.add(row)
        session.commit()
    except Exception as exc:  # never let bookkeeping break extraction
        session.rollback()
        log.warning("Could not record API usage: %s", exc)


def universal_config(session: Session, company: Company, user_id: Optional[int] = None,
                     document_name: str = "") -> Dict:
    """Engine config for GeminiUniversalProvider for this company.

    If the central monthly limit is used up, the config carries a
    blocked_reason: the engine refuses to call Gemini and the parser falls
    back to the offline engine.
    """
    mode = key_mode(session, company.id)
    blocked = ""
    try:
        check_limit(session, company.id)
    except OCRConfigError as exc:
        blocked = str(exc)
    config: Dict = {
        "key_mode": mode,
        "model": get_setting(session, company.id, "universal_model", "") or settings.GEMINI_MODEL,
        "pages_per_call": get_setting(session, company.id, "pages_per_call", "")
        or settings.GEMINI_PAGES_PER_CALL,
        "blocked_reason": blocked,
        "on_usage": lambda rec: record_usage(
            session, company.id, rec, user_id=user_id, document_name=document_name
        ),
    }
    if mode == "customer":
        config["api_key"] = own_key(session, company.id)
        config["transport"] = "direct"
    elif settings.GEMINI_API_KEY:
        config["api_key"] = settings.GEMINI_API_KEY
        config["transport"] = "direct"
    else:
        config["transport"] = "proxy"
        config["proxy_url"] = settings.OCR_PROXY_URL
        config["licence_key"] = decrypt_secret(
            get_setting(session, company.id, "proxy_api_key", "") or ""
        )
    return config


def offline_fallback_provider(session: Session, company: Company):
    """The offline engine used when Gemini is unreachable. None = disabled."""
    name = get_setting(session, company.id, "offline_fallback", "rapidocr") or ""
    if name in ("", "none"):
        return None
    config = {}
    if name == "tesseract":
        config["tesseract_cmd"] = get_setting(session, company.id, "tesseract_cmd", "")
    return get_provider(name, config)


def recent_usage(session: Session, company_id: Optional[int] = None, limit: int = 50) -> List[ApiUsage]:
    query = session.query(ApiUsage)
    if company_id is not None:
        query = query.filter(ApiUsage.company_id == company_id)
    return query.order_by(ApiUsage.created_at.desc()).limit(limit).all()


def universal_provider(session: Session, company: Company, user_id: Optional[int] = None,
                       document_name: str = ""):
    """Ready-to-use Universal (Gemini) engine for this company."""
    from services.gemini_engine import GeminiUniversalProvider
    return GeminiUniversalProvider(
        universal_config(session, company, user_id=user_id, document_name=document_name)
    )
