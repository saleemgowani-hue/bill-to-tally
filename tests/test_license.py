"""Tests for licence key generation, validation and activation."""
import datetime as dt

import pytest

from database.models import Company, License
from services import license_service as ls
from services.license_service import LicenseError


class TestKeyFormat:
    def test_generated_key_is_valid(self):
        for plan in ("YEARLY", "MONTHLY", "TRIAL"):
            info = ls.inspect_key(ls.generate_key(plan))
            assert info.plan == plan

    def test_durations(self):
        assert ls.inspect_key(ls.generate_key("YEARLY")).duration_days == 365
        assert ls.inspect_key(ls.generate_key("MONTHLY")).duration_days == 30
        assert ls.inspect_key(ls.generate_key("TRIAL")).duration_days == 14

    def test_display_shape(self):
        key = ls.generate_key("YEARLY")
        assert key.startswith("SNST-")
        assert len(key.split("-")) == 5
        assert all(len(part) == 4 for part in key.split("-")[1:])

    def test_batch_is_unique(self):
        keys = ls.generate_batch("MONTHLY", 300)
        assert len(set(keys)) == 300

    def test_unknown_plan_rejected(self):
        with pytest.raises(LicenseError):
            ls.generate_key("LIFETIME")


class TestTypedInput:
    """Whatever the customer types over the phone should work."""

    def test_lowercase_and_no_dashes(self):
        key = ls.generate_key("YEARLY")
        assert ls.inspect_key(key.replace("-", "").lower()).plan == "YEARLY"

    def test_spaces_and_missing_prefix(self):
        key = ls.generate_key("MONTHLY")
        body = key[5:]
        assert ls.inspect_key(f"  {body}  ").plan == "MONTHLY"

    def test_confusable_characters_are_forgiven(self):
        # O/0 and I/1 look identical in most fonts.
        key = ls.generate_key("YEARLY")
        typed = ls.normalize(key).replace("0", "O").replace("1", "I")
        assert ls.inspect_key(typed).plan == "YEARLY"

    def test_empty_key(self):
        with pytest.raises(LicenseError, match="Enter your licence key"):
            ls.inspect_key("")


class TestForgery:
    def test_random_string_rejected(self):
        with pytest.raises(LicenseError):
            ls.inspect_key("SNST-ABCD-EFGH-JKMN-PQRS")

    def test_single_character_change_rejected(self):
        raw = list(ls.normalize(ls.generate_key("YEARLY")))
        raw[5] = "Z" if raw[5] != "Z" else "Y"
        with pytest.raises(LicenseError):
            ls.inspect_key("".join(raw))

    def test_plan_cannot_be_upgraded(self):
        """Changing M to Y must not turn a monthly key into a yearly one."""
        raw = ls.normalize(ls.generate_key("MONTHLY"))
        with pytest.raises(LicenseError):
            ls.inspect_key("Y" + raw[1:])

    def test_wrong_length_rejected(self):
        with pytest.raises(LicenseError, match="characters"):
            ls.inspect_key("SNST-ABCD-EFGH")

    def test_invalid_character_rejected(self):
        with pytest.raises(LicenseError):
            ls.inspect_key("SNST-Y!!!-EFGH-JKMN-PQRS")


class TestActivation:
    def test_activation_sets_expiry(self, session, company, user):
        key = ls.generate_key("MONTHLY")
        record = ls.activate(session, company, key, activated_by=user.id)
        assert company.license_plan == "MONTHLY"
        assert record.expires_at > dt.datetime.now(dt.timezone.utc)
        assert 28 <= (record.expires_at - record.activated_at).days <= 30

    def test_raw_key_is_never_stored(self, session, company, user):
        key = ls.generate_key("YEARLY")
        ls.activate(session, company, key, activated_by=user.id)
        stored = session.query(License).filter(License.company_id == company.id).all()
        raw = ls.normalize(key)
        for record in stored:
            assert raw not in (record.key_hash or "")
            assert raw not in (record.key_display or "")
        assert "•" in company.license_key_display

    def test_key_cannot_be_reused_by_another_company(
        self, session, company, other_company, user
    ):
        key = ls.generate_key("YEARLY")
        ls.activate(session, company, key, activated_by=user.id)
        with pytest.raises(LicenseError, match="already been used"):
            ls.activate(session, other_company, key)

    def test_renewal_extends_from_current_expiry(self, session, company, user):
        first = ls.activate(session, company, ls.generate_key("MONTHLY"), activated_by=user.id)
        original = first.expires_at
        second = ls.activate(session, company, ls.generate_key("MONTHLY"), activated_by=user.id)
        # Renewing early must not throw away the days already paid for.
        assert (second.expires_at - original).days >= 28

    def test_renewal_after_expiry_starts_from_today(self, session, company, user):
        company.license_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)
        session.commit()
        record = ls.activate(session, company, ls.generate_key("MONTHLY"), activated_by=user.id)
        assert (record.expires_at - dt.datetime.now(dt.timezone.utc)).days >= 28


