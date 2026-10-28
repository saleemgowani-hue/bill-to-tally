"""
Demo account housekeeping.

A demo company is for showing the software to a prospect, not for keeping
records. After a demo you clear it with one button and the next prospect sees
a fresh install.

Clearing is MANUAL by default. An automatic timer would be worse than useless
here: bills vanishing mid-demo, while a customer is watching, is exactly the
wrong moment. Set DEMO_RETENTION_MINUTES above 0 only for an unattended public
demo nobody is presenting.

What clearing removes: every bill, line, transaction, stock movement and audit
entry created after the demo was set up, and the seeded products go back to
their opening stock.

What is NEVER touched:
  - the company, its users, its settings or its licence
  - the products, suppliers and mappings created when the demo was seeded
  - every other company in the database

That last point is the important one. Every function here refuses outright on a
company whose is_demo flag is not set, so a real customer's books can never be
cleared by this code — not by a mis-click, not by a bug elsewhere, not by an
operator on the wrong screen.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Dict, List

from sqlalchemy import delete as sa_delete
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from database.database import get_setting, set_setting
from database.models import (
    AuditLog,
    Base,
    Company,
    Invoice,
    InvoiceItem,
    OCRResult,
    ProcessingError,
    Product,
    ProductAlias,
    PurchaseTransaction,
    StockMovement,
    Supplier,
)
from utils.config import settings
from utils.logging import get_logger

log = get_logger("demo")

BASELINE_STOCK_KEY = "demo_baseline_stock"
CLEAR_ACTION = "DEMO_DATA_CLEARED"
LAST_SWEEP_KEY = "demo_last_sweep"


@dataclass
class SweepResult:
    ran: bool = False
    deleted: Dict[str, int] = field(default_factory=dict)
    stock_reset: int = 0

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())


def _aware(moment):
    """For arithmetic in Python: everything stored is UTC."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def _naive(moment):
    """For SQL comparisons.

    SQLite has no datetime type — it compares these columns as text. The
    app writes timezone-aware values but reads them back naive, so a bound
    parameter carrying '+00:00' would never compare correctly against the
    stored text. Strip the offset before it reaches a query.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(dt.timezone.utc).replace(tzinfo=None)


def mark_as_demo(session: Session, company: Company) -> None:
    """Flag a company as a demo and record what 'clean' looks like."""
    now = dt.datetime.now(dt.timezone.utc)
    company.is_demo = True
    company.demo_baseline_at = now

    baseline = {
        str(p.id): p.current_stock or 0.0
        for p in session.query(Product).filter(Product.company_id == company.id).all()
    }
    session.commit()
    set_setting(session, company.id, BASELINE_STOCK_KEY, json.dumps(baseline))
    log.info("Company %s marked as demo with %d baseline products", company.id, len(baseline))


def is_demo(company: Company) -> bool:
    return bool(company is not None and company.is_demo)


def auto_wipe_enabled() -> bool:
    """A retention of 0 (the default) means: never clear on a timer."""
    return settings.DEMO_RETENTION_MINUTES > 0


def minutes_until_wipe(session: Session, company: Company) -> int | None:
    """How long the oldest piece of visitor data has left.

    None when nothing is pending, or when the timer is switched off — which it
    is by default, because clearing is a deliberate act between demos.
    """
    if not is_demo(company) or not auto_wipe_enabled():
        return None
    baseline = _naive(company.demo_baseline_at)
    if baseline is None:
        return None
    oldest = (
        session.query(Invoice.created_at)
        .filter(Invoice.company_id == company.id, Invoice.created_at > baseline)
        .order_by(Invoice.created_at.asc())
        .first()
    )
    if oldest is None or oldest[0] is None:
        return None
    due = _aware(oldest[0]) + dt.timedelta(minutes=settings.DEMO_RETENTION_MINUTES)
    remaining = (due - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
    return max(0, int(remaining))


def _chunks(values, size=400):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _detach(session: Session, table_name: str, doomed_ids, result: SweepResult,
            _seen=None) -> None:
    """Make it safe to delete these rows, by dealing with everything that
    points at them.

    SQLite enforces foreign keys here (the app switches them on), so a product
    cannot be deleted while an alias or a stock movement still references it.
    Deleting by timestamp alone does not cover this: a manual stock adjustment
    made during a demo has no invoice, so the invoice pass misses it, and it
    outlives the product it points at. The same gap let a processing error or
    a duplicate-bill link survive its invoice.

    Rather than maintain a hand-written list of what to delete first — which
    goes stale the day someone adds a table — this reads the foreign keys out
    of the schema itself, exactly as delete_company does.

    A reference that cannot be null takes its row with it. One that can be
    null is simply cleared, because deleting an invoice line in order to
    remove a product would silently change that bill's totals, and a bill
    whose figures change on its own is worse than a stale link.
    """
    doomed_ids = list(doomed_ids)
    if not doomed_ids:
        return
    _seen = _seen or set()

    for table in reversed(Base.metadata.sorted_tables):
        for column in table.columns:
            if not any(fk.column.table.name == table_name
                       for fk in column.foreign_keys):
                continue

            key = (table.name, column.name)
            if column.nullable:
                for chunk in _chunks(doomed_ids):
                    session.execute(
                        sa_update(table)
                        .where(column.in_(chunk))
                        .values({column.name: None})
                    )
                continue

            # NOT NULL: the referencing rows cannot survive, so they go too —
            # after whatever points at *them* has been dealt with.
            if key in _seen:            # guards a NOT NULL cycle
                continue
            _seen.add(key)

            children = []
            for chunk in _chunks(doomed_ids):
                children += [
                    row[0] for row in session.execute(
                        sa_select(table.c.id).where(column.in_(chunk))
                    )
                ]
            if not children:
                continue

            _detach(session, table.name, children, result, _seen)
            removed = 0
            for chunk in _chunks(children):
                removed += session.execute(
                    sa_delete(table).where(table.c.id.in_(chunk))
                ).rowcount or 0
            if removed:
                result.deleted[table.name] = (
                    result.deleted.get(table.name, 0) + removed
                )


def sweep_company(session: Session, company: Company, *, force: bool = False) -> SweepResult:
    """Delete demo data older than the retention window."""
    result = SweepResult()
    if not is_demo(company):
        return result

    # A retention of 0 means "no timer", never "delete everything now". Without
    # this, a timed sweep would compute a cutoff of `now` and wipe the demo the
    # instant anything was entered — the opposite of what the setting says.
    if not force and not auto_wipe_enabled():
        return result

    baseline = _naive(company.demo_baseline_at) or _naive(company.created_at)
    if baseline is None:
        return result

    now = dt.datetime.now(dt.timezone.utc)
    # Bounds go into SQL, so they must be naive UTC — see _naive().
    cutoff = _naive(
        now if force else now - dt.timedelta(minutes=settings.DEMO_RETENTION_MINUTES)
    )
    result.ran = True

    # Invoices first: children are removed via their invoice ids, so nothing
    # is orphaned if the sweep is interrupted halfway.
    stale_invoice_ids = [
        row[0]
        for row in session.query(Invoice.id)
        .filter(
            Invoice.company_id == company.id,
            Invoice.created_at > baseline,
            Invoice.created_at < cutoff,
        )
        .all()
    ]

    if stale_invoice_ids:
        for model, column, label in (
            (StockMovement, StockMovement.invoice_id, "stock_movements"),
            (PurchaseTransaction, PurchaseTransaction.invoice_id, "purchase_transactions"),
            (OCRResult, OCRResult.invoice_id, "ocr_results"),
            (InvoiceItem, InvoiceItem.invoice_id, "invoice_items"),
            (AuditLog, AuditLog.invoice_id, "audit_logs"),
        ):
            count = (
                session.query(model)
                .filter(column.in_(stale_invoice_ids))
                .delete(synchronize_session=False)
            )
            if count:
                result.deleted[label] = result.deleted.get(label, 0) + count

        # Anything else still pointing at these invoices — a processing error,
        # a duplicate-bill link — is cleared here. The list above covers the
        # usual children; this covers the ones nobody remembered to add to it.
        _detach(session, "invoices", stale_invoice_ids, result)

        count = (
            session.query(Invoice)
            .filter(Invoice.id.in_(stale_invoice_ids))
            .delete(synchronize_session=False)
        )
        result.deleted["invoices"] = count

    # Child rows the visitor created directly, before the masters they point
    # at. Most of the demo's rows go here, by the ordinary date rule; _detach
    # below only has to mop up whatever this missed.
    for model, label in (
        (StockMovement, "stock_movements"),
        (ProductAlias, "product_aliases"),
        (ProcessingError, "processing_errors"),
        (AuditLog, "audit_logs"),
    ):
        query = session.query(model).filter(
            model.company_id == company.id,
            model.created_at > baseline,
            model.created_at < cutoff,
        )
        if model is AuditLog:
            # Keep the record of previous clears. Otherwise each clear erases
            # the evidence of the last one, and nobody can tell afterwards
            # whether a demo was cleared or simply never used.
            query = query.filter(AuditLog.action != CLEAR_ACTION)
        count = query.delete(synchronize_session=False)
        if count:
            result.deleted[label] = result.deleted.get(label, 0) + count

    # Masters last, each one detached from whatever still refers to it.
    for model, label in ((Product, "products"), (Supplier, "suppliers")):
        doomed = [
            row[0] for row in session.query(model.id).filter(
                model.company_id == company.id,
                model.created_at > baseline,
                model.created_at < cutoff,
            ).all()
        ]
        if not doomed:
            continue
        _detach(session, model.__tablename__, doomed, result)
        count = (
            session.query(model)
            .filter(model.id.in_(doomed))
            .delete(synchronize_session=False)
        )
        if count:
            result.deleted[label] = result.deleted.get(label, 0) + count

    # Put the seeded products back to their original stock.
    raw = get_setting(session, company.id, BASELINE_STOCK_KEY, "")
    if raw:
        try:
            baseline_stock: Dict[str, float] = json.loads(raw)
        except ValueError:
            baseline_stock = {}
        for product_id, quantity in baseline_stock.items():
            product = (
                session.query(Product)
                .filter(Product.id == int(product_id), Product.company_id == company.id)
                .one_or_none()
            )
            if product is not None and product.current_stock != quantity:
                product.current_stock = quantity
                result.stock_reset += 1

    session.commit()
    # The reference clearing above runs as plain SQL, which the ORM's identity
    # map never sees. Anything the caller is still holding would keep showing
    # values that no longer exist in the database — a deleted product, an
    # invoice line still pointing at it. Expiring makes the next attribute
    # read go back to the database, which is what the screen needs.
    session.expire_all()

    if result.total_deleted or result.stock_reset:
        log.info(
            "Demo sweep for company %s removed %d rows, reset %d products",
            company.id, result.total_deleted, result.stock_reset,
        )
    return result


def sweep_all(session: Session) -> List[SweepResult]:
    """Timer-based sweep of every demo company. Safe to call often.

    Does nothing unless DEMO_RETENTION_MINUTES is set above 0, which it is not
    by default — a demo is cleared deliberately, between prospects.
    """
    results: List[SweepResult] = []
    if not auto_wipe_enabled():
        return results

    for company in session.query(Company).filter(Company.is_demo.is_(True)).all():
        last = get_setting(session, company.id, LAST_SWEEP_KEY, "")
        if last:
            try:
                previous = _aware(dt.datetime.fromisoformat(last))
                age = (dt.datetime.now(dt.timezone.utc) - previous).total_seconds()
                if age < settings.DEMO_SWEEP_INTERVAL_SECONDS:
                    continue
            except ValueError:
                pass
        results.append(sweep_company(session, company))
        set_setting(
            session, company.id, LAST_SWEEP_KEY,
            dt.datetime.now(dt.timezone.utc).isoformat(),
        )
    return results


# --------------------------------------------------------------------------
# Manual clear — the button used between demos
# --------------------------------------------------------------------------
class NotADemoCompany(Exception):
    """Raised when something tries to clear a real company's data."""


