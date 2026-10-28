"""
Builds the demo purchase bills used for showing the app without an API key.

    python samples/make_sample_bills.py

Produces, in samples/:
  demo_bill_1_balaji.png     ... one PNG per bill
  sample_bills.json          ... the data the mock OCR provider returns

Every bill is dated 01-09-2026 on purpose: Tally's educational mode only
accepts vouchers on the 1st, 2nd or 31st of a month, so these import on an
unlicensed Tally as well as a licensed one.

The mock provider matches an uploaded image to its data with a perceptual
fingerprint, so four different bills really do extract as four different
suppliers. Re-run this script if you change a bill — the fingerprints are
stored in the JSON and must match the images.
"""
import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.image_tools import perceptual_fingerprint  # noqa: E402

OUT = ROOT / "samples"
DATE_TEXT = "01/09/2026"
DATE_ISO = "2026-09-01"


def font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# The bills. Numbers are exact so the validator reconciles them cleanly.
# ---------------------------------------------------------------------------
BILLS = [
    {
        "file": "demo_bill_1_balaji.png",
        "colour": "#0b4a6f",
        "supplier": {
            "name": "Shree Balaji Traders",
            "gstin": "22ABCDE1234F1Z5",
            "address": "142 Malviya Road, Raipur, Chhattisgarh - 492001",
            "phone": "0771-2234567",
        },
        "invoice": {"invoice_number": "SBT/2026/0412", "invoice_date": DATE_ISO,
                    "due_date": "2026-10-01"},
        "scope": "INTRA",
        "style": "banded",
        "items": [
            # name, hsn, qty, unit, rate, discount, gst%, confidence, barcode
            ("Parle-G Gold Biscuit 800 gm", "19053100", 24, "PCS", 95.00, 0.00, 18, 0.97,
             "8901719101234"),
            ("Tata Salt 1 kg", "25010020", 50, "PCS", 22.00, 0.00, 5, 0.96, ""),
            ("Fortune Sunflower Oil 1 L", "15121110", 20, "LTR", 145.00, 0.00, 5, 0.95, ""),
        ],
        "round_off": 0.00,
        "note": "Clean bill — everything should match your product master.",
    },
    {
        "file": "demo_bill_2_sharda.png",
        "colour": "#6941c6",
        "supplier": {
            "name": "Maa Sharda Agencies",
            "gstin": "22FGHIJ5678K1Z9",
            "address": "Shop 7, Supela Market, Bhilai, Chhattisgarh - 490023",
            "phone": "0788-2456789",
        },
        "invoice": {"invoice_number": "MSA/26-27/0138", "invoice_date": DATE_ISO,
                    "due_date": "2026-09-30"},
        "scope": "INTRA",
        "items": [
            ("Amul Butter 500 gm", "04051000", 12, "PCS", 265.00, 180.00, 12, 0.94, ""),
            ("Britannia Marie Gold 250 gm", "19053100", 40, "PCS", 30.00, 0.00, 18, 0.96, ""),
            ("Colgate Strong Teeth 100 gm", "33061020", 36, "PCS", 52.00, 0.00, 18, 0.93, ""),
            ("Surf Excel Bar 250 gm", "34011190", 48, "PCS", 22.00, 0.00, 18, 0.95, ""),
        ],
        "style": "bank",
        "round_off": -0.04,
        "note": "Has a line discount and a round-off — shows the arithmetic checks.",
    },
    {
        "file": "demo_bill_3_nagpur.png",
        "colour": "#b54708",
        "supplier": {
            "name": "Nagpur Trading Company",
            "gstin": "27LMNOP9012Q1Z3",
            "address": "88 Itwari Bazaar, Nagpur, Maharashtra - 440002",
            "phone": "0712-2567890",
        },
        "invoice": {"invoice_number": "NTC/2026/2291", "invoice_date": DATE_ISO,
                    "due_date": "2026-10-16"},
        "scope": "INTER",
        "style": "compact",
        "items": [
            ("Surf Excel Easy Wash 1 kg", "34022010", 24, "PCS", 118.00, 0.00, 18, 0.95, ""),
            ("Lifebuoy Total Soap 125 gm", "34011110", 100, "PCS", 32.00, 0.00, 18, 0.94, ""),
        ],
        "round_off": 0.24,
        "note": "Out-of-state supplier — IGST instead of CGST/SGST.",
    },
    {
        "file": "demo_bill_4_gupta.png",
        "colour": "#344054",
        "supplier": {
            "name": "Gupta Kirana Bhandar",
            "gstin": "22RSTUV3456W1Z7",
            "address": "Gudhiyari Road, Raipur, Chhattisgarh - 492009",
            "phone": "94255-11223",
        },
        "invoice": {"invoice_number": "GKB/0977", "invoice_date": DATE_ISO, "due_date": ""},
        "scope": "INTRA",
        "items": [
            ("Tata Salt 1 kg", "25010020", 30, "PCS", 22.00, 0.00, 5, 0.96, ""),
            # deliberately low confidence: shows the red flag and the review step
            ("Aashirvaad Atta 5 kg", "11010000", 20, "PCS", 255.00, 0.00, 5, 0.58, ""),
            ("Maggi Noodles 70 gm", "19023010", 96, "PCS", 14.00, 0.00, 18, 0.92, ""),
            ("Sunfeast Dark Fantasy 300 gm", "19053100", 24, "PCS", 110.00, 0.00, 18, 0.91, ""),
            ("Red Label Tea 500 gm", "09024010", 16, "PCS", 265.00, 0.00, 5, 0.93, ""),
        ],
        "style": "plain",
        "round_off": -0.12,
        "note": "One line read poorly — demonstrates the mandatory review.",
    },
]


