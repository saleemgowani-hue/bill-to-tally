"""
Image handling for bill photos.

Pillow is required. OpenCV (perspective correction, denoise) and PyMuPDF
(PDF -> page images) are optional; the module degrades gracefully without them.
"""
from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from utils.logging import get_logger

log = get_logger("image")

try:  # optional
    import cv2
    import numpy as np

    HAS_CV2 = True
except ImportError:  # pragma: no cover
    HAS_CV2 = False

try:  # optional
    import pymupdf  # PyMuPDF >= 1.24 exposes `pymupdf`

    HAS_PDF = True
except ImportError:  # pragma: no cover
    try:
        import fitz as pymupdf  # older PyMuPDF

        HAS_PDF = True
    except ImportError:
        HAS_PDF = False

MAX_EDGE = 2200  # keep vision-API payloads reasonable


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def pdf_to_images(data: bytes, dpi: int = 200, max_pages: int = 20) -> List[Image.Image]:
    """Render each PDF page to an image. Returns [] if PyMuPDF is unavailable."""
    if not HAS_PDF:
        log.warning("PyMuPDF not installed; cannot rasterise PDF invoices")
        return []
    pages: List[Image.Image] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for index, page in enumerate(doc):
            if index >= max_pages:
                break
            zoom = dpi / 72
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            pages.append(
                Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            )
    return pages


def to_images(filename: str, data: bytes) -> List[Image.Image]:
    """Turn any supported upload into a list of page images."""
    if filename.lower().endswith(".pdf") or data[:4] == b"%PDF":
        return pdf_to_images(data)
    return [load_image(data)]


def downscale(img: Image.Image, max_edge: int = MAX_EDGE) -> Image.Image:
    w, h = img.size
    longest = max(w, h)
    if longest <= max_edge:
        return img
    scale = max_edge / longest
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def rotate(img: Image.Image, degrees: int) -> Image.Image:
    if degrees % 360 == 0:
        return img
    return img.rotate(-degrees, expand=True, fillcolor=(255, 255, 255))