class TestStatus:
    def test_no_licence_is_invalid(self, session, company):
        company.license_expires_at = None
        company.license_plan = None
        session.commit()
        assert not ls.status(company).is_valid

    def test_active_licence(self, session, company, user):
        ls.activate(session, company, ls.generate_key("YEARLY"), activated_by=user.id)
        state = ls.status(company)
        assert state.is_valid
        assert state.days_left > 300
        assert not state.is_expiring_soon

    def test_expiring_soon_flag(self, session, company, user):
        ls.activate(session, company, ls.generate_key("YEARLY"), activated_by=user.id)
        company.license_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)
        session.commit()
        state = ls.status(company)
        assert state.is_valid and state.is_expiring_soon

    def test_expired_licence(self, session, company, user):
        ls.activate(session, company, ls.generate_key("MONTHLY"), activated_by=user.id)
        company.license_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        session.commit()
        state = ls.status(company)
        assert not state.is_valid
        assert "expired" in state.message.lower()

    def test_message_names_the_support_number(self, session, company, user):
        ls.activate(session, company, ls.generate_key("MONTHLY"), activated_by=user.id)
        company.license_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        session.commit()
        assert "9993199719" in ls.status(company).message


class TestSigningSecret:
    """A secret baked into the source is no secret. The app must refuse to
    sign or verify anything until one is configured."""

    def test_no_default_secret_in_the_source(self):
        from pathlib import Path

        config = (Path(__file__).resolve().parent.parent / "utils" / "config.py").read_text()
        assert 'os.getenv("LICENSE_SIGNING_SECRET", "")' in config

    def test_env_example_ships_blank(self):
        from pathlib import Path

        example = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
        for line in example.splitlines():
            if line.startswith("LICENSE_SIGNING_SECRET="):
                assert line.strip() == "LICENSE_SIGNING_SECRET=", (
                    "a real secret must never ship in .env.example"
                )
                break
        else:
            pytest.fail("LICENSE_SIGNING_SECRET missing from .env.example")

    def test_blank_secret_is_reported(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "LICENSE_SIGNING_SECRET", "")
        assert ls.secret_problem() is not None

    def test_short_secret_is_rejected(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "LICENSE_SIGNING_SECRET", "short")
        assert "too short" in ls.secret_problem()

    def test_cannot_mint_keys_without_a_secret(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "LICENSE_SIGNING_SECRET", "")
        with pytest.raises(LicenseError):
            ls.generate_key("YEARLY")

    def test_cannot_validate_keys_without_a_secret(self, monkeypatch):
        from utils.config import settings

        key = ls.generate_key("YEARLY")
        monkeypatch.setattr(settings, "LICENSE_SIGNING_SECRET", "")
        with pytest.raises(LicenseError):
            ls.inspect_key(key)

    def test_a_configured_secret_works(self):
        assert ls.secret_problem() is None
        assert ls.inspect_key(ls.generate_key("YEARLY")).plan == "YEARLY"

    def test_keys_do_not_validate_under_a_different_secret(self, monkeypatch):
        """The whole point: a customer build with its own secret cannot use
        keys minted elsewhere."""
        from utils.config import settings

        key = ls.generate_key("YEARLY")
        monkeypatch.setattr(settings, "LICENSE_SIGNING_SECRET", "a-completely-different-secret")
        with pytest.raises(LicenseError):
            ls.inspect_key(key)
