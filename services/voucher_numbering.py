"""
Voucher numbering — choose the number Tally vouchers start from.

One series per voucher kind (purchase, sales, payment, receipt, journal,
contra, debit_note, credit_note), each with a mode:

  tally    Tally decides the number (default — the behaviour before this
           feature; no <VOUCHERNUMBER> is sent).
  custom   prefix + running number + suffix, starting from the number you
           choose:  PUR/ 0101 /26-27  ->  PUR/0101/26-27, PUR/0102/26-27 ...
  invoice  use the bill's own invoice number (sensible for Sales, where the
           printed bill number IS your sales voucher number).

Rules:
* A number is used up only after Tally (or the Excel/XML export) accepts the
  voucher. A failed post re-uses the same number next time — no gaps.
* TEST MODE never uses up a number.
* Tally only keeps the number we send when that voucher type's
  "Method of Voucher Numbering" is Manual or "Automatic (Manual Override)".
  With plain "Automatic" Tally renumbers it — the Tally Connection page
  says so next to the setting.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Dict, Optional

from sqlalchemy.orm import Session

from database.database import get_setting, set_setting

SERIES = {
    "purchase": "Purchase / खरीद",
    "sales": "Sales / बिक्री",
    "payment": "Payment / भुगतान",
    "receipt": "Receipt / प्राप्ति",
    "journal": "Journal / जर्नल",
    "contra": "Contra / कॉन्ट्रा",
    "debit_note": "Debit Note / डेबिट नोट",
    "credit_note": "Credit Note / क्रेडिट नोट",
}

MODES = {
    "tally": "Tally decides (automatic)",
    "custom": "My own series (choose start number)",
    "invoice": "Same as bill / invoice number",
}

DEFAULT_PREFIX = {
    "purchase": "PUR/", "sales": "SAL/", "payment": "PAY/", "receipt": "REC/",
    "journal": "JV/", "contra": "CON/", "debit_note": "DN/", "credit_note": "CN/",
}


@dataclass
class SeriesConfig:
    mode: str = "tally"
    prefix: str = ""
    next_number: int = 1
    digits: int = 1          # zero-padding width (4 -> 0001)
    suffix: str = ""

    def format(self, number: Optional[int] = None) -> str:
        n = self.next_number if number is None else number
        return f"{self.prefix}{str(int(n)).zfill(max(1, int(self.digits)))}{self.suffix}"


def _key(series: str) -> str:
    return f"vchnum_{series}"


def get_config(session: Session, company_id: int, series: str) -> SeriesConfig:
    raw = get_setting(session, company_id, _key(series), "") or ""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    cfg = SeriesConfig(prefix=DEFAULT_PREFIX.get(series, ""))
    for field_name in ("mode", "prefix", "suffix"):
        if isinstance(data.get(field_name), str):
            setattr(cfg, field_name, data[field_name])
    for field_name in ("next_number", "digits"):
        try:
            setattr(cfg, field_name, max(1 if field_name == "digits" else 0,
                                         int(data.get(field_name, getattr(cfg, field_name)))))
        except (TypeError, ValueError):
            pass
    if cfg.mode not in MODES:
        cfg.mode = "tally"
    return cfg


def save_config(session: Session, company_id: int, series: str, cfg: SeriesConfig) -> None:
    if series not in SERIES:
        raise ValueError(f"Unknown voucher series '{series}'.")
    if cfg.mode not in MODES:
        raise ValueError(f"Unknown numbering mode '{cfg.mode}'.")
    if int(cfg.next_number) < 0:
        raise ValueError("Start number cannot be negative.")
    cfg.prefix = (cfg.prefix or "")[:20]
    cfg.suffix = (cfg.suffix or "")[:20]
    cfg.digits = min(max(int(cfg.digits or 1), 1), 10)
    set_setting(session, company_id, _key(series), json.dumps(asdict(cfg)))


def all_configs(session: Session, company_id: int) -> Dict[str, SeriesConfig]:
    return {s: get_config(session, company_id, s) for s in SERIES}


def series_for_payload(payload) -> str:
    """Which series a Tally payload belongs to."""
    from services.tally_connector import AccountingVoucherPayload, LedgerVoucherPayload
    if isinstance(payload, AccountingVoucherPayload):
        return payload.transaction_kind if payload.transaction_kind in SERIES else "journal"
    if isinstance(payload, LedgerVoucherPayload):
        if payload.voucher_type.lower() == "contra":
            return "contra"
        return "payment" if payload.direction == "payment" else "receipt"
    return "sales" if payload.transaction_kind == "sales" else "purchase"


def assign(session: Session, company_id: int, payload, invoice_number: str = "") -> str:
    """Put the voucher number on the payload (without using it up yet).
    Returns the number, or "" when Tally should number the voucher."""
    series = series_for_payload(payload)
    cfg = get_config(session, company_id, series)
    if cfg.mode == "custom":
        number = cfg.format()
    elif cfg.mode == "invoice":
        number = (invoice_number or "").strip()
    else:
        number = ""
    payload.vch_number = number[:60]
    return payload.vch_number


def consume(session: Session, company_id: int, payload, result) -> None:
    """After a successful post/export, move the custom series forward."""
    if not getattr(result, "success", False) or getattr(result, "mode", "") == "TEST_MODE":
        return
    series = series_for_payload(payload)
    cfg = get_config(session, company_id, series)
    if cfg.mode != "custom" or not getattr(payload, "vch_number", ""):
        return
    if payload.vch_number != cfg.format():
        return  # someone changed the series meanwhile — do not skip numbers
    cfg.next_number += 1
    save_config(session, company_id, series, cfg)