def crop_percent(
    img: Image.Image, left: int, top: int, right: int, bottom: int
) -> Image.Image:
    """Crop using percentage margins (0-100) from each edge."""
    w, h = img.size
    box = (
        int(w * left / 100),
        int(h * top / 100),
        int(w * (100 - right) / 100),
        int(h * (100 - bottom) / 100),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return img
    return img.crop(box)


def enhance(
    img: Image.Image,
    contrast: float = 1.0,
    sharpness: float = 1.0,
    brightness: float = 1.0,
    grayscale: bool = False,
    denoise: bool = False,
) -> Image.Image:
    out = img
    if brightness != 1.0:
        out = ImageEnhance.Brightness(out).enhance(brightness)
    if contrast != 1.0:
        out = ImageEnhance.Contrast(out).enhance(contrast)
    if sharpness != 1.0:
        out = ImageEnhance.Sharpness(out).enhance(sharpness)
    if denoise:
        if HAS_CV2:
            arr = np.array(out)
            arr = cv2.fastNlMeansDenoisingColored(arr, None, 7, 7, 7, 21)
            out = Image.fromarray(arr)
        else:
            out = out.filter(ImageFilter.MedianFilter(size=3))
    if grayscale:
        out = ImageOps.grayscale(out).convert("RGB")
    return out


def auto_deskew(img: Image.Image) -> Image.Image:
    """Straighten a mildly rotated document scan. No-op without OpenCV."""
    if not HAS_CV2:
        return img
    try:
        arr = np.array(ImageOps.grayscale(img))
        edges = cv2.Canny(arr, 60, 180)
        lines = cv2.HoughLines(edges, 1, np.pi / 180, 200)
        if lines is None:
            return img
        angles = []
        for line in lines[:60]:
            theta = line[0][1]
            deg = np.degrees(theta) - 90
            if -20 < deg < 20:
                angles.append(deg)
        if not angles:
            return img
        angle = float(np.median(angles))
        if abs(angle) < 0.4:
            return img
        return img.rotate(angle, expand=True, fillcolor=(255, 255, 255))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Deskew failed: %s", exc)
        return img


def perspective_correct(img: Image.Image) -> Image.Image:
    """
    Detect the largest quadrilateral (the paper) and flatten it.
    Returns the original image when no confident page outline is found.
    """
    if not HAS_CV2:
        return img
    try:
        arr = np.array(img)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(gray, 50, 150)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(
            edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            return img
        page_area = arr.shape[0] * arr.shape[1]
        best = max(contours, key=cv2.contourArea)
        if cv2.contourArea(best) < page_area * 0.35:
            return img
        peri = cv2.arcLength(best, True)
        approx = cv2.approxPolyDP(best, 0.02 * peri, True)
        if len(approx) != 4:
            return img

        pts = approx.reshape(4, 2).astype("float32")
        s = pts.sum(axis=1)
        diff = np.diff(pts, axis=1)
        ordered = np.array(
            [
                pts[np.argmin(s)],      # top-left
                pts[np.argmin(diff)],   # top-right
                pts[np.argmax(s)],      # bottom-right
                pts[np.argmax(diff)],   # bottom-left
            ],
            dtype="float32",
        )
        (tl, tr, br, bl) = ordered
        width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
        height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
        if width < 200 or height < 200:
            return img
        dst = np.array(
            [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
            dtype="float32",
        )
        matrix = cv2.getPerspectiveTransform(ordered, dst)
        warped = cv2.warpPerspective(arr, matrix, (width, height))
        return Image.fromarray(warped)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Perspective correction failed: %s", exc)
        return img


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    downscale(img).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def to_jpeg_bytes(img: Image.Image, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    downscale(img).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def to_base64(img: Image.Image, fmt: str = "jpeg") -> Tuple[str, str]:
    """Return (base64_string, media_type)."""
    if fmt == "png":
        return base64.b64encode(to_png_bytes(img)).decode(), "image/png"
    return base64.b64encode(to_jpeg_bytes(img)).decode(), "image/jpeg"


def save_image(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    downscale(img).save(path, format="JPEG", quality=88)
    return path


def read_barcodes(img: Image.Image) -> List[str]:
    """
    Best-effort barcode read (EAN/UPC/GTIN).
    Uses pyzbar if installed, else OpenCV's barcode detector, else [].
    """
    codes: List[str] = []
    try:
        from pyzbar import pyzbar  # type: ignore

        for res in pyzbar.decode(img):
            value = res.data.decode("utf-8", "ignore").strip()
            if value:
                codes.append(value)
        if codes:
            return list(dict.fromkeys(codes))
    except Exception:
        pass

    if HAS_CV2:
        try:
            detector = cv2.barcode.BarcodeDetector()
            ok, decoded, _types, _pts = detector.detectAndDecodeWithType(
                np.array(ImageOps.grayscale(img))
            )
            if ok:
                codes = [c.strip() for c in decoded if c and c.strip()]
        except Exception:
            pass
    return list(dict.fromkeys(codes))


def build_preview(
    img: Image.Image,
    rotation: int = 0,
    crop: Optional[Tuple[int, int, int, int]] = None,
    contrast: float = 1.0,
    sharpness: float = 1.0,
    brightness: float = 1.0,
    grayscale: bool = False,
    denoise: bool = False,
    deskew: bool = False,
    perspective: bool = False,
) -> Image.Image:
    """Apply the full editing pipeline in a predictable order."""
    out = img
    if perspective:
        out = perspective_correct(out)
    if rotation:
        out = rotate(out, rotation)
    if crop:
        out = crop_percent(out, *crop)
    if deskew:
        out = auto_deskew(out)
    out = enhance(out, contrast, sharpness, brightness, grayscale, denoise)
    return downscale(out)


def perceptual_fingerprint(image: Image.Image, width: int = 17, height: int = 16) -> str:
    """A difference-hash of the image, as hex (256 bits by default).

    Used by the mock OCR provider to tell one sample bill from another
    without an API key. It must survive the resizing and JPEG compression the
    upload pipeline applies, so a byte hash is no good.

    A plain average-hash is useless here: a bill is mostly white paper, so the
    average sits near white and almost every cell reads the same. This compares
    each cell with its right-hand neighbour instead, which captures where the
    ink actually falls — the layout — and separates two different bills clearly.
    """
    small = image.convert("L").resize((width, height), Image.LANCZOS)
    pixels = list(small.tobytes())
    bits = []
    for row in range(height):
        offset = row * width
        for column in range(width - 1):
            bits.append("1" if pixels[offset + column] > pixels[offset + column + 1] else "0")
    value = int("".join(bits), 2)
    return f"{value:0{len(bits) // 4}x}"


def fingerprint_distance(first: str, second: str) -> int:
    """Hamming distance between two fingerprints. 0 means identical."""
    try:
        return bin(int(first, 16) ^ int(second, 16)).count("1")
    except (TypeError, ValueError):
        return 256