def compute(bill):
    """Work out every derived figure so the bill and the JSON always agree."""
    lines = []
    taxable_total = discount_total = tax_total = 0.0
    for name, hsn, qty, unit, rate, discount, gst, confidence, barcode in bill["items"]:
        gross = round(qty * rate, 2)
        taxable = round(gross - discount, 2)
        tax = round(taxable * gst / 100, 2)
        if bill["scope"] == "INTRA":
            cgst = sgst = round(tax / 2, 2)
            igst = 0.0
            tax = round(cgst + sgst, 2)
        else:
            cgst = sgst = 0.0
            igst = tax
        line_total = round(taxable + tax, 2)
        lines.append({
            "raw_name": name, "normalized_name": name, "hsn": hsn, "barcode": barcode,
            "quantity": float(qty), "unit": unit, "rate": rate, "discount": discount,
            "taxable_value": taxable, "gst_rate": float(gst),
            "cgst": cgst, "sgst": sgst, "igst": igst, "cess": 0.0,
            "total": line_total, "confidence": confidence,
            "_gross": gross,
        })
        taxable_total += taxable
        discount_total += discount
        tax_total += tax

    taxable_total = round(taxable_total, 2)
    tax_total = round(tax_total, 2)
    grand = round(taxable_total + tax_total + bill["round_off"], 2)
    overall = round(min(l["confidence"] for l in lines), 2)
    return lines, taxable_total, round(discount_total, 2), tax_total, grand, overall


