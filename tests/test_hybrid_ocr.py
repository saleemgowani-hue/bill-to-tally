"""Tests for HybridOCRProvider ("Auto" engine): PP-StructureV3 first, an AI
vision engine only when the offline pass looks unreliable. Uses fake
providers so no real PaddleOCR/Gemini call happens."""
import pytest

from services.ocr_service import (
    HybridOCRProvider,
    OCRError,
    OCRProvider,
    OCRResponse,
    PROVIDER_REGISTRY,
)


def _good_data():
    return {
        "supplier": {"name": "ABC Traders", "gstin": "", "address": ""},
        "invoice": {"invoice_number": "INV-1", "invoice_date": "2026-09-01", "due_date": ""},
        "items": [{"raw_name": "Widget", "total": 118.0}],
        "grand_total": 118.0,
        "overall_confidence": 0.8,
    }


class FakePrimary(OCRProvider):
    name = "ppstructure"
    supports_structured = True

    def __init__(self, config=None, response=None, error=None):
        super().__init__(config)
        self._response = response
        self._error = error

    def extract_structured(self, images, prompt, schema_hint):
        if self._error:
            raise self._error
        return self._response

    def extract_text(self, images):
        raise NotImplementedError


class FakeFallback(OCRProvider):
    name = "gemini"
    supports_structured = True
    called = False

    def extract_structured(self, images, prompt, schema_hint):
        FakeFallback.called = True
        return OCRResponse(provider="gemini", model="fake", structured=_good_data(), duration_ms=1)

    def extract_text(self, images):
        raise NotImplementedError


@pytest.fixture
def hybrid(monkeypatch):
    FakeFallback.called = False
    monkeypatch.setitem(PROVIDER_REGISTRY, "gemini", FakeFallback)
    return HybridOCRProvider


def _patch_primary(monkeypatch, **kwargs):
    import services.ocr_service as svc
    monkeypatch.setattr(svc, "PPStructureProvider", lambda config: FakePrimary(config, **kwargs))


class TestLooksReliable:
    def test_good_result_is_reliable(self, hybrid):
        assert hybrid({})._looks_reliable(_good_data()) is True

    def test_none_is_not_reliable(self, hybrid):
        assert hybrid({})._looks_reliable(None) is False

    def test_low_confidence_is_not_reliable(self, hybrid):
        data = _good_data()
        data["overall_confidence"] = 0.2
        assert hybrid({})._looks_reliable(data) is False

    def test_missing_supplier_is_not_reliable(self, hybrid):
        data = _good_data()
        data["supplier"]["name"] = ""
        assert hybrid({})._looks_reliable(data) is False

    def test_no_items_is_not_reliable(self, hybrid):
        data = _good_data()
        data["items"] = []
        assert hybrid({})._looks_reliable(data) is False

    def test_totals_far_apart_is_not_reliable(self, hybrid):
        data = _good_data()
        data["grand_total"] = 5000.0  # items sum to 118 -> way off
        assert hybrid({})._looks_reliable(data) is False

    def test_small_rounding_difference_is_still_reliable(self, hybrid):
        data = _good_data()
        data["grand_total"] = 119.0  # < 5% off
        assert hybrid({})._looks_reliable(data) is True


def test_reliable_primary_result_is_used_without_calling_fallback(monkeypatch, hybrid):
    _patch_primary(monkeypatch, response=OCRResponse(
        provider="ppstructure", model="pp-structurev3", structured=_good_data(), duration_ms=1,
    ))
    result = hybrid({"fallback_provider": "gemini"}).extract_structured([], "p", "s")
    assert result.provider == "ppstructure"
    assert FakeFallback.called is False
    assert any("was not called" in w for w in result.warnings)


def test_unreliable_primary_falls_back_to_ai_engine(monkeypatch, hybrid):
    bad = _good_data()
    bad["overall_confidence"] = 0.1
    _patch_primary(monkeypatch, response=OCRResponse(
        provider="ppstructure", model="pp-structurev3", structured=bad, duration_ms=1,
    ))
    result = hybrid({"fallback_provider": "gemini"}).extract_structured([], "p", "s")
    assert result.provider == "gemini"
    assert FakeFallback.called is True
    assert any("unreliable" in w for w in result.warnings)


def test_primary_error_falls_back_to_ai_engine(monkeypatch, hybrid):
    _patch_primary(monkeypatch, error=OCRError("paddleocr not installed"))
    result = hybrid({"fallback_provider": "gemini"}).extract_structured([], "p", "s")
    assert result.provider == "gemini"
    assert FakeFallback.called is True
    assert any("paddleocr not installed" in w for w in result.warnings)


def test_primary_error_with_no_fallback_configured_raises_clear_error(monkeypatch, hybrid):
    import services.ocr_service as svc
    monkeypatch.delitem(PROVIDER_REGISTRY, "gemini", raising=False)
    _patch_primary(monkeypatch, error=OCRError("paddleocr not installed"))
    with pytest.raises(OCRError, match="no usable AI vision engine"):
        hybrid({"fallback_provider": "gemini"}).extract_structured([], "p", "s")
    monkeypatch.setitem(PROVIDER_REGISTRY, "gemini", svc.GeminiVisionProvider)