def clear_demo_data(session: Session, company: Company, user=None) -> SweepResult:
    """Clear everything entered since this demo was set up. Immediate.

    Refuses outright on a company that is not flagged as a demo. This is the
    one guard that matters in the whole module: a real customer's purchase
    records must be unreachable from here, whatever screen the operator is on
    and whatever a caller passes in. Raising beats returning an empty result,
    because a silent no-op would let a broken caller report success.
    """
    if company is None:
        raise NotADemoCompany("No company loaded.")
    if not is_demo(company):
        raise NotADemoCompany(
            f"'{company.name}' is a real company, not a demo. Its data is "
            "permanent and cannot be cleared here."
        )

    result = sweep_company(session, company, force=True)

    try:
        from services import audit_service

        audit_service.record(
            session,
            company.id,
            CLEAR_ACTION,
            f"Demo data cleared manually: {result.total_deleted} row(s) removed, "
            f"{result.stock_reset} product(s) reset to opening stock",
            user=user,
        )
    except Exception:          # an audit failure must not lose the clear
        log.exception("Could not write the demo-clear audit entry")

    return result


def demo_data_summary(session: Session, company: Company) -> Dict[str, int]:
    """What a clear would remove right now, for showing on the button."""
    if not is_demo(company):
        return {}
    baseline = _naive(company.demo_baseline_at) or _naive(company.created_at)
    if baseline is None:
        return {}

    counts = {
        "bills": session.query(Invoice)
        .filter(Invoice.company_id == company.id, Invoice.created_at > baseline)
        .count(),
        "products added": session.query(Product)
        .filter(Product.company_id == company.id, Product.created_at > baseline)
        .count(),
        "suppliers added": session.query(Supplier)
        .filter(Supplier.company_id == company.id, Supplier.created_at > baseline)
        .count(),
    }
    return {label: number for label, number in counts.items() if number}


