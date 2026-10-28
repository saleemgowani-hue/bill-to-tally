"""Locks in the fixes that make text-only OCR (Tesseract/PaddleOCR) usable
on real photographed bills: pre-OCR image preparation and a more tolerant
item-line pattern."""
from PIL import Image

from services.ai_invoice_parser import LINE_RE, parse_text_heuristic
from services.ocr_service import _prepare_for_text_ocr


def test_line_re_still_matches_clean_line():
    m = LINE_RE.match("Rice 25kg 10 1200 12000")
    assert m and m.group("name") == "Rice 25kg"
    assert m.group("qty") == "10" and m.group("total") == "12000"


def test_line_re_tolerates_rs_prefix_and_gst_percent():
    m = LINE_RE.match("Refined Oil 15L 5 Rs.1800 18% 9450")
    assert m
    assert m.group("rate") == "1800"
    assert m.group("total") == "9450"


def test_line_re_tolerates_discount_and_gst_percent():
    m = LINE_RE.match("Sugar 50kg 4 2000 100 5% 7900")
    assert m
    assert m.group("disc") == "100"
    assert m.group("total") == "7900"


def test_heuristic_extracts_supplier_invoice_and_items_from_messy_text():
    text = (
        "ABC Traders\nGSTIN 23ABCDE1234F1Z5\nInvoice No: INV-101\n"
        "Invoice Date: 01-09-2026\nRice 25kg 10 1200 12000\n"
        "Refined Oil 15L 5 Rs.1800 18% 9450\nGrand Total: Rs. 21450\n"
    )
    data = parse_text_heuristic(text)
    assert data["supplier"]["name"] == "ABC Traders"
    assert data["invoice"]["invoice_number"] == "INV-101"
    assert len(data["items"]) == 2
    assert data["grand_total"] == 21450.0


def test_prepare_for_text_ocr_returns_an_image_and_never_raises():
    img = Image.new("RGB", (200, 120), "white")
    out = _prepare_for_text_ocr(img)
    assert isinstance(out, Image.Image)
    assert out.size[0] > 0 and out.size[1] > 0


# A real bill (SHREE BALAJI TRADERS, SBT/2026/0412) whose table prints HSN
# BEFORE Qty — a near-universal GST invoice layout — and whose GST% column
# has no literal "%" on each row (only in the header). Before this fix,
# LINE_RE expected at most qty/rate/[disc]/[gst%] between name and total, so
# the extra HSN token broke every line's match: 0 items were ever extracted
# from a Tesseract/PaddleOCR reading of this exact, cleanly printed bill.
def test_line_re_handles_hsn_before_qty_and_bare_gst_percent():
    m = LINE_RE.match("Parle-G Gold Biscuit 800 gm 19053100 24 95.00 0.00 18 2,280.00")
    assert m
    assert m.group("name") == "Parle-G Gold Biscuit 800 gm"
    assert m.group("hsn") == "19053100"
    assert m.group("qty") == "24"
    assert m.group("rate") == "95.00"
    assert m.group("disc") == "0.00"
    assert m.group("gst") == "18"
    assert m.group("total") == "2,280.00"


def test_heuristic_reads_hsn_column_bills_end_to_end():
    text = (
        "SHREE BALAJI TRADERS\nGSTIN: 22ABCDE1234F1Z5\nInvoice No: SBT/2026/0412\n"
        "Invoice Date: 01/09/2026\n"
        "Parle-G Gold Biscuit 800 gm 19053100 24 95.00 0.00 18 2,280.00\n"
        "Tata Salt 1 kg 25010020 50 22.00 0.00 5 1,100.00\n"
        "Fortune Sunflower Oil 1 L 15121110 20 145.00 0.00 5 2,900.00\n"
        "GRAND TOTAL: 6,890.40\n"
    )
    data = parse_text_heuristic(text)
    assert data["supplier"]["name"] == "SHREE BALAJI TRADERS"
    assert len(data["items"]) == 3
    biscuit = data["items"][0]
    assert biscuit["hsn"] == "19053100"
    assert biscuit["gst_rate"] == 18.0
    assert biscuit["total"] == 2280.0


