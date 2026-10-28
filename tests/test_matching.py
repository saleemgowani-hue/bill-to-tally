"""Tests for the product matching engine."""
import pytest

from database.models import MatchStatus, Product
from services.product_matcher import (
    ProductMatcher,
    extract_brand,
    extract_pack_size,
    normalize_name,
    string_similarity,
)
from tests.conftest import make_product


class TestHelpers:
    def test_normalize_name(self):
        assert normalize_name("Parle-G  Gold, 800 gm!") == "parle g gold 800 gm"

    def test_pack_size_units_are_converted(self):
        assert extract_pack_size("Tata Salt 1 KG") == (1000.0, "g")
        assert extract_pack_size("Parle G 800G") == (800.0, "g")
        assert extract_pack_size("Coke 2 ltr") == (2000.0, "ml")

    def test_pack_size_absent(self):
        assert extract_pack_size("Assorted Sweets") is None

    def test_brand_detection(self):
        assert extract_brand("Parle G Gold 800G") == "parle"
        assert extract_brand("Amul Butter 500 gm") == "amul"

    def test_similarity_ordering(self):
        near = string_similarity("Parle G Gold 800G", "Parle-G Gold Biscuit 800 gm")
        far = string_similarity("Parle G Gold 800G", "Colgate Toothpaste 100g")
        assert near > far
        assert near > 0.6


class TestMatching:
    def test_exact_match(self, session, company):
        make_product(session, company, "Tata Salt 1 kg", unit="PCS")
        matcher = ProductMatcher(session, company.id)
        result = matcher.match({"raw_name": "Tata Salt 1 kg", "unit": "PCS"})
        assert result.status == MatchStatus.MATCHED.value

    def test_fuzzy_real_world_case(self, session, company):
        product = make_product(session, company, "Parle-G Gold Biscuit 800 gm", hsn="19053100")
        matcher = ProductMatcher(session, company.id)
        result = matcher.match(
            {"raw_name": "Parle G Gold 800G", "hsn": "19053100", "unit": "PCS"}
        )
        assert result.status in (MatchStatus.MATCHED.value, MatchStatus.POSSIBLE.value)
        assert result.candidates[0].product_id == product.id

    def test_unknown_product_is_new(self, session, company):
        make_product(session, company, "Tata Salt 1 kg")
        matcher = ProductMatcher(session, company.id)
        result = matcher.match({"raw_name": "Havells Ceiling Fan 1200mm"})
        assert result.status == MatchStatus.NEW.value
        assert result.product_id is None

    def test_barcode_wins(self, session, company):
        product = make_product(session, company, "Something Completely Different",
                               barcode="8901719101234")
        matcher = ProductMatcher(session, company.id)
        result = matcher.match({"raw_name": "PARLE BISCUIT", "barcode": "8901719101234"})
        assert result.product_id == product.id
        assert "barcode" in result.reason

    def test_new_barcode_product_status(self, session, company):
        matcher = ProductMatcher(session, company.id)
        result = matcher.match({"raw_name": "Mystery Item", "barcode": "9999999999999"})
        assert result.status == MatchStatus.NEW_BARCODE.value

    def test_different_pack_size_is_penalised(self, session, company):
        make_product(session, company, "Tata Salt 1 kg")
        matcher = ProductMatcher(session, company.id)
        same = matcher.match({"raw_name": "Tata Salt 1 kg"})
        different = matcher.match({"raw_name": "Tata Salt 100 g"})
        assert same.score > different.score

    def test_low_confidence_is_never_auto_applied(self, session, company):
        make_product(session, company, "Britannia Marie Gold 250 gm")
        matcher = ProductMatcher(session, company.id)
        result = matcher.match({"raw_name": "Marie Biscuit"})
        if result.status == MatchStatus.POSSIBLE.value:
            assert result.product_id is None, "A possible match must not be auto-linked"


class TestAliasLearning:
    def test_alias_makes_next_match_instant(self, session, company, user):
        product = make_product(session, company, "Parle-G Gold Biscuit 800 gm")
        matcher = ProductMatcher(session, company.id)
        matcher.remember_mapping("PRLE G GLD 800", product.id, user.id)

        result = matcher.match({"raw_name": "PRLE G GLD 800"})
        assert result.status == MatchStatus.MATCHED.value
        assert result.product_id == product.id
        assert "previously confirmed" in result.reason

    def test_alias_use_count_increments(self, session, company, user):
        product = make_product(session, company, "Amul Butter 500 gm")
        matcher = ProductMatcher(session, company.id)
        alias = matcher.remember_mapping("AMUL BTR 500", product.id, user.id)
        assert alias.times_used == 1
        alias = matcher.remember_mapping("AMUL BTR 500", product.id, user.id)
        assert alias.times_used == 2


class TestProductCreation:
    def test_creates_with_zero_stock(self, session, company, user):
        matcher = ProductMatcher(session, company.id)
        product = matcher.create_product_from_item(
            {
                "normalized_name": "New Widget 500 gm", "hsn": "12345678",
                "unit": "PCS", "gst_rate": 18, "rate": 45.0, "barcode": "",
            },
            user.id,
        )
        assert product.current_stock == 0.0
        assert product.pack_size == "500 g"

    def test_requires_a_name(self, session, company):
        matcher = ProductMatcher(session, company.id)
        with pytest.raises(ValueError):
            matcher.create_product_from_item({"normalized_name": "", "raw_name": ""})


class TestTenantIsolation:
    def test_products_from_another_company_are_invisible(self, session, company, other_company):
        make_product(session, other_company, "Tata Salt 1 kg")
        matcher = ProductMatcher(session, company.id)
        result = matcher.match({"raw_name": "Tata Salt 1 kg"})
        assert result.status == MatchStatus.NEW.value
        assert result.product_id is None
