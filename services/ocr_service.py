"""
OCR / vision abstraction layer.

The rest of the application only talks to `OCRProvider`. Swapping Gemini for
Claude, OpenAI, Tesseract or PaddleOCR is a configuration change, never a code
change.

Two capability levels:

* text providers      -> `extract_text()` only (Tesseract, PaddleOCR)
* structured providers-> `extract_structured()` returns invoice JSON directly
                         (Gemini / OpenAI / Claude vision models)

`InvoiceParser` (ai_invoice_parser.py) picks the right path automatically.
"""
from __future__ import annotations

import abc
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Type

from pathlib import Path

from PIL import Image

from utils.config import settings
from utils.image_tools import to_base64
from utils.image_tools import (
    auto_deskew,
    enhance,
    fingerprint_distance,
    perceptual_fingerprint,
)
from utils.logging import get_logger

log = get_logger("ocr")


class OCRError(RuntimeError):
    """Raised when an OCR/vision provider cannot produce a usable result."""


@dataclass
class OCRResponse:
    provider: str
    model: str = ""
    text: str = ""
    structured: Optional[dict] = None
    duration_ms: int = 0
    attempts: int = 1
    raw_response: str = ""
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
class OCRProvider(abc.ABC):
    """Base interface every OCR/vision provider must implement."""

    name: str = "base"
    supports_structured: bool = False
    requires_api_key: bool = False

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}

    @abc.abstractmethod
    def extract_text(self, images: List[Image.Image]) -> OCRResponse:
        """Return plain text for one or more page images."""

    def extract_structured(
        self, images: List[Image.Image], prompt: str, schema_hint: str
    ) -> OCRResponse:
        raise NotImplementedError(
            f"{self.name} does not support structured extraction"
        )

    def health_check(self) -> tuple[bool, str]:
        return True, "ready"

    @property
    def model_name(self) -> str:
        return self.config.get("model", "")