# The literal "--- PAGE 1 ---" marker text-only engines used to insert even
# for a single-page bill was itself the first line of OCR output, and the
# supplier-name heuristic (first non-empty, non-"tax/invoice/gst" line)
# picked it up as the supplier — exactly what a real bill (SBT/2026/0412)
# showed: Supplier field filled with "--- PAGE 1 ---" instead of the real
# name underneath it.
def test_page_marker_line_is_never_picked_as_the_supplier_name():
    text = "--- PAGE 1 ---\nSHREE BALAJI TRADERS\nInvoice No: SBT/2026/0412\n"
    data = parse_text_heuristic(text)
    assert data["supplier"]["name"] == "SHREE BALAJI TRADERS"


def test_tesseract_omits_the_page_marker_for_a_single_page_bill():
    from services.ocr_service import TesseractOCRProvider

    class _FakeEngine:
        def image_to_string(self, img, config=""):
            return "SHREE BALAJI TRADERS\nInvoice No: SBT/2026/0412\n"

    provider = TesseractOCRProvider()
    provider._engine = lambda: _FakeEngine()
    result = provider.extract_text([Image.new("RGB", (10, 10))])
    assert "PAGE" not in result.text
    assert result.text.startswith("SHREE BALAJI TRADERS")


def test_tesseract_keeps_the_page_marker_for_a_real_multi_page_bill():
    from services.ocr_service import TesseractOCRProvider

    class _FakeEngine:
        def __init__(self):
            self.calls = 0

        def image_to_string(self, img, config=""):
            self.calls += 1
            return f"Page {self.calls} text\n"

    provider = TesseractOCRProvider()
    provider._engine = lambda: _FakeEngine()
    result = provider.extract_text([Image.new("RGB", (10, 10))] * 2)
    assert "--- PAGE 1 ---" in result.text
    assert "--- PAGE 2 ---" in result.text


# A real customer bill (SHREE KRISHNA GENERAL STORE, SKG/2026/0881) whose
# table has separate Taxable / CGST% / CGST / SGST% / SGST columns packed
# tightly together. RapidOCR's (and Tesseract's) text detector glued two
# adjacent narrow columns into one token on this layout - e.g. HSN "10063020"
# + Qty "6" read as one "100630206", or Rate "1450.00" + Taxable "8700.00"
# read as "1450.008700.00" - and LINE_RE still "matched" the garbled line,
# picking the wrong tokens for qty/rate and silently posting a wrong amount
# (calculated total came to Rs 2,715.75 against a real invoice total of
# Rs 17,203.00). A line's total must be ~qty*rate (minus discount, plus at
# most ~28% GST) - anything further off means the columns were glued, not
# that GST was added, so the line must be dropped instead of guessed at.
def test_glued_columns_are_rejected_instead_of_posted_as_a_wrong_amount():
    # Columns merged exactly as RapidOCR's small detector model merged them on
    # the real bill: "HSN+Qty" and "Rate+Taxable" fused with no separating
    # space, "217.5" standing in for one GST-amount column and "2.50" for the
    # other, total garbled down to "326.25".
    line = "Basmati Rice 25kg 100630206 1450.008700.00 2.5 217.5 2.50 326.25"
    data = parse_text_heuristic(f"ABC Traders\n{line}\nGrand Total: 9135.00\n")
    assert data["items"] == []


def test_a_genuinely_high_gst_line_still_passes():
    # 28% GST (the top slab) inflates total well above qty*rate - must not be
    # mistaken for a glued-column line.
    line = "Luxury Item 2 500 28% 1280"  # 2*500=1000, +28% = 1280
    data = parse_text_heuristic(f"ABC Traders\n{line}\nGrand Total: 1280\n")
    assert len(data["items"]) == 1
    assert data["items"][0]["total"] == 1280.0


