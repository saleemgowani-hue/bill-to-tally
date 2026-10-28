"""Clearing a demo must not crash on a foreign key.

A customer pressed **Clear demo data** and got a stack trace:

    sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError)
    FOREIGN KEY constraint failed
    [SQL: DELETE FROM products WHERE products.company_id = ? AND ...]

The app switches foreign keys on in SQLite (they are off by default), so a
product cannot be deleted while an alias or a stock movement still points at
it — and the sweep was deleting products before either.

Deleting by timestamp alone never covered this. A manual stock adjustment
made during a demo has no invoice, so the invoice pass misses it and it
outlives the product it references. The same gap left a processing error and
a duplicate-bill link pointing at an invoice that was about to be deleted.

These tests build each of those situations and clear the demo. They are about
one thing: the button must work, on every shape of demo data, without the
presenter having to know which order things were created in.
"""
import datetime as dt

import pytest

from database.models import (
    AuditLog, Invoice, InvoiceItem, InvoiceStatus, ProcessingError, Product,
    ProductAlias, PurchaseTransaction, StockMovement, Supplier,
)
from services import demo_service
from tests.conftest import make_product


def as_demo(session, company):
    """Flag as a demo with the baseline a moment ago.

    Only rows created *after* the baseline are swept, so anything the test
    creates next is demo data and anything created before it is 'seeded'.
    """
    demo_service.mark_as_demo(session, company)
    company.demo_baseline_at = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    )
    session.commit()
    return company


class TestProductReferences:
    def test_an_alias_does_not_block_its_product(self, session, company):
        """The matcher writes an alias whenever a name is mapped, so almost
        every demo that touches product matching creates one."""
        as_demo(session, company)
        product = make_product(session, company, "Balaji Namkeen")
        session.add(ProductAlias(
            company_id=company.id, product_id=product.id,
            alias_raw="BALAJI NAMKIN", alias_normalized="balaji namkin",
        ))
        session.commit()
        # Held before the clear: afterwards the object is gone, and reading an
        # attribute off it raises rather than handing back a stale value.
        product_id = product.id

        result = demo_service.clear_demo_data(session, company)

        assert session.query(Product).filter(
            Product.id == product_id
        ).one_or_none() is None
        assert result.total_deleted >= 2

    def test_a_manual_stock_adjustment_does_not_block_its_product(
        self, session, company
    ):
        """This is the one that reached a customer: an adjustment made from
        the Products screen has no invoice, so the invoice pass never sees
        it."""
        as_demo(session, company)
        product = make_product(session, company, "Balaji Namkeen")
        session.add(StockMovement(
            company_id=company.id, product_id=product.id,
            quantity_before=0, quantity_change=10, quantity_after=10,
            reason="Manual adjustment during the demo",
        ))
        session.commit()
        product_id = product.id

        demo_service.clear_demo_data(session, company)

        assert session.query(Product).filter(
            Product.id == product_id
        ).one_or_none() is None
        assert session.query(StockMovement).filter(
            StockMovement.company_id == company.id
        ).count() == 0

    def test_a_bill_line_keeps_its_figures_when_a_product_goes(
        self, session, company
    ):
        """A nullable reference is cleared, not followed.

        Deleting an invoice line to remove a product would silently change
        that bill's totals, and a bill whose figures move on their own is
        worse than a stale link.
        """
        seeded_invoice = Invoice(
            company_id=company.id, supplier_name="Old supplier",
            invoice_number="OLD-1", grand_total=500.0,
            status=InvoiceStatus.POSTED.value,
        )
        session.add(seeded_invoice)
        session.commit()
        # Explicitly older than the baseline: this bill was there before the
        # demo began, so the sweep must leave it alone.
        seeded_invoice.created_at = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
        ).replace(tzinfo=None)
        session.commit()

        as_demo(session, company)
        product = make_product(session, company, "Added during the demo")
        line = InvoiceItem(
            company_id=company.id, invoice_id=seeded_invoice.id,
            product_id=product.id, raw_name="Something", quantity=2,
            rate=250.0, total=500.0,
        )
        session.add(line)
        session.commit()
        line_id = line.id

        demo_service.clear_demo_data(session, company)

        kept = session.query(InvoiceItem).filter(
            InvoiceItem.id == line_id
        ).one_or_none()
        # The line itself belongs to a bill from before the demo, so it stays;
        # only its pointer at the removed product is cleared.
        assert kept is not None
        assert kept.product_id is None
        assert kept.total == 500.0