# --------------------------------------------------------------------------
# Mock provider - used for tests, demos and offline development
# --------------------------------------------------------------------------
class MockOCRProvider(OCRProvider):
    name = "mock"
    supports_structured = True

    SAMPLE = {
        "supplier": {
            "name": "Shreeji Distributors",
            "gstin": "23ABCDE1234F1Z5",
            "address": "12 MG Road, Indore, Madhya Pradesh - 452001",
        },
        "invoice": {
            "invoice_number": "SD/2026/1187",
            "invoice_date": "2026-09-04",
            "due_date": "2026-10-04",
        },
        "items": [
            {
                "raw_name": "Parle G Gold 800G",
                "normalized_name": "Parle G Gold 800 G",
                "hsn": "19053100",
                "barcode": "8901719101234",
                "quantity": 24,
                "unit": "PCS",
                "rate": 95.0,
                "discount": 0.0,
                "taxable_value": 2280.0,
                "gst_rate": 18.0,
                "cgst": 205.2,
                "sgst": 205.2,
                "igst": 0.0,
                "cess": 0.0,
                "total": 2690.4,
                "confidence": 0.96,
            },
            {
                "raw_name": "Tata Salt 1KG",
                "normalized_name": "Tata Salt 1 KG",
                "hsn": "25010020",
                "barcode": "",
                "quantity": 50,
                "unit": "PCS",
                "rate": 22.0,
                "discount": 50.0,
                "taxable_value": 1050.0,
                "gst_rate": 5.0,
                "cgst": 26.25,
                "sgst": 26.25,
                "igst": 0.0,
                "cess": 0.0,
                "total": 1102.5,
                "confidence": 0.93,
            },
            {
                "raw_name": "Amul Butter 500 gm",
                "normalized_name": "Amul Butter 500 GM",
                "hsn": "04051000",
                "barcode": "",
                "quantity": 12,
                "unit": "PCS",
                "rate": 265.0,
                "discount": 0.0,
                "taxable_value": 3180.0,
                "gst_rate": 12.0,
                "cgst": 190.8,
                "sgst": 190.8,
                "igst": 0.0,
                "cess": 0.0,
                "total": 3561.6,
                "confidence": 0.64,
            },
        ],
        "subtotal": 6510.0,
        "discount_total": 50.0,
        "tax_total": 844.5,
        "cess_total": 0.0,
        "round_off": 0.0,
        "grand_total": 7354.5,
        "overall_confidence": 0.86,
    }

    # A real transporter's manifest (names/consignment numbers as actually
    # written), for demoing Manifest to Excel without an API key.
    MANIFEST_SAMPLE = {
        "manifest_no": "2264",
        "manifest_date": "2026-10-06",
        "from_station": "K. Gate, Delhi",
        "to_station": "Raipur (MP)",
        "rows": [
            {"s_no": 1, "c_no": "148504", "station": "Bhilai", "consignee": "Soni Auto",
             "weight": 3, "no_of_pkg": 1, "to_pay": "", "paid": "Paid", "amount": "",
             "confidence": 0.9},
            {"s_no": 2, "c_no": "148503", "station": "Raipur", "consignee": "Brijlal Mahajan",
             "weight": 2, "no_of_pkg": 1, "to_pay": 240, "paid": "", "amount": "",
             "confidence": 0.55},
            {"s_no": 3, "c_no": "148493", "station": "Raipur", "consignee": "SHS",
             "weight": 12, "no_of_pkg": 1, "to_pay": 1440, "paid": "", "amount": "",
             "confidence": 0.6},
            {"s_no": 4, "c_no": "535583", "station": "Raipur", "consignee": "Kara Jaya",
             "weight": 3, "no_of_pkg": 1, "to_pay": 360, "paid": "", "amount": "",
             "confidence": 0.55},
            {"s_no": 5, "c_no": "148513", "station": "Raipur", "consignee": "Jaya Auto",
             "weight": 5, "no_of_pkg": 1, "to_pay": 600, "paid": "", "amount": "",
             "confidence": 0.6},
            {"s_no": 6, "c_no": "3014", "station": "Raigarh", "consignee": "Raigarh Auto",
             "weight": 1, "no_of_pkg": 1, "to_pay": "", "paid": "Paid", "amount": "",
             "confidence": 0.5},
            {"s_no": 7, "c_no": "148517", "station": "Raipur", "consignee": "Rameshwar Soni",
             "weight": 1, "no_of_pkg": 1, "to_pay": 120, "paid": "", "amount": "",
             "confidence": 0.65},
            {"s_no": 8, "c_no": "148532", "station": "Raipur", "consignee": "Shri Bajaj",
             "weight": 12, "no_of_pkg": 1, "to_pay": 1440, "paid": "", "amount": "",
             "confidence": 0.5},
            {"s_no": 9, "c_no": "148542", "station": "Raipur", "consignee": "Ayan",
             "weight": 1, "no_of_pkg": 1, "to_pay": 120, "paid": "", "amount": "",
             "confidence": 0.7},
        ],
        "overall_confidence": 0.6,
    }

    BANK_SAMPLE = {
        "transactions": [
            {
                "date": "2026-09-01", "narration": "NEFT DR-HDFC0000123-SHREEJI DISTRIBUTORS-INV-101",
                "reference_no": "N123456789", "debit": 12600.0, "credit": 0.0, "balance": 87400.0,
                "confidence": 0.95,
            },
            {
                "date": "2026-09-05", "narration": "NEFT CR-ICIC0000456-RETAIL MART-SI-1",
                "reference_no": "N987654321", "debit": 0.0, "credit": 3200.5, "balance": 90600.5,
                "confidence": 0.95,
            },
            {
                "date": "2026-09-07", "narration": "BANK CHARGES-QTR MAINTENANCE",
                "reference_no": "", "debit": 236.0, "credit": 0.0, "balance": 90364.5,
                "confidence": 0.9,
            },
        ],
        "overall_confidence": 0.93,
    }

    def extract_text(self, images: List[Image.Image]) -> OCRResponse:
        return OCRResponse(
            provider=self.name,
            model="mock-1",
            text="MOCK OCR TEXT — configure a real provider in Settings.",
            duration_ms=5,
        )

    # Demo bills shipped in samples/. Loaded once, lazily.
    _catalogue = None

    @classmethod
    def catalogue(cls):
        """The demo bills, keyed by image fingerprint.

        Without this the mock provider returns the same invoice for every
        upload, so a demo of four suppliers looks like one supplier billed
        four times — and three of them get flagged as duplicates.
        """
        if cls._catalogue is None:
            path = Path(__file__).resolve().parent.parent / "samples" / "sample_bills.json"
            try:
                cls._catalogue = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cls._catalogue = []
        return cls._catalogue

    def _match(self, images):
        """Nearest fingerprint wins.

        Nearest-neighbour rather than a fixed threshold: a photographed or
        re-saved bill drifts far enough from its original that any threshold
        loose enough to catch it is also loose enough to catch the wrong bill.
        """
        catalogue = self.catalogue()
        if not catalogue or not images:
            return None
        try:
            taken = perceptual_fingerprint(images[0])
        except Exception:
            return None
        scored = sorted(
            (fingerprint_distance(entry.get("fingerprint", ""), taken), index)
            for index, entry in enumerate(catalogue)
        )
        best_distance, best_index = scored[0]
        # Measured on the shipped bills: a resized or skewed copy stays within
        # 55, while an unrelated page sits at 68 or more. That is a thin margin
        # — this is a demo aid, not a recogniser. A photograph of a printed bill
        # will usually miss and fall back to the built-in sample, which is the
        # honest outcome: use a real OCR provider for real bills.
        if best_distance > 60:
            return None
        return catalogue[best_index]

    def extract_structured(self, images, prompt, schema_hint) -> OCRResponse:
        if '"manifest_no"' in schema_hint:
            data = json.loads(json.dumps(self.MANIFEST_SAMPLE))  # deep copy
            return OCRResponse(
                provider=self.name,
                model="mock-1",
                structured=data,
                raw_response=json.dumps(data),
                duration_ms=8,
                warnings=[
                    "Mock provider active — fixed sample manifest, not a real "
                    "reading of your bilty."
                ],
            )
        if '"transactions"' in schema_hint:
            data = json.loads(json.dumps(self.BANK_SAMPLE))  # deep copy
            return OCRResponse(
                provider=self.name,
                model="mock-1",
                structured=data,
                raw_response=json.dumps(data),
                duration_ms=8,
                warnings=[
                    "Mock provider active — fixed sample transactions, not a real "
                    "reading of your statement."
                ],
            )
        matched = self._match(images)
        source = matched["data"] if matched else self.SAMPLE
        data = json.loads(json.dumps(source))  # deep copy
        label = matched["file"] if matched else "built-in sample"
        return OCRResponse(
            provider=self.name,
            model="mock-1",
            structured=data,
            raw_response=json.dumps(data),
            duration_ms=8,
            warnings=[
                f"Mock provider active — sample data from {label}, not a real reading "
                "of your bill."
            ],
        )

    def health_check(self):
        return True, "Mock provider always available"


