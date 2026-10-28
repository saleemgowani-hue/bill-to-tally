"""Per-licence-key daily request counter, so one leaked key can only ever cost
the vendor a bounded amount per day instead of an unlimited bill. SQLite file
next to this module - fine for the request volume a small vendor's customer
base generates; swap for a real database if that ever stops being true.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from pathlib import Path
from typing import Tuple

DB_PATH = Path(__file__).parent / "usage.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS usage ("
        "key_hash TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL, "
        "PRIMARY KEY (key_hash, day))"
    )
    return conn


def _hash(key: str) -> str:
    return hashlib.sha256(key.strip().upper().encode()).hexdigest()


def check_and_increment(key: str, daily_limit: int) -> Tuple[bool, int]:
    """Returns (allowed, requests_used_today_after_this_one).

    Counts this call even when it is the one that goes over the limit, so the
    caller can report "61/60" rather than silently stopping at the round
    number - clearer when a customer asks why they were blocked.
    """
    today = dt.date.today().isoformat()
    khash = _hash(key)
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT count FROM usage WHERE key_hash = ? AND day = ?", (khash, today)
        ).fetchone()
        count = (row[0] if row else 0) + 1
        if count > daily_limit:
            return False, count
        conn.execute(
            "INSERT INTO usage (key_hash, day, count) VALUES (?, ?, ?) "
            "ON CONFLICT(key_hash, day) DO UPDATE SET count = excluded.count",
            (khash, today, count),
        )
        conn.commit()
        return True, count
    finally:
        conn.close()
