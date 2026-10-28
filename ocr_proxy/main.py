"""
SN Softech Cloud AI proxy — runs on the VENDOR's own server, never shipped
inside a customer build.

Why this exists: BillToTallyAgent can use a paid vision AI engine (Gemini)
for much better table accuracy than the offline engines. The straightforward
way to offer that — put a real Gemini API key in every customer's local
.env — means the key sits on each customer's laptop where it can be copied
out and used (and billed) by anyone with file access.

Instead: this proxy holds the ONE real Gemini key, on the vendor's own
server. Each customer's app calls this proxy over the internet, with their
own SN Softech licence key as proof they are a real customer. The proxy
checks that key's signature (license_check.py — the same algorithm the app
itself uses to mint and check keys), applies a daily request cap per key
(ratelimit.py) so a leaked licence key cannot run up an unbounded bill, then
forwards the request to Gemini and relays the answer back. The real Gemini
key never leaves this server.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # fill in GEMINI_API_KEY and LICENSE_SIGNING_SECRET
    uvicorn main:app --host 0.0.0.0 --port 8787

Deploy: see README.md.
"""
from __future__ import annotations

import os
import time
from typing import List

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine in production where the platform injects env vars directly

from license_check import is_valid
from ratelimit import check_and_increment

app = FastAPI(title="SN Softech Cloud AI Proxy", docs_url="/docs")

LICENSE_SIGNING_SECRET = os.getenv("LICENSE_SIGNING_SECRET", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
DAILY_REQUEST_LIMIT = int(os.getenv("DAILY_REQUEST_LIMIT", "60"))
REQUEST_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "120"))


class ImagePart(BaseModel):
    mime_type: str
    data: str  # base64-encoded image bytes


class ExtractRequest(BaseModel):
    license_key: str
    prompt: str
    schema_hint: str = ""
    images: List[ImagePart]


@app.get("/health")
def health():
    """No secrets in the response - just whether this deployment is wired up,
    so the vendor can sanity-check a deploy without exposing anything."""
    return {
        "ok": True,
        "gemini_key_configured": bool(GEMINI_API_KEY),
        "licence_secret_configured": bool(LICENSE_SIGNING_SECRET),
        "daily_request_limit": DAILY_REQUEST_LIMIT,
    }


@app.post("/v1/extract")
def extract(req: ExtractRequest):
    if not LICENSE_SIGNING_SECRET or not GEMINI_API_KEY:
        # A misconfigured deploy must fail loudly for the vendor, not quietly
        # pretend to work and then fail every customer one at a time.
        raise HTTPException(500, "Proxy is not configured. Check its .env.")

    if not is_valid(req.license_key, LICENSE_SIGNING_SECRET):
        raise HTTPException(401, "Invalid licence key.")
    if not req.images:
        raise HTTPException(400, "No images supplied.")

    allowed, count = check_and_increment(req.license_key, DAILY_REQUEST_LIMIT)
    if not allowed:
        raise HTTPException(
            429,
            f"Daily limit of {DAILY_REQUEST_LIMIT} bills reached for this "
            f"licence key. It resets at midnight, or contact SN Softech "
            f"Solutions to raise the limit.",
        )

    parts: list = [{"text": f"{req.prompt}\n\n{req.schema_hint}"}]
    for img in req.images[:8]:
        parts.append({"inline_data": {"mime_type": img.mime_type, "data": img.data}})

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
            "maxOutputTokens": 8192,
        },
    }

    started = time.time()
    resp = None
    try:
        resp = requests.post(
            url, params={"key": GEMINI_API_KEY}, json=payload, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        body = resp.json()
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except requests.HTTPError:
        detail = (resp.text[:300] if resp is not None else "no response")
        raise HTTPException(
            resp.status_code if resp is not None else 502,
            f"Gemini request failed: {detail}",
        )
    except Exception as exc:
        raise HTTPException(502, f"Gemini request failed: {exc}")

    return {
        "text": text,
        "model": GEMINI_MODEL,
        "duration_ms": int((time.time() - started) * 1000),
        "requests_today": count,
        "daily_limit": DAILY_REQUEST_LIMIT,
    }