# --------------------------------------------------------------------------
# Local OCR engines
# --------------------------------------------------------------------------
def _prepare_for_text_ocr(img: Image.Image) -> Image.Image:
    """Deskew + grayscale + contrast boost before a text-only OCR engine.

    Vision LLMs (Gemini/OpenAI/Claude) handle a raw phone photo well on
    their own. Tesseract/PaddleOCR do not — without this, a skewed, dim
    phone photo routinely produces near-unreadable text and near-zero
    extraction, which is what "10% confidence, nothing extracted" usually
    means. This always runs for text-only engines, regardless of whether
    the user separately checked "apply image adjustments" on the upload
    screen (that checkbox is for optional manual tweaks on top of this).
    """
    try:
        out = auto_deskew(img)
        out = enhance(out, contrast=1.35, sharpness=1.2, grayscale=True)
        return out
    except Exception as exc:  # pragma: no cover - never let preprocessing crash OCR
        log.warning("Pre-OCR image preparation failed, using original image: %s", exc)
        return img


def _describe_ppstructure_failure(exc: Exception, action: str) -> str:
    """Turn a raw PaddlePaddle/PaddleX exception into a message that tells
    the user what to actually do, logging the full traceback either way
    since PaddleX's own text is too sparse to self-diagnose without it.
    """
    import traceback

    detail = traceback.format_exc()
    log.error("PP-StructureV3 failed to %s:\n%s", action, detail)
    text = str(exc)
    if "ConvertPirAttribute2RuntimeAttribute" in text or "onednn_instruction" in text:
        return (
            f"PP-StructureV3 failed to {action}: {exc}\n\n"
            "This is a known bug in paddlepaddle 3.3.0/3.3.1 (its CPU oneDNN "
            "engine can't run this model yet — see PaddlePaddle/Paddle#77340). "
            "Fix: downgrade it in your venv —\n"
            '  pip install "paddlepaddle==3.2.2"\n'
            "then restart the app and try again. Full error logged to the "
            "console/log file."
        )
    return (
        f"PP-StructureV3 failed to {action}: {exc}\n\n"
        'Try: pip install "paddlepaddle==3.2.2" "paddleocr[doc-parser]" '
        '"paddlex[ocr]" (all three matter — a newer paddlepaddle can break '
        "this pipeline). Full error logged to the console/log file."
    )


