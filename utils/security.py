"""
Security helpers.

- Passwords: PBKDF2-HMAC-SHA256 with a per-user random salt. Never plaintext.
- API keys at rest: Fernet (AES-128-CBC + HMAC) derived from SECRET_KEY.
- Upload validation: extension, size and magic-byte checks.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from typing import Optional

from utils.config import settings

PBKDF2_ROUNDS = 240_000


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Return 'pbkdf2_sha256$rounds$salt$hash'."""
    if not password:
        raise ValueError("Password must not be empty")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not password or not stored:
        return False
    try:
        algo, rounds, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds)
        )
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, TypeError):
        return False


def password_problems(password: str) -> list[str]:
    """Return a list of human-readable policy failures (empty == acceptable)."""
    issues = []
    if len(password or "") < 8:
        issues.append("at least 8 characters")
    if not re.search(r"[A-Za-z]", password or ""):
        issues.append("at least one letter")
    if not re.search(r"\d", password or ""):
        issues.append("at least one number")
    return issues


def generate_reset_token() -> str:
    return secrets.token_urlsafe(32)


# --------------------------------------------------------------------------
# Secret storage (API keys, Tally credentials)
# --------------------------------------------------------------------------
def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover - optional dependency
        return None
    key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(value: str) -> str:
    if value is None:
        return ""
    f = _fernet()
    if f is None:
        # Reversible obfuscation only; the README flags this as unsupported
        # for production. Install `cryptography` to get real encryption.
        return "plain:" + base64.urlsafe_b64encode(value.encode("utf-8")).decode()
    return "enc:" + f.encrypt(value.encode("utf-8")).decode()


def decrypt_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    if value.startswith("plain:"):
        return base64.urlsafe_b64decode(value[6:].encode()).decode("utf-8")
    if value.startswith("enc:"):
        f = _fernet()
        if f is None:
            return ""
        try:
            return f.decrypt(value[4:].encode()).decode("utf-8")
        except Exception:
            return ""
    return value


def mask_secret(value: Optional[str], keep: int = 4) -> str:
    if not value:
        return "—"
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


# --------------------------------------------------------------------------
# Upload validation
# --------------------------------------------------------------------------
MAGIC_SIGNATURES = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"%PDF": "pdf",
    b"RIFF": "webp",  # RIFF....WEBP
}


def detect_file_type(head: bytes) -> Optional[str]:
    for magic, kind in MAGIC_SIGNATURES.items():
        if head.startswith(magic):
            if kind == "webp" and b"WEBP" not in head[:16]:
                continue
            return kind
    return None


def validate_upload(filename: str, data: bytes) -> tuple[bool, str]:
    """Return (ok, message). Checks extension, size and magic bytes."""
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext not in settings.ALLOWED_EXTENSIONS:
        return False, f"File type '.{ext}' is not allowed."
    size_mb = len(data) / (1024 * 1024)
    if size_mb > settings.MAX_UPLOAD_MB:
        return False, f"File is {size_mb:.1f} MB; limit is {settings.MAX_UPLOAD_MB} MB."
    if not data:
        return False, "File is empty."
    detected = detect_file_type(data[:16])
    if detected is None:
        return False, "File content does not look like a valid image or PDF."
    if detected == "jpg" and ext not in ("jpg", "jpeg"):
        return False, "File content is a JPEG but the extension says otherwise."
    if detected in ("png", "pdf", "webp") and ext != detected:
        return False, f"File content is {detected.upper()} but the extension says .{ext}."
    return True, "ok"


def safe_filename(name: str) -> str:
    name = os.path.basename(name or "upload")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:120] or "upload"


def sanitize_text(value: Optional[str], max_len: int = 500) -> str:
    if value is None:
        return ""
    value = str(value).replace("\x00", "").strip()
    return value[:max_len]
