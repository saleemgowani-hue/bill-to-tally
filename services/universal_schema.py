"""
Universal document schema.

One extraction contract for every industry and every kind of business
document (purchase bill, sales bill, expense receipt, debit/credit note,
transport bilty, delivery challan, payment receipt, bank paper ...).

How it fits the existing pipeline:

* The AI engine is asked for UNIVERSAL_SCHEMA_HINT (a superset of the
  original invoice schema).
* `ai_invoice_parser.normalize_invoice` keeps producing the exact contract
  the rest of the app already relies on (supplier / invoice / items /
  totals), and additionally stores everything extra under
  data["universal"] via `extract_extras` below — so nothing that already
  works is changed, and nothing the bill printed is thrown away.
* `build_universal_document` assembles the full common JSON (the shape in
  the master spec: document_type, supplier, customer, invoice_details,
  items, tax_summary, charges, totals, extraction_confidence,
  validation_errors, source_document) for export, audit and the API.

Safety: values that are not printed stay null/empty. Nothing here computes
or "fixes" an accounting figure — that is the validation engine's job, and
it only ever flags, never edits.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0"

DOCUMENT_TYPES = (
    "purchase_invoice",
    "sales_invoice",
    "expense_receipt",
    "debit_note",
    "credit_note",
    "transport_bilty",
    "delivery_challan",
    "payment_receipt",
    "bank_document",
    "other",
)

UNIVERSAL_SCHEMA_HINT = """Return ONLY a JSON object with exactly this shape (no markdown, no commentary):

{
  "document_type": "purchase_invoice | sales_invoice | expense_receipt | debit_note | credit_note | transport_bilty | delivery_challan | payment_receipt | bank_document | other",
  "document_type_confidence": 0,
  "seller": {"name": "", "gstin": "", "address": "", "state": "", "state_code": ""},
  "buyer": {"name": "", "gstin": "", "address": "", "state": "", "state_code": ""},
  "invoice": {
    "invoice_number": "", "invoice_date": "", "due_date": "",
    "place_of_supply": "", "reverse_charge": "", "po_number": "",
    "eway_bill_number": "", "transport_details": "", "payment_terms": "",
    "original_invoice_number": ""
  },
  "items": [
    {
      "sno": "", "raw_name": "", "normalized_name": "", "description": "",
      "hsn": "", "barcode": "", "quantity": 0, "free_quantity": 0, "unit": "",
      "rate": 0, "discount_percent": 0, "discount": 0, "taxable_value": 0,
      "gst_rate": 0, "cgst_rate": 0, "cgst": 0, "sgst_rate": 0, "sgst": 0,
      "igst_rate": 0, "igst": 0, "cess": 0, "other_charges": 0, "total": 0,
      "confidence": 0, "page": 0
    }
  ],
  "subtotal": 0, "discount_total": 0, "tax_total": 0,
  "cgst_total": 0, "sgst_total": 0, "igst_total": 0, "cess_total": 0,
  "freight": 0, "packing_charges": 0, "other_charges": 0,
  "round_off": 0, "grand_total": 0, "amount_in_words": "", "balance_payable": 0,
  "field_confidence": {
    "seller.name": 0, "seller.gstin": 0, "invoice.invoice_number": 0,
    "invoice.invoice_date": 0, "grand_total": 0
  },
  "overall_confidence": 0
}"""

UNIVERSAL_PROMPT = """You are an expert Indian accountant reading ONE business document for data entry. The business could be in any industry (retail, wholesale, pharmacy, hospital, garments, restaurant, transport, manufacturing, construction, hardware, electronics, automobile, petrol pump, real estate, agriculture, distribution or other). Do not assume an industry — read what is printed.

Step 1 — identify document_type:
- purchase_invoice: a tax invoice / bill raised on {our_role_buyer}.
- sales_invoice: a tax invoice / bill raised BY {our_company} to its customer.
- expense_receipt: a bill for a service or expense (rent, electricity, phone, hotel, fuel, repairs, professional fees) rather than stock goods.
- debit_note / credit_note: a printed debit note or credit note (fill invoice.original_invoice_number if the note refers to one).
- transport_bilty: a lorry receipt / consignment note / bilty / manifest.
- delivery_challan: a challan for moving goods with no tax invoice.
- payment_receipt: a receipt acknowledging money paid or received.
- bank_document: a bank statement, cheque, deposit slip or bank advice.
- other: anything else. document_type_confidence is 0.0-1.0.