class PPStructureProvider(OCRProvider):
    """
    PP-StructureV3 — local, offline layout + table detection (no LLM, no
    API key). Unlike the plain PaddleOCRProvider above (flat text only),
    this one detects real tables, so items are read from actual columns
    instead of guessed with a line-by-line regex.

    IMPORTANT — not verified against a real install: this environment
    cannot run paddleocr[doc-parser]/paddlex[ocr] (~GB-scale download,
    GPU/CPU wheels that don't fit here), so this class has not been
    exercised against
    actual model output. It uses `res.save_to_markdown(...)`, the
    approach documented in PaddleOCR's own quickstart, on the theory that
    it is the most version-stable public surface — but if a PaddleOCR
    release changes that API, this will need a small update. Run
    services/ppstructure_provider_smoketest.py after installing to check.

    supports_structured = True is deliberate: this provider does its own
    complete extraction (not an LLM given a prompt), and slots into
    InvoiceParser's existing vision-model code path unchanged — the
    `prompt`/`schema_hint` arguments below are accepted but unused.
    """

    name = "ppstructure"
    supports_structured = True
    _cached_pipeline = None

    def _engine(self):
        if PPStructureProvider._cached_pipeline is not None:
            return PPStructureProvider._cached_pipeline
        try:
            from paddleocr import PPStructureV3
        except ImportError as exc:
            raise OCRError(
                "PP-StructureV3 is not installed. Run: pip install "
                '"paddlepaddle==3.2.2" "paddleocr[doc-parser]" "paddlex[ocr]" '
                "(needs Python 3.8-3.12 — see INSTALL_OFFLINE_OCR.bat)."
            ) from exc
        log.info("Loading PP-StructureV3 pipeline (first use only)...")
        try:
            PPStructureProvider._cached_pipeline = PPStructureV3(
                device=self.config.get("device", "cpu"),
            )
        except Exception as exc:
            raise OCRError(_describe_ppstructure_failure(exc, "load")) from exc
        return PPStructureProvider._cached_pipeline

    def _page_markdown(self, engine, img: Image.Image) -> str:
        import shutil
        import tempfile

        import numpy as np

        workdir = tempfile.mkdtemp(prefix="ppstructure_")
        try:
            try:
                results = engine.predict(np.array(img.convert("RGB")))
            except Exception as exc:
                raise OCRError(_describe_ppstructure_failure(exc, "read this bill")) from exc
            chunks = []
            for index, res in enumerate(results):
                try:
                    res.save_to_markdown(save_path=workdir)
                except Exception as exc:
                    raise OCRError(
                        f"PP-StructureV3 returned a result object this version "
                        f"of the app doesn't know how to read (save_to_markdown "
                        f"failed: {exc}). The PaddleOCR API may have changed."
                    ) from exc
            for md_file in sorted(Path(workdir).glob("*.md")):
                chunks.append(md_file.read_text(encoding="utf-8", errors="ignore"))
            return "\n\n".join(chunks)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def extract_structured(self, images: List[Image.Image], prompt: str, schema_hint: str) -> OCRResponse:
        from services.ppstructure_normalize import markdown_to_invoice

        engine = self._engine()
        started = time.time()
        warnings: List[str] = []
        merged = None
        for index, img in enumerate(images, start=1):
            markdown = self._page_markdown(engine, img)
            data, notes = markdown_to_invoice(markdown)
            warnings.extend(f"Page {index}: {n}" for n in notes)
            if merged is None:
                merged = data
            else:
                merged["items"].extend(data["items"])
                # Later pages rarely repeat header fields; only fill gaps.
                if not merged["supplier"]["name"]:
                    merged["supplier"] = data["supplier"]
                if not merged["invoice"]["invoice_number"]:
                    merged["invoice"] = data["invoice"]
                if data["grand_total"]:
                    merged["grand_total"] = data["grand_total"]
                for key in ("subtotal", "discount_total", "tax_total", "cgst_total",
                            "sgst_total", "igst_total", "cess_total", "round_off"):
                    merged[key] = merged[key] or data[key]
        return OCRResponse(
            provider=self.name,
            model="pp-structurev3",
            structured=merged,
            duration_ms=int((time.time() - started) * 1000),
            warnings=warnings,
        )

    def health_check(self):
        try:
            self._engine()
            return True, "PP-StructureV3 loaded"
        except OCRError as exc:
            return False, str(exc)
        except Exception as exc:  # pragma: no cover - genuine SDK/runtime failure
            return False, f"PP-StructureV3 failed to load: {exc}"

    def extract_text(self, images: List[Image.Image]) -> OCRResponse:
        # Not the normal path (supports_structured=True routes callers to
        # extract_structured), but required by the OCRProvider contract and
        # harmless to support: same page markdown, tables flattened to text.
        engine = self._engine()
        started = time.time()
        text = "\n\n".join(self._page_markdown(engine, img) for img in images)
        return OCRResponse(
            provider=self.name, model="pp-structurev3", text=text,
            duration_ms=int((time.time() - started) * 1000),
        )


