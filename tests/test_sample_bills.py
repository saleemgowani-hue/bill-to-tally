"""The shipped demo bills must extract as four different suppliers.

Without this, a demo of four bills looks like one supplier billed four times
and three of them get flagged as duplicates.
"""
import json
from pathlib import Path

import pytest
from PIL import Image

from services.invoice_validator import InvoiceValidator, detect_tax_scope
from services.ocr_service import MockOCRProvider
from utils.image_tools import fingerprint_distance, perceptual_fingerprint

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
CATALOGUE = json.loads((SAMPLES / "sample_bills.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def provider():
    MockOCRProvider._catalogue = None      # force a reload
    return MockOCRProvider()


def extracted(provider, entry, image=None):
    image = image or Image.open(SAMPLES / entry["file"])
    return provider.extract_structured([image], "", "").structured


class TestCatalogue:
    def test_four_bills_ship(self):
        assert len(CATALOGUE) == 4

    def test_every_image_exists(self):
        for entry in CATALOGUE:
            assert (SAMPLES / entry["file"]).exists()

    def test_suppliers_are_all_different(self):
        names = [e["data"]["supplier"]["name"] for e in CATALOGUE]
        assert len(set(names)) == 4

    def test_invoice_numbers_are_all_different(self):
        numbers = [e["data"]["invoice"]["invoice_number"] for e in CATALOGUE]
        assert len(set(numbers)) == 4

    def test_all_dated_first_of_september(self):
        """Tally's educational mode only accepts the 1st, 2nd or 31st."""
        for entry in CATALOGUE:
            assert entry["data"]["invoice"]["invoice_date"] == "2026-09-01"


class TestArithmetic:
    """Every shipped bill must reconcile, or the demo opens on a red error."""

    @pytest.mark.parametrize("entry", CATALOGUE, ids=[e["file"] for e in CATALOGUE])
    def test_bill_reconciles(self, entry):
        report = InvoiceValidator().validate(entry["data"])
        blocking = [i.message for i in report.errors if "confidence" not in i.message]
        assert not blocking, blocking

    @pytest.mark.parametrize("entry", CATALOGUE, ids=[e["file"] for e in CATALOGUE])
    def test_lines_add_up_to_the_grand_total(self, entry):
        data = entry["data"]
        total = sum(line["total"] for line in data["items"]) + data["round_off"]
        assert abs(total - data["grand_total"]) < 0.01

    def test_one_bill_is_inter_state(self):
        scopes = [
            detect_tax_scope(
                e["data"]["supplier"]["gstin"], "22ZZZZZ9999Z1Z9", e["data"]["items"]
            )
            for e in CATALOGUE
        ]
        assert "INTER-STATE" in scopes
        assert "INTRA-STATE" in scopes

    def test_one_bill_has_a_low_confidence_line(self):
        assert any(
            any(line["confidence"] < 0.7 for line in e["data"]["items"])
            for e in CATALOGUE
        )


class TestRecognition:
    @pytest.mark.parametrize("entry", CATALOGUE, ids=[e["file"] for e in CATALOGUE])
    def test_each_image_returns_its_own_data(self, provider, entry):
        data = extracted(provider, entry)
        assert data["supplier"]["name"] == entry["data"]["supplier"]["name"]
        assert data["invoice"]["invoice_number"] == entry["data"]["invoice"]["invoice_number"]

    @pytest.mark.parametrize("entry", CATALOGUE, ids=[e["file"] for e in CATALOGUE])
    def test_survives_resizing(self, provider, entry):
        image = Image.open(SAMPLES / entry["file"])
        width, height = image.size
        for variant in (
            image.resize((width // 2, height // 2)),
            image.resize((800, int(800 * height / width))),
        ):
            data = extracted(provider, entry, variant)
            assert data["invoice"]["invoice_number"] == \
                entry["data"]["invoice"]["invoice_number"]

    @pytest.mark.parametrize("entry", CATALOGUE, ids=[e["file"] for e in CATALOGUE])
    def test_survives_a_small_skew(self, provider, entry):
        image = Image.open(SAMPLES / entry["file"])
        skewed = image.rotate(1.5, expand=True, fillcolor="white")
        data = extracted(provider, entry, skewed)
        assert data["invoice"]["invoice_number"] == entry["data"]["invoice"]["invoice_number"]

    def test_unknown_image_falls_back_rather_than_guessing(self, provider):
        blank = Image.new("RGB", (600, 900), "white")
        data = provider.extract_structured([blank], "", "").structured
        shipped = {e["data"]["invoice"]["invoice_number"] for e in CATALOGUE}
        assert data["invoice"]["invoice_number"] not in shipped

    def test_response_says_it_is_sample_data(self, provider):
        image = Image.open(SAMPLES / CATALOGUE[0]["file"])
        response = provider.extract_structured([image], "", "")
        assert any("sample data" in w.lower() for w in response.warnings)


class TestFingerprints:
    def test_stored_fingerprints_match_the_images(self):
        """Catches an image edited without re-running make_sample_bills.py."""
        for entry in CATALOGUE:
            image = Image.open(SAMPLES / entry["file"])
            assert perceptual_fingerprint(image) == entry["fingerprint"], entry["file"]

    def test_bills_are_far_apart_from_each_other(self):
        """Guards against a new demo bill being laid out like an existing one.

        The closest pair currently sits at 35 of 240 bits. That is comfortable
        for an uploaded file (distance 0) or a resized copy, but it is not a
        huge margin — matching is nearest-neighbour, and a heavily skewed
        photograph could still land on the wrong bill. Recognition of realistic
        variants is covered by TestRecognition; this only stops the layouts
        drifting together.
        """
        for i, first in enumerate(CATALOGUE):
            for second in CATALOGUE[i + 1:]:
                distance = fingerprint_distance(
                    first["fingerprint"], second["fingerprint"]
                )
                assert distance > 30, f"{first['file']} vs {second['file']}: {distance}"
