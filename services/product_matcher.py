"""
Product matching engine.

A bill says "Parle G Gold 800G"; the product master says
"Parle-G Gold Biscuit 800 gm". This module decides whether those are the same
item, and how confident it is.

Signals, in priority order:
  1. Confirmed alias (a mapping a human approved before)  -> decisive
  2. Barcode / EAN / UPC / GTIN exact match               -> decisive
  3. SKU exact match                                      -> decisive
  4. Fuzzy name similarity (token + sequence)             -> weighted
  5. Brand agreement                                      -> weighted
  6. Pack size + unit agreement                           -> weighted
  7. HSN agreement                                        -> weighted
  8. Previous purchase history with the same supplier     -> tie-breaker

Nothing below MATCH_AUTO_THRESHOLD is ever auto-applied.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from database.models import (
    InvoiceItem,
    MatchStatus,
    Product,
    ProductAlias,
)
from utils.config import settings
from utils.logging import get_logger

log = get_logger("matcher")

try:
    from rapidfuzz import fuzz as _rf

    HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    HAS_RAPIDFUZZ = False


STOPWORDS = {
    "the", "and", "of", "with", "for", "pack", "packet", "pkt", "box", "ctn",
    "carton", "pc", "pcs", "nos", "no", "qty", "item", "product", "new",
}

UNIT_TOKENS = {
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilogram": "kg",
    "ml": "ml", "l": "l", "ltr": "l", "ltrs": "l", "litre": "l", "liter": "l",
    "mg": "mg", "pcs": "pcs", "pc": "pcs", "n": "pcs",
}

KNOWN_BRANDS = {
    "parle", "britannia", "amul", "tata", "nestle", "itc", "dabur", "hul",
    "cadbury", "haldiram", "patanjali", "sunfeast", "maggi", "colgate",
    "surf", "rin", "lux", "dettol", "lifebuoy", "godrej", "bingo", "kurkure",
    "lays", "pepsi", "coca", "sprite", "thumsup", "fortune", "saffola",
    "aashirvaad", "everest", "mdh", "catch", "kissan", "mother", "dairy",
}


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = (name or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9.]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def tokens(name: str) -> List[str]:
    return [t for t in normalize_name(name).split() if t and t not in STOPWORDS]


PACK_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|kgs|g|gm|gms|gram|grams|ml|l|ltr|ltrs|litre|liter|mg|pcs|pc|n)\b"
)


def extract_pack_size(name: str) -> Optional[tuple[float, str]]:
    """'800 gm' -> (800.0, 'g') ; '1 KG' -> (1000.0, 'g') for comparison."""
    match = PACK_RE.search(normalize_name(name))
    if not match:
        return None
    value = float(match.group(1))
    unit = UNIT_TOKENS.get(match.group(2), match.group(2))
    if unit == "kg":
        value, unit = value * 1000, "g"
    elif unit == "l":
        value, unit = value * 1000, "ml"
    return value, unit


def extract_brand(name: str) -> str:
    for token in tokens(name):
        if token in KNOWN_BRANDS:
            return token
    parts = tokens(name)
    return parts[0] if parts else ""


def string_similarity(a: str, b: str) -> float:
    """0..1 blend of token-set and sequence similarity."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if HAS_RAPIDFUZZ:
        token_score = _rf.token_set_ratio(na, nb) / 100.0
        seq_score = _rf.ratio(na, nb) / 100.0
        partial = _rf.partial_ratio(na, nb) / 100.0
        return max(0.55 * token_score + 0.30 * seq_score + 0.15 * partial, 0.0)
    set_a, set_b = set(tokens(a)), set(tokens(b))
    jaccard = len(set_a & set_b) / len(set_a | set_b) if (set_a | set_b) else 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    return 0.6 * jaccard + 0.4 * seq


@dataclass
class MatchCandidate:
    product_id: int
    product_name: str
    score: float
    reason: str
    unit: str = ""
    current_stock: float = 0.0
    gst_rate: float = 0.0


@dataclass
class MatchResult:
    status: str
    score: float = 0.0
    reason: str = ""
    product_id: Optional[int] = None
    product_name: str = ""
    candidates: List[MatchCandidate] = field(default_factory=list)

    @property
    def is_confident(self) -> bool:
        return self.status == MatchStatus.MATCHED.value


