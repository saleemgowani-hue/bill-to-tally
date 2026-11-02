"""
Document classifier.

Decides what a document IS and what the software should offer to DO with
it. The AI reports a document_type (see universal_schema.DOCUMENT_TYPES);
this module cross-checks that against simple printed-text signals and maps
it to an accounting action. It never forces a document into a Purchase
voucher: a bilty, a challan or a bank paper is reported as such and the
user picks what to do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from services.universal_schema import DOCUMENT_TYPES, normalize_document_type

LABELS = {
    "purchase_invoice": "Purchase Invoice / खरीद बिल",
    "sales_invoice": "Sales Invoice / बिक्री बिल",
    "expense_receipt": "Expense Bill / खर्च बिल",
    "debit_note": "Debit Note / डेबिट नोट",
    "credit_note": "Credit Note / क्रेडिट नोट",
    "transport_bilty": "Transport Bilty / LR / बिल्टी",
    "delivery_challan": "Delivery Challan / चालान",
    "payment_receipt": "Payment Receipt / रसीद",
    "bank_document": "Bank Document / बैंक दस्तावेज़",
    "other": "Other Document / अन्य",
}

# document type -> (default Tally voucher type, is it an accounting entry?)
DEFAULT_ACTION = {
    "purchase_invoice": ("Purchase", True),
    "sales_invoice": ("Sales", True),
    "expense_receipt": ("Purchase", True),     # accounting-mode expense purchase
    "debit_note": ("Debit Note", True),
    "credit_note": ("Credit Note", True),
    "payment_receipt": ("Payment", True),
    "bank_document": ("Contra", True),
    "transport_bilty": ("", False),
    "delivery_challan": ("", False),
    "other": ("", False),
}

# Actions a user can choose on the upload screen.
ACTIONS = {
    "purchase": "Post as Purchase voucher",
    "sales": "Post as Sales voucher",
    "debit_note": "Post as Debit Note",
    "credit_note": "Post as Credit Note",
    "manifest": "Convert to Excel (Manifest / Bilty)",
    "bank": "Open in Bank reconciliation",
    "store_only": "Store only — no accounting entry",
}

_SIGNALS = [
    ("credit_note", re.compile(r"\bcredit\s*note\b", re.I)),
    ("debit_note", re.compile(r"\bdebit\s*note\b", re.I)),
    ("transport_bilty", re.compile(r"\b(lorry\s*receipt|consignment\s*note|bilty|g\.?\s*r\.?\s*no|l\.?\s*r\.?\s*no)\b", re.I)),
    ("delivery_challan", re.compile(r"\bdelivery\s*challan\b", re.I)),
    ("bank_document", re.compile(r"\b(statement\s*of\s*account|bank\s*statement|ifsc|cheque\s*no)\b", re.I)),
    ("payment_receipt", re.compile(r"\b(payment\s*receipt|received\s*with\s*thanks|money\s*receipt)\b", re.I)),
]


@dataclass
class Classification:
    document_type: str
    label: str
    confidence: float
    voucher_type: str
    is_accounting: bool
    suggested_action: str
    matches_flow: bool = True
    notes: List[str] = field(default_factory=list)


def classify(
    ai_type: str = "",
    ai_confidence: float = 0.0,
    text: str = "",
    flow: str = "purchase",
    seller_gstin: str = "",
    buyer_gstin: str = "",
    our_gstin: str = "",
) -> Classification:
    """Combine the AI's answer with printed-text signals.

    flow: which screen the document came in through ("purchase" or "sales").
    """
    doc_type = normalize_document_type(ai_type)
    confidence = float(ai_confidence or 0.0)
    notes: List[str] = []

    signal = next((name for name, rx in _SIGNALS if text and rx.search(text)), "")
    if signal and doc_type and signal != doc_type and confidence < 0.8:
        notes.append(
            f"Printed text looks like a {LABELS[signal].split(' /')[0]}; "
            f"AI said {LABELS.get(doc_type, doc_type).split(' /')[0]}."
        )
        doc_type, confidence = signal, max(confidence, 0.6)
    if not doc_type:
        doc_type = signal or ("sales_invoice" if flow == "sales" else "purchase_invoice")
        confidence = confidence or (0.6 if signal else 0.4)

    # Our own GSTIN tells us which side of an invoice we are on.
    ours = (our_gstin or "").strip().upper()
    if ours and doc_type in ("purchase_invoice", "sales_invoice"):
        if seller_gstin and seller_gstin.upper() == ours:
            if doc_type != "sales_invoice":
                notes.append("Seller GSTIN is your own GSTIN — this is a sales invoice.")
            doc_type = "sales_invoice"
        elif buyer_gstin and buyer_gstin.upper() == ours and doc_type != "purchase_invoice":
            notes.append("Buyer GSTIN is your own GSTIN — this is a purchase invoice.")
            doc_type = "purchase_invoice"

    voucher_type, is_accounting = DEFAULT_ACTION.get(doc_type, ("", False))
    action = {
        "purchase_invoice": "purchase", "expense_receipt": "purchase",
        "sales_invoice": "sales", "debit_note": "debit_note",
        "credit_note": "credit_note", "transport_bilty": "manifest",
        "delivery_challan": "store_only", "payment_receipt": "bank",
        "bank_document": "bank", "other": "store_only",
    }.get(doc_type, "store_only")

    expected = "sales_invoice" if flow == "sales" else "purchase_invoice"
    matches = doc_type == expected or (flow == "purchase" and doc_type == "expense_receipt")
    if not matches:
        notes.append(
            f"This looks like a {LABELS.get(doc_type, doc_type).split(' /')[0]}, "
            f"not a {LABELS[expected].split(' /')[0]}. Please choose the right action "
            "before posting anything to Tally."
        )
    return Classification(
        document_type=doc_type,
        label=LABELS.get(doc_type, doc_type),
        confidence=round(min(max(confidence, 0.0), 1.0), 2),
        voucher_type=voucher_type,
        is_accounting=is_accounting,
        suggested_action=action,
        matches_flow=matches,
        notes=notes,
    )


def classify_parsed(data: dict, flow: str = "purchase", our_gstin: str = "",
                    raw_text: Optional[str] = None) -> Classification:
    extras = (data or {}).get("universal") or {}
    return classify(
        ai_type=extras.get("document_type", ""),
        ai_confidence=extras.get("document_type_confidence", 0.0),
        text=raw_text or "",
        flow=flow,
        seller_gstin=(extras.get("seller") or {}).get("gstin", ""),
        buyer_gstin=(extras.get("buyer") or {}).get("gstin", ""),
        our_gstin=our_gstin,
    )


assert set(DEFAULT_ACTION) == set(DOCUMENT_TYPES)
