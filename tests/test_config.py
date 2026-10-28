"""Tests for the .env loader.

An inline comment once turned LICENSE_REQUIRED=true into a string that was not
"true", which silently switched licence enforcement off. Nothing warned anyone.
"""
import re
from pathlib import Path

import pytest

from utils.config import _clean_value

ROOT = Path(__file__).resolve().parent.parent


class TestValueCleaning:
    @pytest.mark.parametrize("raw,expected", [
        ("true         # false disables the key check", "true"),
        ("10          # start warning this many days before", "10"),
        ("0.90        # green", "0.90"),
        ("15", "15"),
        ("  spaced  # comment ", "spaced"),
        ("", ""),
    ])
    def test_inline_comments_are_stripped(self, raw, expected):
        assert _clean_value(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ('"pa#ssword"', "pa#ssword"),
        ("'quoted value'", "quoted value"),
        ("sk-abc#123", "sk-abc#123"),          # no space before #, so it is data
        ("#leading", "#leading"),
    ])
    def test_hashes_that_are_part_of_the_value_survive(self, raw, expected):
        assert _clean_value(raw) == expected


class TestSettingsParseCorrectly:
    """Every commented setting in .env.example must still parse."""

    def test_every_commented_line_cleans_to_something_usable(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for line in example.splitlines():
            if not re.match(r"^[A-Z_]+=", line):
                continue
            key, _, raw = line.partition("=")
            value = _clean_value(raw)
            assert "#" not in value, f"{key} keeps a comment: {value!r}"
            assert value == value.strip()

    def test_boolean_settings_are_booleans(self):
        from utils.config import settings

        for name in ("LICENSE_REQUIRED", "TALLY_TEST_MODE"):
            assert isinstance(getattr(settings, name), bool)

    def test_numeric_settings_are_numbers(self):
        from utils.config import settings

        assert isinstance(settings.LICENSE_WARN_DAYS, int)
        assert isinstance(settings.CONFIDENCE_HIGH, float)
        assert 0 < settings.CONFIDENCE_HIGH <= 1

    def test_licence_enforcement_defaults_to_on(self):
        """If this ever reads False by accident, the product ships unlocked."""
        import os

        from utils.config import _b

        os.environ.pop("LICENSE_REQUIRED", None)
        assert _b("LICENSE_REQUIRED", True) is True
