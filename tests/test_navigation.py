"""Guards for the sidebar routing key.

The router reads st.session_state["nav"], a page name. Any page that jumps the
user somewhere else must set that exact key — an older build set "nav_index",
which silently did nothing and left people staring at the wrong bill.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "app.py").read_text(encoding="utf-8")
PAGE_FILES = sorted((ROOT / "app_pages").glob("*.py"))


def page_names():
    return set(re.findall(r'^\s*"([^"]+)":\s*\("', APP, re.M))


class TestRouterKey:
    def test_router_reads_nav(self):
        assert 'st.session_state.nav' in APP

    def test_no_page_sets_a_stale_routing_key(self):
        """nav_index, page_index and friends are not read by anything."""
        for path in PAGE_FILES:
            text = path.read_text(encoding="utf-8")
            for stale in ("nav_index", "page_index", "current_page"):
                assert stale not in text, f"{path.name} sets {stale}, which nothing reads"

    def test_every_jump_targets_a_real_page(self):
        names = page_names()
        assert names, "could not parse PAGES from app.py"
        for path in PAGE_FILES:
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r'session_state\.nav\s*=\s*"([^"]+)"', text):
                assert target in names, f"{path.name} jumps to unknown page {target!r}"

    def test_pages_dict_shape(self):
        """Each entry is (icon, module, colour) — the sidebar depends on it."""
        entries = re.findall(r'^\s*"[^"]+":\s*\("([^"]*)",\s*"([^"]+)",\s*"(#[0-9a-fA-F]{6})"\)',
                             APP, re.M)
        assert len(entries) == len(page_names())
        for icon, module, colour in entries:
            assert (ROOT / "app_pages" / f"{module}.py").exists()

    def test_colours_are_distinct(self):
        colours = re.findall(r'\("[^"]*",\s*"[^"]+",\s*"(#[0-9a-fA-F]{6})"\)', APP)
        assert len(set(colours)) == len(colours), "two menu buttons share a colour"


class TestReviewSelection:
    def test_upload_points_the_review_screen_at_the_new_bill(self):
        text = (ROOT / "app_pages" / "upload_bill.py").read_text(encoding="utf-8")
        assert "st.session_state.review_invoice_id = invoice.id" in text

    def test_review_falls_back_to_the_newest_bill(self):
        """With no stored selection the list is newest-first at index 0."""
        text = (ROOT / "app_pages" / "review_bills.py").read_text(encoding="utf-8")
        assert "Invoice.created_at.desc()" in text
        assert "ids.index(preselect) if preselect in ids else 0" in text


class TestDataFrameColumnTypes:
    """A column mixing an int with "" becomes an `object` dtype that Arrow
    refuses to convert, and Streamlit then renders nothing — silently. Two
    tables shipped like that before this test existed."""

    MIXED = re.compile(
        r'"([^"]+)":\s*[\w.]+\.(?:id|invoice_id|company_id|user_id|product_id)\s+or\s+""'
    )

    def test_no_id_column_falls_back_to_an_empty_string(self):
        offenders = []
        for path in sorted((ROOT / "app_pages").glob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if "str(" in line:
                    continue
                match = self.MIXED.search(line)
                if match:
                    offenders.append(f"{path.name}:{number} column {match.group(1)!r}")
        assert not offenders, (
            "wrap these in str() — an int mixed with '' breaks the table: "
            + "; ".join(offenders)
        )