def draw_bill(bill, lines, taxable, discount_total, tax_total, grand, overall):
    W, H = 1000, 1350
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    f_title, f_h, f_b, f_s = font(28, True), font(16, True), font(15), font(12)
    supplier, invoice = bill["supplier"], bill["invoice"]

    style = bill.get("style", "banded")
    if style == "plain":
        # No colour band: a plain shop bill, structurally different from the rest
        d.rectangle([25, 20, W - 25, 110], outline=bill["colour"], width=3)
        d.text((40, 32), supplier["name"].upper(), font=f_title, fill=bill["colour"])
        d.text((40, 70), supplier["address"], font=f_s, fill="#475467")
        d.text((40, 88), f"Phone: {supplier['phone']}", font=f_s, fill="#475467")
    elif style == "compact":
        d.rectangle([0, 0, W, 70], fill=bill["colour"])
        d.text((30, 16), supplier["name"].upper(), font=f_title, fill="white")
        d.text((30, 50), f"{supplier['address']}  |  {supplier['phone']}",
               font=f_s, fill="#fde6cf")
    else:
        d.rectangle([0, 0, W, 96], fill=bill["colour"])
        d.text((30, 18), supplier["name"].upper(), font=f_title, fill="white")
        d.text((30, 56), supplier["address"], font=f_s, fill="#dbeafe")
        d.text((30, 74), f"Phone: {supplier['phone']}", font=f_s, fill="#dbeafe")

    d.text((30, 118), f"GSTIN: {supplier['gstin']}", font=f_b, fill="black")
    state = "Chhattisgarh (22)" if supplier["gstin"].startswith("22") else "Maharashtra (27)"
    d.text((30, 142), f"State: {state}", font=f_b, fill="black")

    d.text((620, 118), "TAX INVOICE", font=f_h, fill="black")
    d.text((620, 144), f"Invoice No: {invoice['invoice_number']}", font=f_b, fill="black")
    d.text((620, 168), f"Invoice Date: {DATE_TEXT}", font=f_b, fill="black")
    if invoice["due_date"]:
        d.text((620, 192), "Due Date: 01/10/2026", font=f_b, fill="black")

    inter = bill["scope"] == "INTER"
    y = 240
    columns = [
        (30, "Description"), (395, "HSN"), (490, "Qty"), (550, "Rate"),
        (635, "Disc"), (710, "GST%"), (785, "Amount"),
    ]
    d.rectangle([25, y - 8, W - 25, y + 26], fill="#eaecf0")
    for x, label in columns:
        d.text((x, y), label, font=f_h, fill="black")
    y += 42

    for line in lines:
        values = [
            line["raw_name"], line["hsn"], f"{line['quantity']:.0f}",
            f"{line['rate']:.2f}", f"{line['discount']:.2f}",
            f"{line['gst_rate']:.0f}", f"{line['_gross']:,.2f}",
        ]
        for (x, _), value in zip(columns, values):
            d.text((x, y), value, font=f_b, fill="black")
        d.line([25, y + 28, W - 25, y + 28], fill="#d0d5dd")
        y += 44

    y += 24
    rows = [("Taxable Value:", taxable)]
    if discount_total:
        rows.append(("Total Discount:", discount_total))
    if inter:
        rows.append(("IGST:", sum(l["igst"] for l in lines)))
    else:
        rows.append(("CGST:", sum(l["cgst"] for l in lines)))
        rows.append(("SGST:", sum(l["sgst"] for l in lines)))
    rows.append(("Round Off:", bill["round_off"]))

    for label, value in rows:
        d.text((620, y), label, font=f_b, fill="black")
        d.text((850, y), f"{value:,.2f}", font=f_b, fill="black")
        y += 30

    d.rectangle([615, y - 8, W - 25, y + 32], fill="#eaecf0")
    d.text((620, y), "GRAND TOTAL:", font=f_h, fill="black")
    d.text((850, y), f"{grand:,.2f}", font=f_h, fill="black")

    y += 90
    if lines[0]["barcode"]:
        d.text((30, y), f"Barcode ({lines[0]['raw_name']}): {lines[0]['barcode']}",
               font=f_s, fill="black")
        y += 24
    if style == "bank":
        d.rectangle([25, y - 10, 470, y + 96], outline="#d0d5dd", width=2)
        d.text((40, y), "Bank Details", font=f_h, fill="black")
        d.text((40, y + 26), "Bank: State Bank of India, Supela", font=f_s, fill="#475467")
        d.text((40, y + 46), "A/c No: 3812 4455 9901", font=f_s, fill="#475467")
        d.text((40, y + 66), "IFSC: SBIN0004521", font=f_s, fill="#475467")
        y += 116
    d.text((30, y), "Terms: Goods once sold will not be taken back. E&OE.",
           font=f_s, fill="#667085")
    d.text((700, y + 60), f"For {supplier['name'].upper()}", font=f_b, fill="black")
    return trim(img)


def trim(img, margin=60):
    """Cut the unused paper below the last printed line.

    Bills end up different heights this way, which is realistic and — since the
    mock provider matches on layout — also helps tell one bill from another.
    """
    from PIL import ImageChops

    gray = img.convert("L").point(lambda v: 0 if v > 240 else 255)
    box = ImageChops.invert(gray).getbbox()          # bounds of the white area
    ink = gray.getbbox()                             # bounds of the ink
    width, height = img.size
    last = (ink[3] if ink else height) - 1
    return img.crop((0, 0, width, min(height, last + margin)))


def main():
    OUT.mkdir(exist_ok=True)
    catalogue = []

    for bill in BILLS:
        lines, taxable, discount_total, tax_total, grand, overall = compute(bill)
        image = draw_bill(bill, lines, taxable, discount_total, tax_total, grand, overall)
        path = OUT / bill["file"]
        image.save(path)

        for line in lines:
            line.pop("_gross", None)

        catalogue.append({
            "file": bill["file"],
            "fingerprint": perceptual_fingerprint(image),
            "note": bill["note"],
            "data": {
                "supplier": bill["supplier"],
                "invoice": bill["invoice"],
                "items": lines,
                "subtotal": taxable,
                "discount_total": discount_total,
                "tax_total": tax_total,
                "cess_total": 0.0,
                "round_off": bill["round_off"],
                "grand_total": grand,
                "overall_confidence": overall,
            },
        })
        print(f"{bill['file']:28} {supplier_name(bill):24} total {grand:>10,.2f}")

    (OUT / "sample_bills.json").write_text(
        json.dumps(catalogue, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {OUT / 'sample_bills.json'}")


def supplier_name(bill):
    return bill["supplier"]["name"]


if __name__ == "__main__":
    main()
