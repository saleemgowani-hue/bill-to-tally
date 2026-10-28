"""Transient API errors (503/429/timeouts) must be retried; real client errors must not."""
import pytest
import requests as real_requests

from services import ocr_service
from services.ocr_service import GeminiVisionProvider, OCRError, _post_with_retry


class FakeResp:
    def __init__(self, status, headers=None, url=""):
        self.status_code = status
        self.headers = headers or {}
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise real_requests.exceptions.HTTPError(
                f"{self.status_code} Server Error: for url: {self.url}", response=self
            )

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}


class FakeRequests:
    """Stands in for the `requests` module, replaying a scripted sequence."""
    exceptions = real_requests.exceptions

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ocr_service.time, "sleep", lambda s: None)


def test_retries_503_then_succeeds():
    fake = FakeRequests([FakeResp(503), FakeResp(503), FakeResp(200)])
    resp = _post_with_retry(fake, "http://x")
    assert resp.status_code == 200
    assert fake.calls == 3


def test_retries_429_then_succeeds():
    fake = FakeRequests([FakeResp(429), FakeResp(200)])
    assert _post_with_retry(fake, "http://x").status_code == 200
    assert fake.calls == 2


def test_gives_up_after_max_attempts_and_returns_last_response():
    fake = FakeRequests([FakeResp(503)] * 4)
    resp = _post_with_retry(fake, "http://x", max_attempts=4)
    assert resp.status_code == 503
    assert fake.calls == 4


def test_client_errors_are_not_retried():
    for status in (400, 401, 403, 404):
        fake = FakeRequests([FakeResp(status)])
        assert _post_with_retry(fake, "http://x").status_code == status
        assert fake.calls == 1


def test_timeout_is_retried_then_succeeds():
    fake = FakeRequests([real_requests.exceptions.Timeout("slow"), FakeResp(200)])
    assert _post_with_retry(fake, "http://x").status_code == 200
    assert fake.calls == 2


def test_timeout_on_every_attempt_finally_raises():
    fake = FakeRequests([real_requests.exceptions.Timeout("slow")] * 3)
    with pytest.raises(real_requests.exceptions.Timeout):
        _post_with_retry(fake, "http://x", max_attempts=3)
    assert fake.calls == 3


def test_retry_after_header_is_honoured_but_capped(monkeypatch):
    waits = []
    monkeypatch.setattr(ocr_service.time, "sleep", lambda s: waits.append(s))
    fake = FakeRequests([FakeResp(429, {"Retry-After": "999"}), FakeResp(200)])
    _post_with_retry(fake, "http://x")
    assert waits == [15.0]


# Gemini puts the API key in the request URL's query string (?key=...), and
# requests.Response.raise_for_status() embeds the response's own .url
# verbatim in its exception text — which used to reach the review screen
# as-is (a real key was seen leaking into the Streamlit UI this way).
SECRET_KEY = "FAKE-TEST-KEY-not-a-real-credential-0123456789"


class _FakeGeminiProvider(GeminiVisionProvider):
    """A GeminiVisionProvider whose `requests` is the scripted FakeRequests
    above, so extract_structured() can be driven end to end without a real
    network call."""

    def __init__(self, fake_requests, key=SECRET_KEY):
        super().__init__({"api_key": key})
        self._fake = fake_requests

    def _requests(self):
        return self._fake


def test_api_key_never_leaks_into_the_error_message(monkeypatch):
    monkeypatch.setattr(ocr_service.time, "sleep", lambda s: None)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?key={SECRET_KEY}"
    fake = FakeRequests([FakeResp(503, url=url)] * 4)
    provider = _FakeGeminiProvider(fake)
    with pytest.raises(OCRError) as exc_info:
        provider.extract_structured([], "prompt", "schema")
    message = str(exc_info.value)
    assert SECRET_KEY not in message
    assert "***" in message


def test_sustained_503_gives_a_plain_language_overload_message(monkeypatch):
    monkeypatch.setattr(ocr_service.time, "sleep", lambda s: None)
    fake = FakeRequests([FakeResp(503, url="https://x?key=" + SECRET_KEY)] * 4)
    provider = _FakeGeminiProvider(fake)
    with pytest.raises(OCRError) as exc_info:
        provider.extract_structured([], "prompt", "schema")
    message = str(exc_info.value)
    assert "temporarily overloaded" in message
    assert "not a problem with your bill or API key" in message


def test_a_real_client_error_keeps_the_plain_message(monkeypatch):
    monkeypatch.setattr(ocr_service.time, "sleep", lambda s: None)
    fake = FakeRequests([FakeResp(401, url="https://x?key=" + SECRET_KEY)])
    provider = _FakeGeminiProvider(fake)
    with pytest.raises(OCRError) as exc_info:
        provider.extract_structured([], "prompt", "schema")
    message = str(exc_info.value)
    assert SECRET_KEY not in message
    assert "temporarily overloaded" not in message
    assert "Gemini request failed:" in message