Step 2 — extract exactly as printed. Rules:
1. NEVER guess or invent. If a value is not printed or not legible use "" for text and 0 for numbers, and give that field a low confidence.
2. Do NOT calculate anything that is not printed. Copy printed figures; the software checks the arithmetic itself.
3. seller = the party that issued the document; buyer = the party it is addressed to.
4. Dates as ISO YYYY-MM-DD. Indian documents are normally DD/MM/YYYY — convert accordingly.
5. Numbers plain (1234.50): no currency symbols, commas or % signs. Rates are percentages (18 means 18%).
6. Extract EVERY item row, in printed order, including rows with long multi-line descriptions, rows in tables without borders, merged cells, and readable handwritten rows. Never skip or summarise rows. raw_name is the item text exactly as printed; normalized_name a cleaned version; description any extra printed detail (batch, expiry, size, colour, model, serial).
7. Freight, packing/forwarding, loading, insurance and similar charges printed BELOW the item table go in freight / packing_charges / other_charges — not in items.
8. Split tax into cgst/sgst/igst only as printed. If tax amounts are printed only in the totals section, copy them into cgst_total / sgst_total / igst_total and leave per-line tax amounts at 0.
9. amount_in_words: copy the 'Rupees ... only' line exactly if printed.
10. confidence (per item) and field_confidence values are 0.0-1.0: how sure you are that the value was read correctly. overall_confidence reflects legibility and completeness.
11. page is the 1-based page number (within the pages you were given) on which the item row is printed.
{page_note}"""


def _num(value: Any) -> Optional[float]:
    """Number or None. None means 'not printed' and is kept distinct from 0."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
    if text in ("", "-", ".", "-."):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _txt(value: Any, max_len: int = 300) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:max_len]


def _party(block: Any) -> Dict[str, str]:
    if isinstance(block, str):
        block = {"name": block}
    block = block if isinstance(block, dict) else {}
    return {
        "name": _txt(block.get("name"), 200),
        "gstin": _txt(block.get("gstin"), 20).upper().replace(" ", ""),
        "address": _txt(block.get("address"), 500),
        "state": _txt(block.get("state"), 80),
        "state_code": _txt(block.get("state_code"), 4),
    }


def normalize_document_type(value: Any) -> str:
    text = _txt(value, 60).lower().replace(" ", "_").replace("-", "_")
    if text in DOCUMENT_TYPES:
        return text
    aliases = {
        "tax_invoice": "purchase_invoice", "invoice": "purchase_invoice",
        "bill": "purchase_invoice", "purchase_bill": "purchase_invoice",
        "sales_bill": "sales_invoice", "expense": "expense_receipt",
        "expense_bill": "expense_receipt", "lorry_receipt": "transport_bilty",
        "bilty": "transport_bilty", "lr": "transport_bilty",
        "consignment_note": "transport_bilty", "manifest": "transport_bilty",
        "challan": "delivery_challan", "receipt": "payment_receipt",
        "bank_statement": "bank_document",
    }
    return aliases.get(text, "other" if text else "")


def is_universal_payload(data: dict) -> bool:
    """True when the AI answered with the universal schema (seller/buyer)."""
    return isinstance(data, dict) and ("seller" in data or "document_type" in data)


def to_legacy_shape(data: dict, our_side: str = "buyer") -> dict:
    """Map a universal answer onto the original contract.

    The original contract has one "supplier" block meaning *the other party*
    on the bill. For a purchase that is the seller; for a sales invoice we
    are the seller, so the other party is the buyer.
    """
    if not is_universal_payload(data):
        return data
    out = dict(data)
    seller = data.get("seller") or {}
    buyer = data.get("buyer") or {}
    out["supplier"] = buyer if our_side == "seller" else seller
    out.setdefault("customer", seller if our_side == "seller" else buyer)
    return out