# --------------------------------------------------------------------------
# Sample data — the products and suppliers the demo was seeded with
# --------------------------------------------------------------------------
# These exist so product matching has something to match against and so stock
# figures move visibly during a demo. They are not something anyone uploaded,
# which is confusing the first time a name like "Shreeji Distributors" appears
# in the supplier list. Recording their ids lets the app label them as samples
# and remove them on request, without adding a column to every table.
SAMPLE_IDS_KEY = "demo_sample_ids"


def record_sample_data(session: Session, company: Company, *,
                       product_ids=(), supplier_ids=()) -> None:
    """Remember which rows were seeded, not entered."""
    set_setting(
        session, company.id, SAMPLE_IDS_KEY,
        json.dumps({
            "products": sorted(int(i) for i in product_ids),
            "suppliers": sorted(int(i) for i in supplier_ids),
        }),
    )


def _samples_from_baseline(session: Session, company: Company) -> Dict[str, set]:
    """Work out the samples for a demo seeded before ids were recorded.

    Anything that existed when the demo was marked came from the seed, by
    definition — the baseline is the moment the company was declared clean.
    Without this, an installation set up by an earlier version would show no
    sample labels at all until it was re-seeded.
    """
    baseline = _naive(company.demo_baseline_at)
    if baseline is None:
        return {"products": set(), "suppliers": set()}
    return {
        "products": {
            row[0] for row in session.query(Product.id).filter(
                Product.company_id == company.id,
                Product.created_at <= baseline,
            ).all()
        },
        "suppliers": {
            row[0] for row in session.query(Supplier.id).filter(
                Supplier.company_id == company.id,
                Supplier.created_at <= baseline,
            ).all()
        },
    }


