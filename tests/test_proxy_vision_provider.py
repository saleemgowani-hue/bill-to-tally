"""ProxyVisionProvider: the client side of ocr_proxy/ (see its README). This
provider never holds a real Gemini key - the "API key" it sends is the
customer's own SN Softech licence key, forwarded to the vendor's proxy."""
import pytest
import requests as real_requests
from PIL import Image

from services import ocr_service
from services.ocr_service import OCRError, ProxyVisionProvider
from utils.config import settings


class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self.headers = {}
        self.url = ""
        self._body = body or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise real_requests.exceptions.HTTPError(
                f"{self.status_code} error", response=self
            )

    def json(self):
        return self._body


class FakeRequests:
    exceptions = real_requests.exceptions

    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.last_payload = None
        self.last_url = None
        self.calls = 0

    def post(self, url, json=None, **kwargs):
        self.calls += 1
        self.last_url = url
        self.last_payload = json
        if self.raise_exc:
            raise self.raise_exc
        return self.response

    def get(self, url, **kwargs):
        self.calls += 1
        self.last_url = url
        return self.response


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ocr_service.time, "sleep", lambda s: None)


@pytest.fixture(autouse=True)
def proxy_url(monkeypatch):
    monkeypatch.setattr(settings, "OCR_PROXY_URL", "https://proxy.example.com")


def _provider(api_key="SNST-TEST-TEST-TEST-TEST"):
    p = ProxyVisionProvider({"api_key": api_key})
    return p


def test_registered_under_proxy_with_a_label():
    assert ocr_service.PROVIDER_REGISTRY["proxy"] is ProxyVisionProvider
    assert "proxy" in ocr_service.PROVIDER_LABELS


def test_sends_licence_key_prompt_and_images_to_the_proxy_url():
    provider = _provider()
    fake = FakeRequests(FakeResp(200, {"text": "{}", "model": "sn-cloud-ai"}))
    provider._requests = lambda: fake
    img = Image.new("RGB", (10, 10), "white")

    provider.extract_structured([img], "read this bill", "schema hint")

    assert fake.last_url == "https://proxy.example.com/v1/extract"
    payload = fake.last_payload
    assert payload["license_key"] == "SNST-TEST-TEST-TEST-TEST"
    assert payload["prompt"] == "read this bill"
    assert payload["schema_hint"] == "schema hint"
    assert len(payload["images"]) == 1
    assert "mime_type" in payload["images"][0] and "data" in payload["images"][0]


def test_raw_response_is_the_proxys_text_field():
    provider = _provider()
    fake = FakeRequests(FakeResp(200, {"text": '{"supplier": {}}', "model": "sn-cloud-ai"}))
    provider._requests = lambda: fake
    img = Image.new("RGB", (10, 10), "white")

    response = provider.extract_structured([img], "p", "s")
    assert response.raw_response == '{"supplier": {}}'
    assert response.provider == "proxy"


def test_missing_licence_key_fails_fast_without_a_network_call():
    provider = _provider(api_key="")
    fake = FakeRequests(FakeResp(200, {"text": "{}"}))
    provider._requests = lambda: fake
    img = Image.new("RGB", (10, 10), "white")

    with pytest.raises(OCRError, match="licence key"):
        provider.extract_structured([img], "p", "s")
    assert fake.calls == 0


def test_missing_proxy_url_fails_with_a_clear_message(monkeypatch):
    monkeypatch.setattr(settings, "OCR_PROXY_URL", "")
    provider = _provider()
    img = Image.new("RGB", (10, 10), "white")

    with pytest.raises(OCRError, match="OCR_PROXY_URL"):
        provider.extract_structured([img], "p", "s")


def test_401_from_proxy_surfaces_as_ocr_error_not_a_crash():
    provider = _provider()
    fake = FakeRequests(FakeResp(401, text="Invalid licence key."))
    provider._requests = lambda: fake
    img = Image.new("RGB", (10, 10), "white")

    with pytest.raises(OCRError):
        provider.extract_structured([img], "p", "s")


def test_429_rate_limit_is_retried_then_surfaces_clearly():
    """The proxy's own daily-limit 429 should go through the same
    retry+backoff as a direct Gemini call before being reported."""
    fake = FakeRequests()
    responses = [FakeResp(429), FakeResp(429), FakeResp(429), FakeResp(429)]
    fake.post = lambda url, json=None, **kw: responses.pop(0)
    provider = _provider()
    provider._requests = lambda: fake
    img = Image.new("RGB", (10, 10), "white")

    with pytest.raises(OCRError):
        provider.extract_structured([img], "p", "s")


def test_health_check_reports_missing_proxy_url(monkeypatch):
    monkeypatch.setattr(settings, "OCR_PROXY_URL", "")
    ok, message = _provider().health_check()
    assert not ok
    assert "OCR_PROXY_URL" in message


def test_health_check_reports_missing_licence_key():
    ok, message = _provider(api_key="").health_check()
    assert not ok
    assert "licence key" in message.lower()


def test_health_check_pings_the_proxys_health_endpoint():
    provider = _provider()
    fake = FakeRequests(FakeResp(200, {"ok": True}))
    provider._requests = lambda: fake
    ok, message = provider.health_check()
    assert ok
    assert fake.last_url == "https://proxy.example.com/health"