class HybridOCRProvider(OCRProvider):
    """
    "Auto" engine: try PP-StructureV3 first (free, offline, no API key).
    Only calls a paid AI vision engine when PP-StructureV3's own result
    looks unreliable — low confidence, no supplier name, no items, or the
    line totals don't add up anywhere near the printed grand total. This
    is the cost-control behaviour the integration brief asked for: don't
    spend API calls on a bill the offline engine already read fine.

    Same caveat as PPStructureProvider above: the PP-StructureV3 side of
    this has not been run against a real install from here.
    """

    name = "auto"
    supports_structured = True
    CONFIDENCE_FLOOR = 0.55
    TOTAL_MISMATCH_TOLERANCE = 0.05  # 5% - a looser pre-check than InvoiceValidator's

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self._fallback_name = self.config.get("fallback_provider") or "gemini"

    def _looks_reliable(self, data: Optional[dict]) -> bool:
        if not data:
            return False
        if (data.get("overall_confidence") or 0) < self.CONFIDENCE_FLOOR:
            return False
        if not (data.get("supplier") or {}).get("name"):
            return False
        items = data.get("items") or []
        if not items:
            return False
        grand_total = data.get("grand_total") or 0
        items_sum = sum((i.get("total") or 0) for i in items)
        if grand_total and abs(items_sum - grand_total) / grand_total > self.TOTAL_MISMATCH_TOLERANCE:
            return False
        return True

    def extract_structured(self, images: List[Image.Image], prompt: str, schema_hint: str) -> OCRResponse:
        primary_response, primary_error = None, None
        try:
            primary_response = PPStructureProvider(self.config).extract_structured(
                images, prompt, schema_hint
            )
        except OCRError as exc:
            primary_error = exc

        if primary_response and self._looks_reliable(primary_response.structured):
            primary_response.warnings = list(primary_response.warnings) + [
                "PP-StructureV3's result looked reliable — the paid AI vision "
                "engine was not called."
            ]
            return primary_response

        fallback_cls = PROVIDER_REGISTRY.get(self._fallback_name)
        fallback = fallback_cls(self.config) if fallback_cls else None
        if fallback is None or not fallback.supports_structured:
            if primary_response:
                return primary_response  # something is better than a hard failure
            raise OCRError(
                f"PP-StructureV3 failed ({primary_error}), and no usable AI vision "
                "engine is configured as a fallback. Set an API key in Settings -> "
                "AI / OCR."
            )

        reason = (
            "PP-StructureV3's result looked unreliable"
            if primary_response else f"PP-StructureV3 failed ({primary_error})"
        )
        fallback_response = fallback.extract_structured(images, prompt, schema_hint)
        fallback_response.warnings = list(fallback_response.warnings) + [
            f"{reason} — used "
            f"{PROVIDER_LABELS.get(self._fallback_name, self._fallback_name)} instead."
        ]
        return fallback_response

    def extract_text(self, images: List[Image.Image]) -> OCRResponse:
        raise OCRError("The Auto engine only supports structured extraction.")

    def health_check(self):
        ok, msg = PPStructureProvider(self.config).health_check()
        return True, (
            f"Auto mode ready. PP-StructureV3: {msg if ok else 'unavailable - ' + msg}. "
            f"Fallback engine: {PROVIDER_LABELS.get(self._fallback_name, self._fallback_name)}."
        )


class TesseractOCRProvider(OCRProvider):
    name = "tesseract"
    supports_structured = False

    def _engine(self):
        try:
            import pytesseract
        except ImportError as exc:
            raise OCRError(
                "pytesseract is not installed. Run: pip install pytesseract "
                "and install the Tesseract binary."
            ) from exc
        cmd = self.config.get("tesseract_cmd") or settings.TESSERACT_CMD
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        return pytesseract

    def extract_text(self, images: List[Image.Image]) -> OCRResponse:
        engine = self._engine()
        started = time.time()
        chunks = []
        multi_page = len(images) > 1
        for index, img in enumerate(images, start=1):
            try:
                prepared = _prepare_for_text_ocr(img)
                # psm 4 ("single column of variable-sized text") reads a
                # dense multi-column GST table more reliably than psm 6
                # ("single uniform block") - psm 6 was seen dropping a whole
                # item row outright next to a bill's header line on a tightly
                # packed table. Verified against every sample/demo bill this
                # app ships with plus real customer-reported bills: psm 4
                # never does worse, and recovers rows psm 6 silently lost.
                page_text = engine.image_to_string(prepared, config="--psm 4")
                # Only a real multi-page bill needs a page marker — on the
                # (overwhelmingly common) single-page bill, this literal
                # "--- PAGE 1 ---" text used to become the first line of the
                # OCR output and get picked up as the supplier name by the
                # heuristic parser below.
                chunks.append(f"--- PAGE {index} ---\n{page_text}" if multi_page else page_text)
            except Exception as exc:
                raise OCRError(f"Tesseract failed on page {index}: {exc}") from exc
        return OCRResponse(
            provider=self.name,
            model="tesseract",
            text="\n".join(chunks),
            duration_ms=int((time.time() - started) * 1000),
        )

    def health_check(self):
        try:
            engine = self._engine()
            version = engine.get_tesseract_version()
            return True, f"Tesseract {version}"
        except Exception as exc:
            return False, str(exc)