# The SHREE KRISHNA GENERAL STORE bill above (SKG/2026/0881) uses the OTHER
# very common Indian GST layout: a separate Taxable-value column, plus
# CGST%/CGST and SGST%/SGST printed as their own columns (intra-state),
# rather than one combined GST% + total. Even once RapidOCR/Tesseract read
# every column cleanly and separately (no gluing at all), LINE_RE could not
# read this shape - it only has two optional slots (discount, one gst%)
# between rate and total, but this layout has five fields in between. The
# line used to silently disappear (0 items) even on a perfect OCR reading.
def test_reads_a_cleanly_separated_cgst_sgst_split_line():
    line = "Basmati Rice 25kg 10063020 6 1450.00 8700.00 2.5 217.50 2.5 217.50 9135.00"
    data = parse_text_heuristic(f"SHREE KRISHNA GENERAL STORE\n{line}\nGrand Total: 9135.00\n")
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["raw_name"] == "Basmati Rice 25kg"
    assert item["hsn"] == "10063020"
    assert item["quantity"] == 6.0
    assert item["rate"] == 1450.0
    assert item["taxable_value"] == 8700.0
    assert item["cgst"] == 217.50
    assert item["sgst"] == 217.50
    assert item["gst_rate"] == 5.0  # 2.5 + 2.5
    assert item["total"] == 9135.0


def test_reads_a_cleanly_separated_igst_split_line():
    line = "LED Bulb 9W 85395090 15 420.00 6300.00 18 1134.00 7434.00"
    data = parse_text_heuristic(f"SHIVAM ELECTRICALS\n{line}\nGrand Total: 7434.00\n")
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["igst"] == 1134.0
    assert item["cgst"] == 0.0 and item["sgst"] == 0.0
    assert item["gst_rate"] == 18.0
    assert item["total"] == 7434.0


def test_reads_a_cgst_sgst_split_line_with_a_discount_column():
    line = "Denim Jeans 62034200 20 650.00 500.00 12500.00 2.5 312.50 2.5 312.50 13125.00"
    data = parse_text_heuristic(f"GANESH TEXTILES\n{line}\nGrand Total: 13125.00\n")
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["discount"] == 500.0
    assert item["taxable_value"] == 12500.0
    assert item["total"] == 13125.0


def test_split_tax_line_is_rejected_when_taxable_does_not_match_qty_times_rate():
    # A still-garbled line (e.g. a glued digit) must not be guessed at here
    # either - same safety principle as the LINE_RE arithmetic check above.
    line = "Mystery Item 12345678 6 1450.00 9999.00 2.5 217.50 2.5 217.50 9135.00"
    data = parse_text_heuristic(f"ABC Traders\n{line}\nGrand Total: 9135.00\n")
    assert data["items"] == []


# A real customer bill (SHIVAM ELECTRICALS, SEH/2026/1142): Tesseract read a
# 5-digit Taxable value "10000.00" as "10000 .00" - a stray space right next
# to the decimal point. That torn-apart amount no longer looked like a number
# at all, so the whole line (a perfectly good "Copper Wire" row) silently
# disappeared - 2 items showed on a 3-item bill with no error explaining why.
def test_a_decimal_point_split_by_a_stray_space_is_rejoined():
    line = "Copper Wire 1.5mm 90m 85444920 8 1250.00 10000 .00 18 1800.00 11800.00"
    data = parse_text_heuristic(f"SHIVAM ELECTRICALS\n{line}\nGrand Total: 11800.00\n")
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["taxable_value"] == 10000.0
    assert item["total"] == 11800.0


def test_a_decimal_point_split_with_space_after_the_dot_is_also_rejoined():
    line = "Bulk Item 10001234 10 2000.00 20000. 00 12 2400.00 22400.00"
    data = parse_text_heuristic(f"ABC Traders\n{line}\nGrand Total: 22400.00\n")
    assert len(data["items"]) == 1
    assert data["items"][0]["taxable_value"] == 20000.0


def test_split_decimal_fix_does_not_touch_a_bare_gst_percent():
    # Must not accidentally glue "2" and ".5" (a one-decimal-digit GST%) into
    # something else - the fix only ever touches a TWO-digit decimal tail.
    from services.ai_invoice_parser import _SPLIT_DECIMAL_RE
    assert _SPLIT_DECIMAL_RE.sub(r"\1.\2", "2 .5") == "2 .5"
