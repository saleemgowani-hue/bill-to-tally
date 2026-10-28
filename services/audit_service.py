"""Audit trail + processing-error recording."""
from __future__ import annotations

import traceback
from typing import Optional

from sqlalchemy.orm import Session

from database.models import AuditLog, ProcessingError
from utils.logging import get_logger
from utils.security import sanitize_text

log = get_logger("audit")


def record(
    session: Session,
    company_id: int,
    action: str,
    detail: str = "",
    user=None,
    invoice_id: Optional[int] = None,
    sales_invoice_id: Optional[int] = None,
    commit: bool = True,
) -> AuditLog:
    entry = AuditLog(
        company_id=company_id,
        user_id=getattr(user, "id", None),
        user_name=getattr(user, "full_name", "system"),
        invoice_id=invoice_id,
        sales_invoice_id=sales_invoice_id,
        action=action[:80],
        detail=sanitize_text(detail, 4000),
    )
    session.add(entry)
    if commit:
        session.commit()
    log.info("[company=%s] %s :: %s", company_id, action, detail[:200])
    return entry


def record_error(
    session: Session,
    company_id: int,
    stage: str,
    message: str,
    exc: Optional[BaseException] = None,
    invoice_id: Optional[int] = None,
    sales_invoice_id: Optional[int] = None,
    commit: bool = True,
) -> ProcessingError:
    tb = ""
    if exc is not None:
        tb = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[:8000]
    err = ProcessingError(
        company_id=company_id,
        invoice_id=invoice_id,
        sales_invoice_id=sales_invoice_id,
        stage=stage[:60],
        message=sanitize_text(message, 2000),
        traceback_text=tb,
    )
    session.add(err)
    if commit:
        session.commit()
    log.error("[company=%s][%s] %s", company_id, stage, message)
    return err


def field_change(
    session: Session,
    company_id: int,
    user,
    invoice_id: Optional[int],
    field: str,
    old_value,
    new_value,
    sales_invoice_id: Optional[int] = None,
) -> None:
    if str(old_value) == str(new_value):
        return
    record(
        session,
        company_id,
        "FIELD_EDITED",
        f"Changed {field} from '{old_value}' to '{new_value}'",
        user=user,
        invoice_id=invoice_id,
        sales_invoice_id=sales_invoice_id,
    )