class RapidOCRProvider(OCRProvider):
    """Fast offline text OCR using RapidOCR (ONNXRuntime, not PaddlePaddle).

    Why this exists alongside Tesseract/PaddleOCR/PP-StructureV3: PP-StructureV3
    is table-aware but genuinely slow (its full layout+OCR+table pipeline, plus
    a first-run model download of 1-2 GB) - customers found this frustrating.
    RapidOCR runs the same family of PP-OCR text models through ONNXRuntime
    instead of the full PaddlePaddle framework, so there is no framework
    warm-up cost and no first-run download - its models ship inside the pip
    package. In testing this reads a bill in ~1-2 seconds on a plain CPU,
    versus PP-StructureV3's minutes.

    It is not a full table-structure model (that would be RapidTable, which
    we deliberately did NOT add - its model is not bundled and downloads from
    modelscope.cn on first use, which is unreachable from some networks/
    firewalls, i.e. exactly the "first run hangs" problem we already fixed
    for PP-StructureV3). Instead, `to_markdown()` reconstructs row/column
    layout from each word's bounding-box position, which is a reasonable
    approximation of a table for the simple, single-table GST bills this app
    deals with. That text is fed into the same `parse_text_heuristic()` /
    `LINE_RE` pipeline already used for Tesseract/PaddleOCR - same accuracy
    ceiling as those engines on messy text, but noticeably better column
    alignment on a clean table, and much faster.
    """

    name = "rapidocr"
    supports_structured = False
    _cached = None

    def _engine(self):
        if RapidOCRProvider._cached is not None:
            return RapidOCRProvider._cached
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise OCRError(
                "rapidocr is not installed. Run: pip install rapidocr"
            ) from exc
        RapidOCRProvider._cached = RapidOCR()
        return RapidOCRProvider._cached

    def extract_text(self, images: List[Image.Image]) -> OCRResponse:
        import numpy as np

        engine = self._engine()
        started = time.time()
        chunks = []
        multi_page = len(images) > 1
        for index, img in enumerate(images, start=1):
            try:
                prepared = _prepare_for_text_ocr(img)
                arr = np.array(prepared.convert("RGB"))
                result = engine(arr)
                page_text = result.to_markdown() if result.txts else ""
                chunks.append(f"--- PAGE {index} ---\n{page_text}" if multi_page else page_text)
            except Exception as exc:
                raise OCRError(f"RapidOCR failed on page {index}: {exc}") from exc
        return OCRResponse(
            provider=self.name,
            model="rapidocr",
            text="\n".join(chunks),
            duration_ms=int((time.time() - started) * 1000),
        )

    def health_check(self):
        try:
            self._engine()
            return True, "RapidOCR ready"
        except Exception as exc:
            return False, str(exc)


class PaddleOCRProvider(OCRProvider):
    name = "paddle"
    supports_structured = False
    _cached = None

    def _engine(self):
        if PaddleOCRProvider._cached is not None:
            return PaddleOCRProvider._cached
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise OCRError(
                "paddleocr is not installed. Run: pip install paddleocr paddlepaddle"
            ) from exc
        PaddleOCRProvider._cached = PaddleOCR(
            use_angle_cls=True, lang=self.config.get("lang", "en"), show_log=False
        )
        return PaddleOCRProvider._cached

    def extract_text(self, images: List[Image.Image]) -> OCRResponse:
        import numpy as np

        engine = self._engine()
        started = time.time()
        lines = []
        multi_page = len(images) > 1
        for index, img in enumerate(images, start=1):
            if multi_page:
                lines.append(f"--- PAGE {index} ---")
            prepared = _prepare_for_text_ocr(img)
            result = engine.ocr(np.array(prepared), cls=True)
            for block in result or []:
                for entry in block or []:
                    try:
                        lines.append(str(entry[1][0]))
                    except (IndexError, TypeError):
                        continue
        return OCRResponse(
            provider=self.name,
            model="paddleocr",
            text="\n".join(lines),
            duration_ms=int((time.time() - started) * 1000),
        )

    def health_check(self):
        try:
            self._engine()
            return True, "PaddleOCR ready"
        except Exception as exc:
            return False, str(exc)


# --------------------------------------------------------------------------
# Vision LLM providers (OCR + parsing in a single call)
# --------------------------------------------------------------------------
# HTTP statuses that mean "the server had a hiccup, try again" rather than
# "your request is wrong". Gemini in particular returns 503 (overloaded) and
# 429 (rate limit) fairly often, especially on the free tier — a single one of
# these used to fail the whole extraction and force the user to click again.
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)


