"""
Universal Gemini engine.

Google Gemini as the primary document-understanding engine, built so that:

* No model name is hard-wired: the model comes from settings / the company's
  choice, and `list_models()` asks Google which models the key can use right
  now (so a retired model never strands a customer).
* The API key travels in the `x-goog-api-key` header, never in a URL where it
  could end up in a log or an error message.
* Long PDFs are processed in page groups (pages_per_call) and merged in
  order, instead of silently dropping pages; a group whose answer gets cut
  off by the output limit is split in half and re-asked, so item rows are
  never lost to truncation.
* Two key modes:
    customer — the customer's own Gemini key, stored encrypted per company.
    central  — SN Softech's key. On the SaaS server it is the server's
               GEMINI_API_KEY (never shown to customers); on an offline/local
               install it goes through the SN Softech Cloud AI proxy with the
               customer's licence key, so the real key never sits on a
               customer laptop.
* Token usage of every call is reported to an `on_usage` callback, which
  services/api_manager.py uses for customer-wise usage, cost estimates and
  monthly limits.
* Problems that retrying cannot fix (no key, invalid key, quota/limit) raise
  OCRConfigError so the parser can switch to the offline engine at once.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image

from services.ocr_service import (
    OCRConfigError,
    OCRError,
    OCRProvider,
    OCRResponse,
    ProxyVisionProvider,
    _describe_request_failure,
    _post_with_retry,
    to_base64,
)
from services.universal_schema import UNIVERSAL_SCHEMA_HINT
from utils.config import settings
from utils.logging import get_logger

log = get_logger("gemini")

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_PAGES_PER_CALL = 4
DEFAULT_MAX_OUTPUT_TOKENS = 32768

TOTAL_KEYS = (
    "subtotal", "discount_total", "tax_total", "cgst_total", "sgst_total",
    "igst_total", "cess_total", "freight", "packing_charges", "other_charges",
    "round_off", "grand_total", "balance_payable",
)
TEXT_TOTAL_KEYS = ("amount_in_words",)


class GeminiTruncated(OCRError):
    """The model stopped at its output limit — the answer is incomplete."""


# --------------------------------------------------------------------------
# Key / model helpers (used by the Settings page and the admin panel)
# --------------------------------------------------------------------------
def _requests():
    try:
        import requests
    except ImportError as exc:  # pragma: no cover
        raise OCRError("The 'requests' package is required.") from exc
    return requests


def list_models(api_key: str, timeout: int = 20) -> List[str]:
    """Model ids this key may call for generateContent, newest names first.

    Raises OCRConfigError for a bad key, OCRError for network trouble.
    """
    if not (api_key or "").strip():
        raise OCRConfigError("No Gemini API key entered.")
    requests = _requests()
    names: List[str] = []
    page_token = ""
    resp = None
    try:
        for _ in range(10):
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            resp = requests.get(
                f"{API_ROOT}/models",
                headers={"x-goog-api-key": api_key.strip()},
                params=params,
                timeout=timeout,
            )
            if resp.status_code in (400, 401, 403):
                raise OCRConfigError(
                    "Google rejected this API key (it is invalid, disabled, or "
                    "the Generative Language API is not enabled for it)."
                )
            resp.raise_for_status()
            body = resp.json()
            for model in body.get("models", []):
                methods = model.get("supportedGenerationMethods") or []
                name = (model.get("name") or "").replace("models/", "")
                if "generateContent" in methods and "gemini" in name and not any(
                    tag in name for tag in ("embedding", "tts", "image", "live", "audio")
                ):
                    names.append(name)
            page_token = body.get("nextPageToken") or ""
            if not page_token:
                break
    except OCRConfigError:
        raise
    except Exception as exc:
        raise OCRError(_describe_request_failure(exc, resp, "Gemini", api_key)) from exc
    # stable "latest" aliases first, then the rest alphabetically descending
    aliases = sorted(n for n in names if n.endswith("-latest"))
    rest = sorted((n for n in names if not n.endswith("-latest")), reverse=True)
    return aliases + rest


def validate_key(api_key: str) -> Tuple[bool, str, List[str]]:
    """(ok, message, models) — safe to show the message to the user."""
    try:
        models = list_models(api_key)
    except OCRConfigError as exc:
        return False, str(exc), []
    except OCRError as exc:
        return False, f"Could not reach Google to check the key: {exc}", []
    if not models:
        return False, "Key works, but no Gemini model with document support is enabled for it.", []
    return True, f"Key is valid — {len(models)} Gemini model(s) available.", models


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 8}{key[-4:]}"


def estimate_cost_inr(prompt_tokens: int, output_tokens: int) -> float:
    """Rough cost from configurable per-million-token USD rates."""
    usd = (
        prompt_tokens / 1_000_000 * settings.GEMINI_PRICE_INPUT_PER_M_USD
        + output_tokens / 1_000_000 * settings.GEMINI_PRICE_OUTPUT_PER_M_USD
    )
    return round(usd * settings.USD_TO_INR, 4)


# --------------------------------------------------------------------------
# Merging page-group answers
# --------------------------------------------------------------------------
def _first_filled(blocks: List[dict]) -> dict:
    merged: dict = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if value not in (None, "", 0, 0.0) and merged.get(key) in (None, "", 0, 0.0):
                merged[key] = value
            merged.setdefault(key, value)
    return merged


def merge_chunk_results(chunks: List[dict], page_offsets: List[int]) -> dict:
    """Merge answers for consecutive page groups of ONE document.

    Header blocks: first non-empty value per field. Items: concatenated in
    page order (page numbers re-based to the whole document). Totals: taken
    from the LAST group that printed them (totals sit on the last page).
    """
    if len(chunks) == 1:
        return chunks[0]
    merged: dict = {}
    merged["document_type"] = next(
        (c.get("document_type") for c in chunks if c.get("document_type")), ""
    )
    merged["document_type_confidence"] = max(
        (float(c.get("document_type_confidence") or 0) for c in chunks), default=0.0
    )
    for key in ("seller", "buyer", "supplier", "invoice"):
        merged[key] = _first_filled([c.get(key) or {} for c in chunks])
    items: List[dict] = []
    for chunk, offset in zip(chunks, page_offsets):
        for item in chunk.get("items") or []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            try:
                page = int(float(row.get("page") or 0))
            except (TypeError, ValueError):
                page = 0
            row["page"] = page + offset if page else offset + 1
            items.append(row)
    merged["items"] = items
    for key in TOTAL_KEYS + TEXT_TOTAL_KEYS:
        value = next(
            (c.get(key) for c in reversed(chunks) if c.get(key) not in (None, "", 0, 0.0)),
            0 if key in TOTAL_KEYS else "",
        )
        merged[key] = value
    conf: Dict[str, float] = {}
    for chunk in chunks:
        for k, v in (chunk.get("field_confidence") or {}).items():
            try:
                conf[k] = max(conf.get(k, 0.0), float(v or 0))
            except (TypeError, ValueError):
                continue
    merged["field_confidence"] = conf
    scores = [float(c.get("overall_confidence") or 0) for c in chunks if c.get("items")]
    merged["overall_confidence"] = min(scores) if scores else 0.0
    # Any other schema (bank statement rows, manifest consignments ...):
    # lists are concatenated in page order, dicts first-filled, scalars
    # taken from the last group that has them.
    handled = set(merged)
    for chunk in chunks:
        for key, value in chunk.items():
            if key in handled:
                continue
            if isinstance(value, list):
                merged.setdefault(key, [])
                merged[key].extend(value)
            elif isinstance(value, dict):
                merged[key] = _first_filled([merged.get(key) or {}, value])
            elif value not in (None, "", 0, 0.0) or key not in merged:
                merged[key] = value
    return merged


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------
class GeminiUniversalProvider(OCRProvider):
    """Gemini with page grouping, key modes, usage reporting.

    config keys:
      api_key          customer key (customer mode) or server key (central/direct)
      transport        "direct" | "proxy"
      model            model id (default settings.GEMINI_MODEL)
      pages_per_call   page-group size (default 4)
      on_usage         callable(dict) — receives one record per API call
      proxy_url / licence_key   for transport="proxy"
      key_mode         "customer" | "central" (only used in messages/usage)
    """

    name = "universal"
    supports_structured = True
    requires_api_key = True
    uses_universal_schema = True

    @property
    def model_name(self) -> str:
        return (self.config.get("model") or settings.GEMINI_MODEL).strip()

    @property
    def transport(self) -> str:
        return self.config.get("transport") or "direct"

    @property
    def pages_per_call(self) -> int:
        try:
            return min(8, max(1, int(self.config.get("pages_per_call") or settings.GEMINI_PAGES_PER_CALL or DEFAULT_PAGES_PER_CALL)))
        except (TypeError, ValueError):
            return DEFAULT_PAGES_PER_CALL

    def _report(self, record: dict) -> None:
        callback: Optional[Callable] = self.config.get("on_usage")
        if callback:
            try:
                callback(record)
            except Exception as exc:  # usage logging must never break extraction
                log.warning("Usage callback failed: %s", exc)

    # -- one HTTP call -------------------------------------------------
    def _call_direct(self, images: List[Image.Image], prompt: str, schema_hint: str,
                     max_tokens: int) -> Tuple[str, dict]:
        key = (self.config.get("api_key") or "").strip()
        if not key:
            raise OCRConfigError(
                "No Gemini API key is set. Add your key in API Settings, or ask "
                "SN Softech Solutions to enable the central key for your account."
            )
        requests = _requests()
        parts: list = [{"text": f"{prompt}\n\n{schema_hint}" if schema_hint else prompt}]
        for img in images:
            b64, media = to_base64(img, "jpeg")
            parts.append({"inline_data": {"mime_type": media, "data": b64}})
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "maxOutputTokens": max_tokens,
            },
        }
        resp = None
        try:
            resp = _post_with_retry(
                requests,
                f"{API_ROOT}/models/{self.model_name}:generateContent",
                headers={"x-goog-api-key": key},
                json=payload,
                timeout=settings.OCR_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise OCRError(_describe_request_failure(exc, resp, "Gemini", key)) from exc
        if resp.status_code in (400, 401, 403):
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                pass
            if "API key" in detail or resp.status_code in (401, 403):
                raise OCRConfigError(
                    "Gemini rejected the API key. Check it in API Settings."
                    + (f" ({detail.replace(key, '***')[:200]})" if detail else "")
                )
            raise OCRError(f"Gemini refused the request: {detail[:300] or resp.status_code}")
        if resp.status_code == 404:
            raise OCRConfigError(
                f"Gemini model '{self.model_name}' is not available. Pick another "
                "model in API Settings (the list is fetched live from Google)."
            )
        if resp.status_code == 429:
            raise OCRConfigError(
                "Gemini quota exceeded for this key (too many requests or daily "
                "limit). Wait and retry, or raise the quota in Google AI Studio."
            )
        try:
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            raise OCRError(_describe_request_failure(exc, resp, "Gemini", key)) from exc

        usage_meta = body.get("usageMetadata") or {}
        usage = {
            "prompt_tokens": int(usage_meta.get("promptTokenCount") or 0),
            "output_tokens": int(usage_meta.get("candidatesTokenCount") or 0)
            + int(usage_meta.get("thoughtsTokenCount") or 0),
            "total_tokens": int(usage_meta.get("totalTokenCount") or 0),
        }
        candidates = body.get("candidates") or []
        if not candidates:
            reason = (body.get("promptFeedback") or {}).get("blockReason", "no answer")
            raise OCRError(f"Gemini returned no result ({reason}).")
        candidate = candidates[0]
        text = "".join(
            p.get("text", "") for p in (candidate.get("content") or {}).get("parts", [])
            if not p.get("thought")
        )
        if candidate.get("finishReason") == "MAX_TOKENS":
            raise GeminiTruncated("Gemini's answer was cut off at the output limit.")
        return text, usage

    def _call_proxy(self, images, prompt, schema_hint) -> Tuple[str, dict]:
        proxy = ProxyVisionProvider({
            "api_key": self.config.get("licence_key", ""),
            "proxy_url": self.config.get("proxy_url", ""),
        })
        try:
            response = proxy.extract_structured(images, prompt, schema_hint)
        except OCRError as exc:
            if "licence key" in str(exc).lower() or "no cloud ai proxy" in str(exc).lower():
                raise OCRConfigError(str(exc)) from exc
            raise
        return response.raw_response, {}

    # -- one page group, splitting on truncation -------------------------
    def _extract_group(self, images, prompt_template, schema_hint, first_page, total_pages,
                       max_tokens) -> List[Tuple[dict, int]]:
        last = first_page + len(images) - 1
        page_note = (
            f"You are given pages {first_page}-{last} of a {total_pages}-page document "
            "(all pages are ONE document). Extract only item rows printed on these "
            "pages; fill header fields and totals only if printed on these pages."
            if total_pages > 1 and len(images) < total_pages
            else ("All pages supplied belong to ONE document; merge item rows in order."
                  if len(images) > 1 else "")
        )
        prompt = prompt_template.replace("{page_note}", page_note)
        started = time.time()
        try:
            if self.transport == "proxy":
                text, usage = self._call_proxy(images, prompt, schema_hint)
            else:
                text, usage = self._call_direct(images, prompt, schema_hint, max_tokens)
        except GeminiTruncated:
            if len(images) == 1:
                raise OCRError(
                    f"Page {first_page} has more rows than Gemini can return in one "
                    "answer. Crop the page into two halves and upload them as one bill."
                )
            half = len(images) // 2
            log.info("Truncated answer for pages %s-%s, splitting", first_page, last)
            return (
                self._extract_group(images[:half], prompt_template, schema_hint,
                                    first_page, total_pages, max_tokens)
                + self._extract_group(images[half:], prompt_template, schema_hint,
                                      first_page + half, total_pages, max_tokens)
            )
        duration = int((time.time() - started) * 1000)
        self._report({
            "provider": self.name,
            "model": self.model_name if self.transport == "direct" else "sn-cloud-ai",
            "key_mode": self.config.get("key_mode", "customer"),
            "pages": len(images),
            "duration_ms": duration,
            **usage,
        })
        from services.ai_invoice_parser import ParseError, loads_strict
        try:
            data = loads_strict(text)
        except ParseError as exc:
            raise OCRError(f"Gemini returned malformed JSON for pages {first_page}-{last}: {exc}")
        return [(data, first_page - 1)]

    # -- public interface -------------------------------------------------
    def extract_structured(self, images, prompt, schema_hint) -> OCRResponse:
        if self.config.get("blocked_reason"):
            raise OCRConfigError(self.config["blocked_reason"])
        if not images:
            # used by the parser's JSON-repair call
            text, usage = (self._call_proxy([], prompt, schema_hint)
                           if self.transport == "proxy"
                           else self._call_direct([], prompt, schema_hint, 8192))
            return OCRResponse(provider=self.name, model=self.model_name,
                               raw_response=text, usage=usage)
        started = time.time()
        max_tokens = int(self.config.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS)
        size = self.pages_per_call
        results: List[Tuple[dict, int]] = []
        for start in range(0, len(images), size):
            group = images[start:start + size]
            results.extend(self._extract_group(
                group, prompt, schema_hint, start + 1, len(images), max_tokens
            ))
        merged = merge_chunk_results([r[0] for r in results], [r[1] for r in results])
        warnings = []
        if len(results) > 1:
            warnings.append(
                f"{len(images)}-page document read in {len(results)} parts and merged "
                "— check that no item row is repeated or missing at page breaks."
            )
        return OCRResponse(
            provider=self.name,
            model=self.model_name if self.transport == "direct" else "sn-cloud-ai",
            structured=merged,
            raw_response=json.dumps(merged, ensure_ascii=False),
            duration_ms=int((time.time() - started) * 1000),
            warnings=warnings,
        )

    def extract_text(self, images: List[Image.Image]) -> OCRResponse:
        text, usage = (
            self._call_proxy(images, "Transcribe every line exactly as printed. Plain text.", "")
            if self.transport == "proxy"
            else self._call_direct(
                images, "Transcribe every line exactly as printed. Return JSON "
                '{"text": "..."}.', "", 16384)
        )
        try:
            text = json.loads(text).get("text", text)
        except Exception:
            pass
        return OCRResponse(provider=self.name, model=self.model_name, text=text, usage=usage)

    def health_check(self):
        if self.transport == "proxy":
            proxy = ProxyVisionProvider({
                "api_key": self.config.get("licence_key", ""),
                "proxy_url": self.config.get("proxy_url", ""),
            })
            return proxy.health_check()
        key = (self.config.get("api_key") or "").strip()
        if not key:
            return False, "No Gemini API key available for this account."
        ok, message, models = validate_key(key)
        if ok and self.model_name not in models and not self.model_name.endswith("-latest"):
            return False, f"{message} But model '{self.model_name}' is not among them."
        return ok, message


def universal_prompt(our_company: str = "", flow: str = "purchase") -> str:
    """The universal extraction prompt, with {page_note} left for the engine."""
    from services.universal_schema import UNIVERSAL_PROMPT
    company = our_company.strip() or "our company"
    return (
        UNIVERSAL_PROMPT
        .replace("{our_company}", company)
        .replace("{our_role_buyer}", f"{company} (the buyer)")
    )


__all__ = [
    "GeminiUniversalProvider", "list_models", "validate_key", "mask_key",
    "estimate_cost_inr", "merge_chunk_results", "universal_prompt",
    "UNIVERSAL_SCHEMA_HINT",
]


# Make sure the engine is selectable even when this module was imported
# before services.ocr_service finished loading.
from services import ocr_service as _ocr  # noqa: E402
_ocr.PROVIDER_REGISTRY["universal"] = GeminiUniversalProvider
_ocr.PROVIDER_LABELS["universal"] = (
    "Universal AI — Gemini, all document types, offline fallback (recommended)"
)