class ProductMatcher:
    """Stateless matcher over a company's product master."""

    def __init__(
        self,
        session: Session,
        company_id: int,
        auto_threshold: Optional[float] = None,
        suggest_threshold: Optional[float] = None,
    ):
        self.session = session
        self.company_id = company_id
        self.auto_threshold = (
            auto_threshold if auto_threshold is not None else settings.MATCH_AUTO_THRESHOLD
        )
        self.suggest_threshold = (
            suggest_threshold
            if suggest_threshold is not None
            else settings.MATCH_SUGGEST_THRESHOLD
        )
        self._products: Optional[List[Product]] = None
        self._aliases: Optional[Dict[str, ProductAlias]] = None
        self._supplier_history: Dict[int, set] = {}

    # -- caches ----------------------------------------------------------
    @property
    def products(self) -> List[Product]:
        if self._products is None:
            self._products = (
                self.session.query(Product)
                .filter(
                    Product.company_id == self.company_id,
                    Product.is_active.is_(True),
                )
                .all()
            )
        return self._products

    @property
    def aliases(self) -> Dict[str, ProductAlias]:
        if self._aliases is None:
            rows = (
                self.session.query(ProductAlias)
                .filter(ProductAlias.company_id == self.company_id)
                .all()
            )
            self._aliases = {row.alias_normalized: row for row in rows}
        return self._aliases

    def refresh(self) -> None:
        self._products = None
        self._aliases = None

    def _supplier_products(self, supplier_id: Optional[int]) -> set:
        if not supplier_id:
            return set()
        if supplier_id not in self._supplier_history:
            rows = (
                self.session.query(InvoiceItem.product_id)
                .join(InvoiceItem.invoice)
                .filter(
                    InvoiceItem.company_id == self.company_id,
                    InvoiceItem.product_id.isnot(None),
                )
                .all()
            )
            self._supplier_history[supplier_id] = {r[0] for r in rows}
        return self._supplier_history[supplier_id]

    # -- scoring ---------------------------------------------------------
    def score_pair(self, item: dict, product: Product, supplier_id=None) -> tuple[float, str]:
        bill_name = item.get("raw_name") or item.get("normalized_name") or ""
        name_score = string_similarity(bill_name, product.name or "")

        score = name_score * 0.62
        reasons = [f"name {name_score:.0%}"]

        bill_brand = extract_brand(bill_name)
        prod_brand = (product.brand or "").lower() or extract_brand(product.name or "")
        if bill_brand and prod_brand:
            if bill_brand == prod_brand:
                score += 0.10
                reasons.append("brand match")
            elif bill_brand in KNOWN_BRANDS and prod_brand in KNOWN_BRANDS:
                score -= 0.18
                reasons.append("brand differs")

        bill_pack = extract_pack_size(bill_name)
        prod_pack = extract_pack_size(f"{product.name} {product.pack_size or ''}")
        if bill_pack and prod_pack:
            if bill_pack == prod_pack:
                score += 0.14
                reasons.append("pack size match")
            elif bill_pack[1] == prod_pack[1]:
                score -= 0.20
                reasons.append("pack size differs")

        bill_unit = (item.get("unit") or "").upper()
        if bill_unit and product.unit and bill_unit == product.unit.upper():
            score += 0.04
            reasons.append("unit match")

        bill_hsn = re.sub(r"\D", "", item.get("hsn") or "")
        prod_hsn = re.sub(r"\D", "", product.hsn or "")
        if bill_hsn and prod_hsn:
            if bill_hsn[:4] == prod_hsn[:4]:
                score += 0.08
                reasons.append("HSN match")
            else:
                score -= 0.06
                reasons.append("HSN differs")

        if supplier_id and product.id in self._supplier_products(supplier_id):
            score += 0.04
            reasons.append("bought from this supplier before")

        return max(0.0, min(score, 0.99)), ", ".join(reasons)

    # -- public API ------------------------------------------------------
    def match(self, item: dict, supplier_id: Optional[int] = None) -> MatchResult:
        bill_name = (item.get("raw_name") or "").strip()
        barcode = re.sub(r"\D", "", item.get("barcode") or "")

        # 1. confirmed alias
        alias = self.aliases.get(normalize_name(bill_name))
        if alias:
            product = next((p for p in self.products if p.id == alias.product_id), None)
            if product:
                return MatchResult(
                    status=MatchStatus.MATCHED.value,
                    score=0.99,
                    reason="previously confirmed mapping",
                    product_id=product.id,
                    product_name=product.name,
                )

        # 2. barcode
        if barcode:
            product = next(
                (p for p in self.products if (p.barcode or "").strip() == barcode), None
            )
            if product:
                return MatchResult(
                    status=MatchStatus.MATCHED.value,
                    score=0.99,
                    reason=f"barcode {barcode} matched",
                    product_id=product.id,
                    product_name=product.name,
                )

        # 3. SKU
        sku = (item.get("sku") or "").strip().lower()
        if sku:
            product = next(
                (p for p in self.products if (p.sku or "").strip().lower() == sku), None
            )
            if product:
                return MatchResult(
                    status=MatchStatus.MATCHED.value,
                    score=0.97,
                    reason=f"SKU {sku} matched",
                    product_id=product.id,
                    product_name=product.name,
                )

        # 4. identical name (after normalisation) is decisive
        normalized = normalize_name(bill_name)
        if normalized:
            for product in self.products:
                if normalize_name(product.name) == normalized:
                    return MatchResult(
                        status=MatchStatus.MATCHED.value,
                        score=0.98,
                        reason="exact product name match",
                        product_id=product.id,
                        product_name=product.name,
                    )

        # 5-9. weighted similarity
        scored: List[MatchCandidate] = []
        for product in self.products:
            score, reason = self.score_pair(item, product, supplier_id)
            if score >= 0.35:
                scored.append(
                    MatchCandidate(
                        product_id=product.id,
                        product_name=product.name,
                        score=round(score, 4),
                        reason=reason,
                        unit=product.unit or "",
                        current_stock=product.current_stock or 0.0,
                        gst_rate=product.gst_rate or 0.0,
                    )
                )
        scored.sort(key=lambda c: c.score, reverse=True)
        top = scored[:5]

        if not top:
            status = (
                MatchStatus.NEW_BARCODE.value if barcode else MatchStatus.NEW.value
            )
            return MatchResult(
                status=status,
                score=0.0,
                reason="no similar product in master",
                candidates=[],
            )

        best = top[0]
        if best.score >= self.auto_threshold:
            return MatchResult(
                status=MatchStatus.MATCHED.value,
                score=best.score,
                reason=best.reason,
                product_id=best.product_id,
                product_name=best.product_name,
                candidates=top,
            )
        if best.score >= self.suggest_threshold:
            return MatchResult(
                status=MatchStatus.POSSIBLE.value,
                score=best.score,
                reason=best.reason,
                product_id=None,          # never auto-applied
                product_name=best.product_name,
                candidates=top,
            )
        status = MatchStatus.NEW_BARCODE.value if barcode else MatchStatus.NEW.value
        return MatchResult(
            status=status,
            score=best.score,
            reason="closest product was below the suggestion threshold",
            candidates=top,
        )

    def match_all(
        self, items: Sequence[dict], supplier_id: Optional[int] = None
    ) -> List[MatchResult]:
        return [self.match(item, supplier_id) for item in items]

    # -- learning --------------------------------------------------------
    def remember_mapping(
        self,
        raw_name: str,
        product_id: int,
        user_id: Optional[int] = None,
        supplier_id: Optional[int] = None,
        barcode: str = "",
    ) -> ProductAlias:
        """Persist a human-confirmed mapping so the next bill matches instantly."""
        key = normalize_name(raw_name)
        existing = (
            self.session.query(ProductAlias)
            .filter(
                ProductAlias.company_id == self.company_id,
                ProductAlias.alias_normalized == key,
            )
            .one_or_none()
        )
        if existing:
            existing.product_id = product_id
            existing.times_used = (existing.times_used or 0) + 1
            if barcode:
                existing.barcode = barcode
            alias = existing
        else:
            alias = ProductAlias(
                company_id=self.company_id,
                product_id=product_id,
                supplier_id=supplier_id,
                alias_raw=raw_name[:250],
                alias_normalized=key[:250],
                barcode=barcode or None,
                created_by=user_id,
            )
            self.session.add(alias)
        self.session.commit()
        self.refresh()
        log.info("Alias saved: '%s' -> product %s", raw_name, product_id)
        return alias

    def link_barcode(self, product_id: int, barcode: str) -> bool:
        barcode = re.sub(r"\D", "", barcode or "")
        if not barcode:
            return False
        product = (
            self.session.query(Product)
            .filter(Product.company_id == self.company_id, Product.id == product_id)
            .one_or_none()
        )
        if not product:
            return False
        product.barcode = barcode
        self.session.commit()
        self.refresh()
        return True

    def create_product_from_item(self, item: dict, user_id: Optional[int] = None) -> Product:
        """Create a master record for a NEW PRODUCT after the user approves it."""
        name = (item.get("normalized_name") or item.get("raw_name") or "").strip()
        if not name:
            raise ValueError("Cannot create a product without a name")
        pack = extract_pack_size(name)
        product = Product(
            company_id=self.company_id,
            name=name[:250],
            hsn=(item.get("hsn") or "")[:20] or None,
            barcode=(re.sub(r"\D", "", item.get("barcode") or "") or None),
            unit=(item.get("unit") or "PCS")[:20],
            pack_size=f"{pack[0]:g} {pack[1]}" if pack else None,
            brand=extract_brand(name) or None,
            gst_rate=float(item.get("gst_rate") or 0.0),
            purchase_rate=float(item.get("rate") or 0.0),
            current_stock=0.0,
        )
        self.session.add(product)
        self.session.commit()
        self.refresh()
        log.info("Product created from bill line: %s", product.name)
        return product
