"""RapidOCR is the fast offline engine added after PP-StructureV3 proved too
slow for real use — see services/ocr_service.py's RapidOCRProvider docstring
for why RapidTable was deliberately NOT used (its model is not bundled and
downloads from modelscope.cn on first use, which is unreachable on some
networks — exactly the "first run hangs" problem already fixed for
PP-StructureV3). These tests use a fake engine so they run without the real
`rapidocr` package installed, the same pattern used for the Tesseract tests
in test_text_ocr_improvements.py.
"""
from PIL import Image

from services.ai_invoice_parser import parse_text_heuristic
from services.ocr_service import PROVIDER_LABELS, PROVIDER_REGISTRY, RapidOCRProvider


class _FakeRapidOCRResult:
    def __init__(self, markdown: str, txts=("x",)):
        self._markdown = markdown
        self.txts = txts

    def to_markdown(self) -> str:
        return self._markdown


class _FakeRapidOCREngine:
    """Records how many times it was called and replays scripted results."""

    def __init__(self, markdowns):
        self.markdowns = list(markdowns)
        self.calls = 0

    def __call__(self, arr):
        self.calls += 1
        return _FakeRapidOCRResult(self.markdowns[self.calls - 1])


def test_registered_under_rapidocr_with_a_label():
    assert PROVIDER_REGISTRY["rapidocr"] is RapidOCRProvider
    assert "rapidocr" in PROVIDER_LABELS


def test_extract_text_returns_the_markdown_reconstruction():
    provider = RapidOCRProvider()
    fake = _FakeRapidOCREngine(["SHREE KRISHNA GENERAL STORE\nInvoice No: SKG/2026/0881\n"])
    provider._engine = lambda: fake
    result = provider.extract_text([Image.new("RGB", (10, 10))])
    assert result.provider == "rapidocr"
    assert "SHREE KRISHNA GENERAL STORE" in result.text
    assert fake.calls == 1


def test_single_page_bill_gets_no_page_marker():
    """Same fix as Tesseract/PaddleOCR: a page marker on a single-page bill
    used to get picked up as the supplier name by the heuristic parser."""
    provider = RapidOCRProvider()
    fake = _FakeRapidOCREngine(["SHREE KRISHNA GENERAL STORE\nInvoice No: SKG/2026/0881\n"])
    provider._engine = lambda: fake
    result = provider.extract_text([Image.new("RGB", (10, 10))])
    assert "PAGE" not in result.text
    data = parse_text_heuristic(result.text)
    assert data["supplier"]["name"] == "SHREE KRISHNA GENERAL STORE"


def test_multi_page_bill_keeps_page_markers():
    provider = RapidOCRProvider()
    fake = _FakeRapidOCREngine(["Page one text\n", "Page two text\n"])
    provider._engine = lambda: fake
    result = provider.extract_text([Image.new("RGB", (10, 10))] * 2)
    assert "--- PAGE 1 ---" in result.text
    assert "--- PAGE 2 ---" in result.text
    assert fake.calls == 2


def test_engine_is_cached_across_calls():
    """Model loading is the slow part (~1s) — must happen once per process,
    not once per bill."""
    RapidOCRProvider._cached = "sentinel"
    try:
        provider = RapidOCRProvider()
        assert provider._engine() == "sentinel"
    finally:
        RapidOCRProvider._cached = None


def test_a_real_rapidocr_markdown_table_reads_end_to_end():
    """Shaped like RapidOCR's actual to_markdown() output (verified by hand
    against the real package) — word positions reconstruct columns well
    enough for the existing LINE_RE heuristic to read it."""
    markdown = (
        "SHREE KRISHNA GENERAL STORE\n"
        "Invoice No: SKG/2026/0881\n"
        "Invoice Date: 15/09/2026\n\n"
        "Item         HSN   Qty Rate  GST% Total\n"
        "Basmati Rice 25kg 10063020   6   1450.00  5%  9135.00\n"
        "Toor Dal 1kg  07136000 30  140.00  5%  4410.00\n\n"
        "Taxable Value:      7,100.00\n"
        "CGST:            177.50\n"
        "SGST:            177.50\n"
        "GRAND TOTAL:       7,455.00\n"
    )
    provider = RapidOCRProvider()
    provider._engine = lambda: _FakeRapidOCREngine([markdown])
    result = provider.extract_text([Image.new("RGB", (10, 10))])
    data = parse_text_heuristic(result.text)
    assert data["supplier"]["name"] == "SHREE KRISHNA GENERAL STORE"
    assert len(data["items"]) == 2
    assert data["items"][0]["total"] == 9135.0
    assert data["items"][1]["total"] == 4410.0