class TestInvoiceReferences:
    def test_a_processing_error_does_not_block_its_invoice(
        self, session, company
    ):
        """Every failed upload during a demo writes one of these."""
        as_demo(session, company)
        invoice = Invoice(
            company_id=company.id, supplier_name="Balaji Traders",
            invoice_number="B-1", grand_total=100.0,
            status=InvoiceStatus.FAILED.value,
        )
        session.add(invoice)
        session.commit()
        session.add(ProcessingError(
            company_id=company.id, invoice_id=invoice.id,
            stage="ocr", message="Could not read the image",
        ))
        session.commit()

        demo_service.clear_demo_data(session, company)

        assert session.query(Invoice).filter(
            Invoice.company_id == company.id
        ).count() == 0

    def test_a_duplicate_link_does_not_block_its_invoice(self, session, company):
        """Uploading the same bill twice is a standard part of the demo, so
        `duplicate_of_id` is set more often than anywhere else."""
        as_demo(session, company)
        original = Invoice(
            company_id=company.id, supplier_name="Balaji Traders",
            invoice_number="B-1", grand_total=100.0,
            status=InvoiceStatus.POSTED.value,
        )
        session.add(original)
        session.commit()
        session.add(Invoice(
            company_id=company.id, supplier_name="Balaji Traders",
            invoice_number="B-1", grand_total=100.0,
            status=InvoiceStatus.DUPLICATE.value, duplicate_of_id=original.id,
        ))
        session.commit()

        demo_service.clear_demo_data(session, company)

        assert session.query(Invoice).filter(
            Invoice.company_id == company.id
        ).count() == 0

    def test_a_full_posted_bill_clears(self, session, company):
        """The ordinary path, with every child table populated at once."""
        as_demo(session, company)
        supplier = Supplier(company_id=company.id, name="Balaji Traders")
        product = make_product(session, company, "Balaji Namkeen")
        session.add(supplier)
        session.commit()

        invoice = Invoice(
            company_id=company.id, supplier_id=supplier.id,
            supplier_name="Balaji Traders", invoice_number="B-7",
            grand_total=4820.0, status=InvoiceStatus.POSTED.value,
        )
        session.add(invoice)
        session.commit()
        session.add_all([
            InvoiceItem(company_id=company.id, invoice_id=invoice.id,
                        product_id=product.id, raw_name="Namkeen",
                        quantity=10, rate=482.0, total=4820.0),
            PurchaseTransaction(company_id=company.id, invoice_id=invoice.id,
                                idempotency_key="demo-1"),
            StockMovement(company_id=company.id, product_id=product.id,
                          invoice_id=invoice.id, quantity_before=0,
                          quantity_change=10, quantity_after=10),
            ProductAlias(company_id=company.id, product_id=product.id,
                         supplier_id=supplier.id, alias_raw="NAMKEEN",
                         alias_normalized="namkeen"),
            AuditLog(company_id=company.id, invoice_id=invoice.id,
                     action="POSTED", detail="demo"),
        ])
        session.commit()

        demo_service.clear_demo_data(session, company)

        for model in (Invoice, InvoiceItem, PurchaseTransaction,
                      StockMovement, ProductAlias, Product, Supplier):
            assert session.query(model).filter(
                model.company_id == company.id
            ).count() == 0, f"{model.__name__} survived the clear"


class TestWhatSurvives:
    def test_seeded_products_survive_and_go_back_to_opening_stock(
        self, session, company
    ):
        seeded = make_product(session, company, "Parle-G", current_stock=120)
        demo_service.mark_as_demo(session, company)      # baseline = now
        seeded.current_stock = 144                        # a demo purchase
        session.commit()

        demo_service.clear_demo_data(session, company)

        session.refresh(seeded)
        assert seeded.current_stock == 120

    def test_a_real_company_is_untouched_by_a_demo_clear(
        self, session, company, other_company
    ):
        """The guarantee the whole feature rests on, re-checked here because
        this fix rewrote the delete logic."""
        theirs = make_product(session, other_company, "Their product")
        session.add(StockMovement(
            company_id=other_company.id, product_id=theirs.id,
            quantity_before=0, quantity_change=5, quantity_after=5,
        ))
        session.commit()

        as_demo(session, company)
        make_product(session, company, "Demo product")
        demo_service.clear_demo_data(session, company)

        assert session.query(Product).filter(Product.id == theirs.id).one()
        assert session.query(StockMovement).filter(
            StockMovement.company_id == other_company.id
        ).count() == 1


class TestTheHelperItself:
    def test_it_reads_the_schema_rather_than_a_hand_written_list(self):
        """The point of doing it this way: a table added next year is handled
        without anyone remembering to update the sweep."""
        import inspect

        source = inspect.getsource(demo_service._detach)
        assert "Base.metadata.sorted_tables" in source
        assert "foreign_keys" in source

    def test_nothing_to_detach_is_not_an_error(self, session):
        result = demo_service.SweepResult()
        demo_service._detach(session, "products", [], result)
        assert result.total_deleted == 0

    def test_it_handles_more_ids_than_sqlite_takes_in_one_statement(
        self, session, company
    ):
        """SQLite caps the parameters in a single statement, so the ids are
        chunked. A big demo must not fail on its size."""
        as_demo(session, company)
        products = [
            make_product(session, company, f"Product {n}") for n in range(450)
        ]
        session.add_all([
            StockMovement(company_id=company.id, product_id=p.id,
                          quantity_before=0, quantity_change=1, quantity_after=1)
            for p in products
        ])
        session.commit()

        demo_service.clear_demo_data(session, company)

        assert session.query(Product).filter(
            Product.company_id == company.id
        ).count() == 0