def _post_with_retry(requests, url, max_attempts: int = 4, backoff: float = 2.0, **kwargs):
    """requests.post with a short retry+backoff for transient server errors.

    Waits 2s, 4s, 8s between attempts (or the server's Retry-After, capped at
    15s). Client mistakes (400/401/403/404) are returned immediately — retrying
    those would only waste time — and the caller's raise_for_status() reports
    them. After the last attempt the final response is returned as-is.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt == max_attempts:
                raise
            time.sleep(backoff * (2 ** (attempt - 1)))
            continue
        if resp.status_code in _RETRYABLE_STATUS and attempt < max_attempts:
            wait = backoff * (2 ** (attempt - 1))
            retry_after = resp.headers.get("Retry-After", "")
            if retry_after.isdigit():
                wait = min(float(retry_after), 15.0)
            log.warning(
                "OCR API returned %s (attempt %s/%s) - retrying in %.0fs",
                resp.status_code, attempt, max_attempts, wait,
            )
            time.sleep(wait)
            continue
        return resp
    raise last_exc  # pragma: no cover - loop always returns or raises


def _describe_request_failure(exc: Exception, resp, service: str, key: str) -> str:
    """Build the OCRError text for a failed vision-API call: never lets the
    API key leak into the message (Gemini puts it in the URL's query
    string, which requests.Response.raise_for_status() embeds verbatim in
    its exception text — that text used to reach the review screen as-is),
    and gives a plain-language explanation when the failure is the kind
    _post_with_retry already retried several times for (the service's own
    servers being overloaded/rate-limited) rather than a problem with the
    bill or the API key.
    """
    message = str(exc)
    if key:
        message = message.replace(key, "***")
    status = getattr(resp, "status_code", None)
    if status in _RETRYABLE_STATUS:
        return (
            f"{service}'s servers returned a {status} error after several "
            f"automatic retries — they are temporarily overloaded or "
            f"rate-limited, this is not a problem with your bill or API "
            f"key. Wait a minute and click Retry, or switch to an offline "
            f"engine (Settings → AI / OCR) meanwhile. Detail: {message}"
        )
    return f"{service} request failed: {message}"


class _HTTPVisionProvider(OCRProvider):
    """Shared plumbing for hosted vision APIs."""

    supports_structured = True
    requires_api_key = True
    api_key_setting = ""

    def api_key(self) -> str:
        return (self.config.get("api_key") or "").strip()

    def _requests(self):
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise OCRError("The 'requests' package is required.") from exc
        return requests

    def extract_text(self, images: List[Image.Image]) -> OCRResponse:
        response = self.extract_structured(
            images,
            "Transcribe every line of this document exactly as printed. "
            "Return plain text only, no commentary.",
            "",
        )
        return OCRResponse(
            provider=self.name,
            model=self.model_name,
            text=response.raw_response,
            duration_ms=response.duration_ms,
        )

    def health_check(self):
        if not self.api_key():
            return False, "API key not configured"
        return True, f"{self.name} key present ({self.model_name})"


class GeminiVisionProvider(_HTTPVisionProvider):
    name = "gemini"

    @property
    def model_name(self) -> str:
        return self.config.get("model") or settings.GEMINI_MODEL

    def extract_structured(self, images, prompt, schema_hint) -> OCRResponse:
        requests = self._requests()
        key = self.api_key()
        if not key:
            raise OCRError("Gemini API key is not configured.")
        parts: list = [{"text": f"{prompt}\n\n{schema_hint}"}]
        for img in images[:8]:
            b64, media = to_base64(img, "jpeg")
            parts.append({"inline_data": {"mime_type": media, "data": b64}})

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model_name}:generateContent"
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
            resp = _post_with_retry(
                requests,
                url,
                params={"key": key},
                json=payload,
                timeout=settings.OCR_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            body = resp.json()
            text = body["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            raise OCRError(_describe_request_failure(exc, resp, "Gemini", key)) from exc
        return OCRResponse(
            provider=self.name,
            model=self.model_name,
            raw_response=text,
            duration_ms=int((time.time() - started) * 1000),
        )


class OpenAIVisionProvider(_HTTPVisionProvider):
    name = "openai"

    @property
    def model_name(self) -> str:
        return self.config.get("model") or settings.OPENAI_MODEL

    def extract_structured(self, images, prompt, schema_hint) -> OCRResponse:
        requests = self._requests()
        key = self.api_key()
        if not key:
            raise OCRError("OpenAI API key is not configured.")
        content: list = [{"type": "text", "text": f"{prompt}\n\n{schema_hint}"}]
        for img in images[:8]:
            b64, media = to_base64(img, "jpeg")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{b64}"},
                }
            )
        payload = {
            "model": self.model_name,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 8000,
        }
        started = time.time()
        resp = None
        try:
            resp = _post_with_retry(
                requests,
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=settings.OCR_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            raise OCRError(_describe_request_failure(exc, resp, "OpenAI", key)) from exc
        return OCRResponse(
            provider=self.name,
            model=self.model_name,
            raw_response=text,
            duration_ms=int((time.time() - started) * 1000),
        )


class ClaudeVisionProvider(_HTTPVisionProvider):
    name = "claude"

    @property
    def model_name(self) -> str:
        return self.config.get("model") or settings.ANTHROPIC_MODEL

    def extract_structured(self, images, prompt, schema_hint) -> OCRResponse:
        requests = self._requests()
        key = self.api_key()
        if not key:
            raise OCRError("Anthropic API key is not configured.")
        content: list = []
        for img in images[:8]:
            b64, media = to_base64(img, "jpeg")
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media,
                        "data": b64,
                    },
                }
            )
        content.append({"type": "text", "text": f"{prompt}\n\n{schema_hint}"})
        payload = {
            "model": self.model_name,
            "max_tokens": 8000,
            "temperature": 0,
            "messages": [{"role": "user", "content": content}],
        }
        started = time.time()
        resp = None
        try:
            resp = _post_with_retry(
                requests,
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=settings.OCR_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            blocks = resp.json().get("content", [])
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except Exception as exc:
            raise OCRError(_describe_request_failure(exc, resp, "Anthropic", key)) from exc
        return OCRResponse(
            provider=self.name,
            model=self.model_name,
            raw_response=text,
            duration_ms=int((time.time() - started) * 1000),
        )


class ProxyVisionProvider(_HTTPVisionProvider):
    """Pay-per-use AI vision engine via SN Softech's own cloud proxy.

    Unlike Gemini/OpenAI/Claude above, this never holds a raw provider API
    key on the customer's machine. Instead the "API key" field here is the
    customer's own SN Softech licence key (the one they activated with) -
    the proxy (see ocr_proxy/) checks that key's signature, applies a daily
    cap, and only then spends the vendor's real Gemini key on their behalf.
    If the key is stolen off this machine, it only grants the proxy's
    OCR endpoint (rate-limited) - never the underlying paid API key itself.
    """

    name = "proxy"
    api_key_setting = "licence key"

    @property
    def model_name(self) -> str:
        return "sn-cloud-ai"

    def _base_url(self) -> str:
        return (self.config.get("proxy_url") or settings.OCR_PROXY_URL or "").strip()

    def extract_structured(self, images, prompt, schema_hint) -> OCRResponse:
        requests = self._requests()
        key = self.api_key()
        if not key:
            raise OCRError(
                "Enter your SN Softech licence key (Settings -> AI / OCR) to "
                "use this engine."
            )
        base_url = self._base_url()
        if not base_url:
            raise OCRError(
                "This installation has no cloud AI proxy configured "
                "(OCR_PROXY_URL is empty). Contact SN Softech Solutions."
            )
        images_payload = []
        for img in images[:8]:
            b64, media = to_base64(img, "jpeg")
            images_payload.append({"mime_type": media, "data": b64})
        payload = {
            "license_key": key,
            "prompt": prompt,
            "schema_hint": schema_hint,
            "images": images_payload,
        }
        started = time.time()
        resp = None
        try:
            resp = _post_with_retry(
                requests,
                f"{base_url.rstrip('/')}/v1/extract",
                json=payload,
                timeout=settings.OCR_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            body = resp.json()
            text = body["text"]
        except Exception as exc:
            raise OCRError(
                _describe_request_failure(exc, resp, "SN Softech Cloud AI", key)
            ) from exc
        return OCRResponse(
            provider=self.name,
            model=body.get("model", self.model_name) if resp is not None else self.model_name,
            raw_response=text,
            duration_ms=int((time.time() - started) * 1000),
        )

    def health_check(self):
        if not self._base_url():
            return False, "Cloud AI proxy is not configured (OCR_PROXY_URL is empty)."
        if not self.api_key():
            return False, "Enter your SN Softech licence key."
        requests = self._requests()
        try:
            resp = requests.get(f"{self._base_url().rstrip('/')}/health", timeout=10)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            return False, f"Could not reach the cloud AI proxy: {exc}"
        if not body.get("ok"):
            return False, "Cloud AI proxy reports it is not ready."
        return True, "SN Softech Cloud AI ready (uses your licence key)."


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
PROVIDER_REGISTRY: Dict[str, Type[OCRProvider]] = {
    "mock": MockOCRProvider,
    "rapidocr": RapidOCRProvider,
    "tesseract": TesseractOCRProvider,
    "paddle": PaddleOCRProvider,
    "ppstructure": PPStructureProvider,
    "auto": HybridOCRProvider,
    "gemini": GeminiVisionProvider,
    "openai": OpenAIVisionProvider,
    "claude": ClaudeVisionProvider,
    "proxy": ProxyVisionProvider,
}

PROVIDER_LABELS = {
    "mock": "Mock / Demo (no API, sample data)",
    "rapidocr": "RapidOCR (offline, fast - default)",
    "tesseract": "Tesseract OCR (offline, fallback)",
    "paddle": "PaddleOCR (offline)",
    "ppstructure": "PP-StructureV3 (offline, table-aware, slow)",
    "auto": "Auto / Hybrid (PP-StructureV3, AI engine as fallback)",
    "gemini": "Google Gemini Vision",
    "openai": "OpenAI Vision",
    "claude": "Anthropic Claude Vision",
    "proxy": "SN Softech Cloud AI (pay-per-use, no API key setup)",
}


def register_provider(name: str, cls: Type[OCRProvider]) -> None:
    """Public hook so a customer-specific OCR engine can be plugged in."""
    PROVIDER_REGISTRY[name.lower()] = cls


def get_provider(name: Optional[str] = None, config: Optional[Dict] = None) -> OCRProvider:
    key = (name or settings.OCR_PROVIDER or "mock").strip().lower()
    cls = PROVIDER_REGISTRY.get(key)
    if cls is None:
        log.warning("Unknown OCR provider '%s'; falling back to mock", key)
        cls = MockOCRProvider
    return cls(config or {})


def available_providers() -> List[str]:
    return list(PROVIDER_REGISTRY.keys())