def sample_ids(session: Session, company: Company) -> Dict[str, set]:
    """Ids of the seeded sample rows. Empty for a real company."""
    empty = {"products": set(), "suppliers": set()}
    if not is_demo(company):
        return empty
    raw = get_setting(session, company.id, SAMPLE_IDS_KEY, "")
    if not raw:
        return _samples_from_baseline(session, company)
    try:
        stored = json.loads(raw)
    except ValueError:
        return empty
    return {
        "products": {int(i) for i in stored.get("products", [])},
        "suppliers": {int(i) for i in stored.get("suppliers", [])},
    }


def sample_data_summary(session: Session, company: Company) -> Dict[str, int]:
    """How many sample rows are still present."""
    ids = sample_ids(session, company)
    counts = {}
    if ids["products"]:
        counts["sample products"] = (
            session.query(Product)
            .filter(Product.company_id == company.id,
                    Product.id.in_(ids["products"]))
            .count()
        )
    if ids["suppliers"]:
        counts["sample suppliers"] = (
            session.query(Supplier)
            .filter(Supplier.company_id == company.id,
                    Supplier.id.in_(ids["suppliers"]))
            .count()
        )
    return {label: number for label, number in counts.items() if number}


def remove_sample_data(session: Session, company: Company, user=None) -> Dict[str, int]:
    """Delete the seeded products and suppliers, leaving a bare demo.

    Refuses on a real company for the same reason clear_demo_data does: no
    path in this module may touch a customer's masters.

    A sample row referenced by a bill is kept, because deleting it would leave
    that bill pointing at nothing. Clear the demo data first, then remove the
    samples.
    """
    if company is None:
        raise NotADemoCompany("No company loaded.")
    if not is_demo(company):
        raise NotADemoCompany(
            f"'{company.name}' is a real company, not a demo. Its products and "
            "suppliers are permanent and cannot be removed here."
        )

    ids = sample_ids(session, company)
    removed: Dict[str, int] = {}
    kept = 0

    if ids["suppliers"]:
        in_use = {
            row[0] for row in session.query(Invoice.supplier_id)
            .filter(Invoice.company_id == company.id,
                    Invoice.supplier_id.in_(ids["suppliers"]))
            .distinct().all()
        }
        removable = ids["suppliers"] - in_use
        kept += len(in_use)
        if removable:
            removed["suppliers"] = (
                session.query(Supplier)
                .filter(Supplier.company_id == company.id,
                        Supplier.id.in_(removable))
                .delete(synchronize_session=False)
            )

    if ids["products"]:
        in_use = {
            row[0] for row in session.query(InvoiceItem.product_id)
            .filter(InvoiceItem.company_id == company.id,
                    InvoiceItem.product_id.in_(ids["products"]))
            .distinct().all()
        }
        removable = ids["products"] - in_use
        kept += len(in_use)
        if removable:
            session.query(ProductAlias).filter(
                ProductAlias.company_id == company.id,
                ProductAlias.product_id.in_(removable),
            ).delete(synchronize_session=False)
            session.query(StockMovement).filter(
                StockMovement.company_id == company.id,
                StockMovement.product_id.in_(removable),
            ).delete(synchronize_session=False)
            removed["products"] = (
                session.query(Product)
                .filter(Product.company_id == company.id,
                        Product.id.in_(removable))
                .delete(synchronize_session=False)
            )

    # Forget whatever was actually removed; anything kept stays labelled.
    still_there = {
        "products": sorted(
            i for i in ids["products"]
            if session.query(Product.id).filter(Product.id == i).first()
        ),
        "suppliers": sorted(
            i for i in ids["suppliers"]
            if session.query(Supplier.id).filter(Supplier.id == i).first()
        ),
    }
    session.commit()
    set_setting(session, company.id, SAMPLE_IDS_KEY, json.dumps(still_there))

    # The opening-stock baseline described products that no longer exist.
    set_setting(
        session, company.id, BASELINE_STOCK_KEY,
        json.dumps({
            str(p.id): p.current_stock or 0.0
            for p in session.query(Product)
            .filter(Product.company_id == company.id).all()
        }),
    )

    if kept:
        removed["kept (used by a bill)"] = kept

    try:
        from services import audit_service

        audit_service.record(
            session, company.id, CLEAR_ACTION,
            "Demo sample data removed: "
            + (", ".join(f"{n} {label}" for label, n in removed.items()) or "nothing"),
            user=user,
        )
    except Exception:
        log.exception("Could not write the sample-removal audit entry")

    return removed