def extract_extras(data: dict, our_side: str = "buyer") -> Dict[str, Any]:
    """Everything the universal schema carries beyond the original contract."""
    if not is_universal_payload(data):
        return {}
    invoice = data.get("invoice") if isinstance(data.get("invoice"), dict) else {}
    seller = _party(data.get("seller"))
    buyer = _party(data.get("buyer"))
    raw_items = data.get("items") if isinstance(data.get("items"), list) else []
    item_extras: List[dict] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        item_extras.append({
            "sno": _txt(entry.get("sno"), 10),
            "description": _txt(entry.get("description"), 500),
            "free_quantity": _num(entry.get("free_quantity")),
            "discount_percent": _num(entry.get("discount_percent")),
            "cgst_rate": _num(entry.get("cgst_rate")),
            "sgst_rate": _num(entry.get("sgst_rate")),
            "igst_rate": _num(entry.get("igst_rate")),
            "other_charges": _num(entry.get("other_charges")),
            "page": int(_num(entry.get("page")) or 0),
        })
    field_conf = data.get("field_confidence")
    field_conf = {
        str(k): max(0.0, min(1.0, (_num(v) or 0.0) / (100.0 if (_num(v) or 0) > 1 else 1.0)))
        for k, v in (field_conf.items() if isinstance(field_conf, dict) else [])
    }
    dtc = _num(data.get("document_type_confidence")) or 0.0
    if dtc > 1:
        dtc = min(dtc / 100.0, 1.0)
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": normalize_document_type(data.get("document_type")),
        "document_type_confidence": dtc,
        "our_side": our_side,
        "seller": seller,
        "buyer": buyer,
        "invoice_details": {
            "place_of_supply": _txt(invoice.get("place_of_supply"), 80),
            "reverse_charge": _txt(invoice.get("reverse_charge"), 10),
            "po_number": _txt(invoice.get("po_number"), 60),
            "eway_bill_number": _txt(invoice.get("eway_bill_number"), 30),
            "transport_details": _txt(invoice.get("transport_details"), 300),
            "payment_terms": _txt(invoice.get("payment_terms"), 200),
            "original_invoice_number": _txt(invoice.get("original_invoice_number"), 80),
        },
        "charges": {
            "freight": _num(data.get("freight")),
            "packing_charges": _num(data.get("packing_charges")),
            "other_charges": _num(data.get("other_charges")),
        },
        "amount_in_words": _txt(data.get("amount_in_words"), 400),
        "balance_payable": _num(data.get("balance_payable")),
        "field_confidence": field_conf,
        "item_extras": item_extras,
    }


def charges_total(data: dict) -> float:
    """Freight + packing + other charges printed outside the item table."""
    charges = ((data or {}).get("universal") or {}).get("charges") or {}
    return round(sum(float(v or 0) for v in charges.values()), 2)


def build_universal_document(
    data: dict,
    validation: Optional[dict] = None,
    source: Optional[dict] = None,
) -> dict:
    """Assemble the full common JSON from a normalized invoice dict."""
    data = data or {}
    extras = data.get("universal") or {}
    supplier = data.get("supplier") or {}
    invoice = data.get("invoice") or {}
    our_side = extras.get("our_side", "buyer")
    other_party = dict(supplier)
    seller = extras.get("seller") or (other_party if our_side == "buyer" else {})
    buyer = extras.get("buyer") or (other_party if our_side == "seller" else {})

    items = []
    item_extras = extras.get("item_extras") or []
    for index, item in enumerate(data.get("items") or []):
        row = dict(item)
        if index < len(item_extras):
            row.update({k: v for k, v in item_extras[index].items() if v not in (None, "")})
        items.append(row)

    validation = validation or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": extras.get("document_type") or "",
        "supplier": seller,
        "customer": buyer,
        "invoice_details": {
            "invoice_number": invoice.get("invoice_number") or None,
            "invoice_date": invoice.get("invoice_date") or None,
            "due_date": invoice.get("due_date") or None,
            **{k: (v or None) for k, v in (extras.get("invoice_details") or {}).items()},
        },
        "items": items,
        "tax_summary": {
            "cgst_total": data.get("cgst_total"),
            "sgst_total": data.get("sgst_total"),
            "igst_total": data.get("igst_total"),
            "cess_total": data.get("cess_total"),
            "tax_total": data.get("tax_total"),
        },
        "charges": extras.get("charges") or {},
        "totals": {
            "subtotal": data.get("subtotal"),
            "discount_total": data.get("discount_total"),
            "round_off": data.get("round_off"),
            "grand_total": data.get("grand_total"),
            "amount_in_words": extras.get("amount_in_words") or None,
            "balance_payable": extras.get("balance_payable"),
        },
        "extraction_confidence": {
            "overall": data.get("overall_confidence"),
            "document_type": extras.get("document_type_confidence"),
            "fields": extras.get("field_confidence") or {},
            "items": [i.get("confidence") for i in data.get("items") or []],
        },
        "validation_errors": [
            issue for issue in (validation.get("issues") or [])
            if issue.get("severity") in ("error", "warning")
        ],
        "verification_status": validation.get("verification_status"),
        "source_document": source or {},
    }
