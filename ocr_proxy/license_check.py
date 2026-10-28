"""
Licence-key signature check for the proxy - a trimmed-down, dependency-free
copy of services/license_service.py's inspect_key().

Why a copy instead of importing the real thing: license_service.py imports
database.models (SQLAlchemy models) at module level, which would drag the
whole app's database stack into this small standalone proxy for no reason -
this proxy never touches any customer's database, it only needs to confirm a
key's signature was minted with SN Softech's LICENSE_SIGNING_SECRET.

MUST stay byte-for-byte identical to license_service.py's signing algorithm
(ALPHABET, PREFIX, PAYLOAD_LEN, SIGNATURE_LEN, the HMAC construction), or a
real customer's licence key will fail here even though it works fine in the
app itself. tests/test_proxy_license_check.py in the main app's test suite
cross-checks this module against the real one on a batch of generated keys -
if you ever change the algorithm in one place, change it in both and run
that test before shipping either.

Deliberately does NOT check activation/expiry/one-key-one-company (this
proxy has no database to check that against) - it only confirms the key is
genuinely SN-Softech-signed, which is enough to keep the proxy, and the
vendor's Gemini bill, restricted to actual licence holders.
"""
from __future__ import annotations

import hashlib
import hmac

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PREFIX = "SNST"
PAYLOAD_LEN = 10
SIGNATURE_LEN = 6
RAW_LEN = PAYLOAD_LEN + SIGNATURE_LEN
CONFUSABLES = {"I": "1", "L": "1", "O": "0", "U": "V"}


def normalize(key: str) -> str:
    cleaned = "".join(ch for ch in (key or "").upper() if ch.isalnum())
    if cleaned.startswith(PREFIX):
        cleaned = cleaned[len(PREFIX):]
    return "".join(CONFUSABLES.get(ch, ch) for ch in cleaned)


def _signature(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return "".join(ALPHABET[b % len(ALPHABET)] for b in digest[:SIGNATURE_LEN])


def is_valid(key: str, secret: str) -> bool:
    """True only if `key` carries a genuine SN Softech signature."""
    if not secret:
        return False
    raw = normalize(key)
    if len(raw) != RAW_LEN or any(ch not in ALPHABET for ch in raw):
        return False
    payload, signature = raw[:PAYLOAD_LEN], raw[PAYLOAD_LEN:]
    return hmac.compare_digest(signature, _signature(payload, secret))
