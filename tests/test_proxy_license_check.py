"""ocr_proxy/license_check.py is a dependency-free copy of
services/license_service.py's signature check (see its own docstring for
why it has to be a copy, not an import). This test is the guard against the
two drifting apart: if someone changes the signing algorithm in one place
without the other, a real customer licence key would be accepted by the app
but rejected by the proxy (or vice versa) - exactly the kind of bug that
would not show up until a real customer hit it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ocr_proxy"))
import license_check  # noqa: E402  (path must be set up first)

from services.license_service import generate_key, inspect_key, LicenseError
from utils.config import settings

SECRET = "test-secret-for-cross-check-1234567890"


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "LICENSE_SIGNING_SECRET", SECRET)


@pytest.mark.parametrize("plan", ["YEARLY", "MONTHLY", "TRIAL"])
def test_every_real_key_the_app_accepts_the_proxy_accepts_too(plan):
    for _ in range(10):
        key = generate_key(plan)
        inspect_key(key)  # raises if the app itself would reject it
        assert license_check.is_valid(key, SECRET), (
            f"app accepted {key} but the proxy's copy of the algorithm rejected it"
        )


def test_a_tampered_key_is_rejected_by_both():
    key = generate_key("MONTHLY")
    tampered = key[:-1] + ("A" if key[-1] != "A" else "B")
    with pytest.raises(LicenseError):
        inspect_key(tampered)
    assert not license_check.is_valid(tampered, SECRET)


def test_wrong_secret_is_rejected():
    key = generate_key("MONTHLY")
    assert not license_check.is_valid(key, "a-completely-different-secret")


def test_garbage_input_is_rejected_not_raised():
    assert license_check.is_valid("not a licence key at all", SECRET) is False
    assert license_check.is_valid("", SECRET) is False
    assert license_check.is_valid(None, SECRET) is False  # type: ignore[arg-type]


def test_blank_secret_never_validates_anything():
    key = generate_key("MONTHLY")
    assert not license_check.is_valid(key, "")
