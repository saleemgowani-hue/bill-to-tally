"""End-to-end workflow tests: ingest → confirm → post → stock, plus safety rails."""
import pytest
from PIL import Image

from database.models import (
    AuditLog,
    InvoiceItem,
    InvoiceStatus,
    Product,
    PurchaseTransaction,
    Role,
    StockMovement,
    User,
)
from services.ai_invoice_parser import InvoiceParser
from services.invoice_validator import check_duplicate
from services.ocr_service import MockOCRProvider
from services.purchase_service import (
    confirm_invoice,
    idempotency_key,
    ingest_bill,
    post_to_tally,
)
from services.tally_connector import MockTallyConnector
from tests.conftest import make_product


def blank_image():
    return Image.new("RGB", (600, 900), "white")


def ingest(session, company, user, name="bill.jpg", data=b"\xff\xd8\xffdemo"):
    return ingest_bill(
        session, company, user, name, data, [blank_image()],
        InvoiceParser(MockOCRProvider()), store_image=False,
    )


class TestIngestion:
    def test_creates_invoice_and_items(self, session, company, user):
        result = ingest(session, company, user)
        assert result.invoice.id is not None
        assert result.invoice.supplier_name == "Shreeji Distributors"
        items = session.query(InvoiceItem).filter(
            InvoiceItem.invoice_id == result.invoice.id
        ).all()
        assert len(items) == 3

    def test_lands_in_review_required(self, session, company, user):
        result = ingest(session, company, user)
        assert result.invoice.status == InvoiceStatus.REVIEW_REQUIRED.value

    def test_never_posts_automatically(self, session, company, user):
        result = ingest(session, company, user)
        assert result.invoice.status != InvoiceStatus.POSTED.value
        assert session.query(PurchaseTransaction).filter(
            PurchaseTransaction.invoice_id == result.invoice.id
        ).count() == 0

    def test_no_stock_moves_on_ingest(self, session, company, user):
        before = session.query(StockMovement).count()
        ingest(session, company, user)
        assert session.query(StockMovement).count() == before

    def test_supplier_is_created(self, session, company, user):
        result = ingest(session, company, user)
        assert result.invoice.supplier_id is not None

    def test_audit_trail_is_written(self, session, company, user):
        result = ingest(session, company, user)
        actions = {
            a.action
            for a in session.query(AuditLog).filter(
                AuditLog.invoice_id == result.invoice.id
            ).all()
        }
        assert "BILL_UPLOADED" in actions
        assert "AI_EXTRACTED" in actions

    def test_low_confidence_line_is_flagged(self, session, company, user):
        result = ingest(session, company, user)
        # the mock bill deliberately contains one line at 0.64 confidence
        assert any((i.get("confidence") or 0) < 0.7 for i in result.parsed.items)
        assert result.report.is_blocking, "A sub-threshold line must force review"


class TestDuplicates:
    def test_same_file_is_flagged(self, session, company, user):
        ingest(session, company, user, data=b"\xff\xd8\xffidentical")
        second = ingest(session, company, user, data=b"\xff\xd8\xffidentical")
        assert second.invoice.status == InvoiceStatus.DUPLICATE.value
        assert second.duplicate_reason

    def test_same_invoice_number_is_flagged(self, session, company, user):
        ingest(session, company, user, data=b"\xff\xd8\xffone")
        second = ingest(session, company, user, data=b"\xff\xd8\xfftwo")
        assert second.invoice.status == InvoiceStatus.DUPLICATE.value

    def test_check_duplicate_returns_clean_for_new_bill(self, session, company):
        result = check_duplicate(
            session, company.id, "Brand New Supplier", "XYZ-999", "2026-01-01", 500.0
        )
        assert not result.is_duplicate


class TestConfirmation:
    def _prepare(self, session, company, user):
        """Ingest, link every line to a product and clean the numbers."""
        result = ingest(session, company, user)
        invoice = result.invoice
        items = session.query(InvoiceItem).filter(
            InvoiceItem.invoice_id == invoice.id
        ).all()
        for item in items:
            product = make_product(session, company, item.raw_name, unit=item.unit or "PCS")
            item.product_id = product.id
            item.confidence = 0.99
        invoice.overall_confidence = 0.99
        session.commit()
        return invoice, items

    def test_blocked_when_a_line_has_no_product(self, session, company, user):
        result = ingest(session, company, user)
        ok, message = confirm_invoice(session, company, user, result.invoice)
        assert not ok
        assert "not linked to a product" in message

    def test_operator_cannot_confirm(self, session, company, user):
        invoice, _ = self._prepare(session, company, user)
        operator = User(
            company_id=company.id, full_name="Op", email="op@test.local",
            password_hash="x", role=Role.OPERATOR.value,
        )
        session.add(operator)
        session.commit()
        ok, message = confirm_invoice(session, company, operator, invoice)
        assert not ok
        assert "role" in message.lower()

    def test_blocking_errors_need_an_override_reason(self, session, company, user):
        invoice, items = self._prepare(session, company, user)
        items[0].total = 999999.0  # break reconciliation
        session.commit()
        ok, _ = confirm_invoice(session, company, user, invoice)
        assert not ok
        ok, _ = confirm_invoice(session, company, user, invoice, "checked against the paper bill")
        assert ok

    def test_override_reason_is_audited(self, session, company, user):
        invoice, items = self._prepare(session, company, user)
        items[0].total = 999999.0
        session.commit()
        confirm_invoice(session, company, user, invoice, "manager approved")
        logs = session.query(AuditLog).filter(
            AuditLog.invoice_id == invoice.id, AuditLog.action == "INVOICE_CONFIRMED"
        ).all()
        assert any("OVERRIDE" in (l.detail or "") for l in logs)


class TestPostingAndStock:
    def _confirmed(self, session, company, user):
        result = ingest(session, company, user)
        invoice = result.invoice
        items = session.query(InvoiceItem).filter(
            InvoiceItem.invoice_id == invoice.id
        ).all()
        for item in items:
            product = make_product(
                session, company, item.raw_name, unit=item.unit or "PCS", current_stock=120.0
            )
            item.product_id = product.id
            item.confidence = 0.99
        invoice.overall_confidence = 0.99
        session.commit()
        ok, message = confirm_invoice(session, company, user, invoice, "test fixture")
        assert ok, message
        return invoice, items

    def test_stock_increases_after_posting(self, session, company, user):
        invoice, items = self._confirmed(session, company, user)
        first_item = items[0]
        product = session.query(Product).filter(Product.id == first_item.product_id).one()
        before = product.current_stock

        result, transaction = post_to_tally(
            session, company, user, invoice,
            MockTallyConnector({"company": company.name}), run_preflight=False,
        )
        assert result.success
        session.refresh(product)
        assert product.current_stock == before + first_item.quantity
        assert invoice.status == InvoiceStatus.POSTED.value
        assert transaction.stock_applied

    def test_posting_twice_does_not_double_stock(self, session, company, user):
        invoice, items = self._confirmed(session, company, user)
        connector = MockTallyConnector({"company": company.name})
        post_to_tally(session, company, user, invoice, connector, run_preflight=False)

        product = session.query(Product).filter(Product.id == items[0].product_id).one()
        after_first = product.current_stock

        invoice.status = InvoiceStatus.CONFIRMED.value  # simulate a retry
        session.commit()
        result, _ = post_to_tally(session, company, user, invoice, connector, run_preflight=False)

        session.refresh(product)
        assert not result.success
        assert "already been posted" in result.message
        assert product.current_stock == after_first

    def test_unconfirmed_invoice_cannot_be_posted(self, session, company, user):
        result = ingest(session, company, user)
        posted, _ = post_to_tally(
            session, company, user, result.invoice,
            MockTallyConnector(), run_preflight=False,
        )
        assert not posted.success

    def test_operator_cannot_post(self, session, company, user):
        invoice, _ = self._confirmed(session, company, user)
        operator = User(
            company_id=company.id, full_name="Op2", email="op2@test.local",
            password_hash="x", role=Role.OPERATOR.value,
        )
        session.add(operator)
        session.commit()
        result, _ = post_to_tally(
            session, company, operator, invoice, MockTallyConnector(), run_preflight=False
        )
        assert not result.success

    def test_stock_movements_are_recorded(self, session, company, user):
        invoice, items = self._confirmed(session, company, user)
        post_to_tally(
            session, company, user, invoice,
            MockTallyConnector(), run_preflight=False,
        )
        movements = session.query(StockMovement).filter(
            StockMovement.invoice_id == invoice.id
        ).all()
        assert len(movements) == len(items)
        for movement in movements:
            assert movement.quantity_after == movement.quantity_before + movement.quantity_change

    def test_failed_preflight_posts_nothing(self, session, company, user):
        invoice, items = self._confirmed(session, company, user)
        product = session.query(Product).filter(Product.id == items[0].product_id).one()
        before = product.current_stock
        # the mock connector knows nothing about these ledgers/items
        result, transaction = post_to_tally(
            session, company, user, invoice, MockTallyConnector(), run_preflight=True
        )
        assert not result.success
        assert transaction is None
        session.refresh(product)
        assert product.current_stock == before
        assert invoice.status == InvoiceStatus.FAILED.value


class TestIdempotencyKey:
    def test_stable_for_the_same_bill(self, session, company, user):
        result = ingest(session, company, user)
        first = idempotency_key(company.id, result.invoice)
        second = idempotency_key(company.id, result.invoice)
        assert first == second

    def test_differs_across_companies(self, session, company, other_company, user):
        result = ingest(session, company, user)
        assert idempotency_key(company.id, result.invoice) != idempotency_key(
            other_company.id, result.invoice
        )


class TestFileExportMode:
    """Writing an XML file is not Tally accepting it. Stock must wait for a
    person to confirm the import, or our stock and Tally silently diverge."""

    def _confirmed(self, session, company, user):
        result = ingest(session, company, user, data=b"\xff\xd8\xffexport")
        invoice = result.invoice
        items = session.query(InvoiceItem).filter(
            InvoiceItem.invoice_id == invoice.id
        ).all()
        for item in items:
            product = make_product(
                session, company, item.raw_name, unit=item.unit or "PCS",
                current_stock=100.0,
            )
            item.product_id = product.id
            item.confidence = 0.99
        invoice.overall_confidence = 0.99
        session.commit()
        ok, message = confirm_invoice(session, company, user, invoice, "test fixture")
        assert ok, message
        return invoice, items

    def test_export_does_not_post_or_move_stock(self, session, company, user):
        from database.models import InvoiceStatus
        from services.tally_connector import FileExportConnector

        invoice, items = self._confirmed(session, company, user)
        before = session.query(Product).filter(
            Product.id == items[0].product_id
        ).one().current_stock

        result, transaction = post_to_tally(
            session, company, user, invoice, FileExportConnector(), run_preflight=False
        )
        assert result.success
        assert result.mode == "XML_EXPORT"
        assert invoice.status == InvoiceStatus.EXPORTED.value
        assert transaction.stock_applied is False

        product = session.query(Product).filter(Product.id == items[0].product_id).one()
        session.refresh(product)
        assert product.current_stock == before

    def test_warning_tells_the_user_stock_is_pending(self, session, company, user):
        from services.tally_connector import FileExportConnector

        invoice, _ = self._confirmed(session, company, user)
        result, _ = post_to_tally(
            session, company, user, invoice, FileExportConnector(), run_preflight=False
        )
        assert any("NOT been updated" in w for w in result.warnings)

    def test_confirming_the_import_moves_stock(self, session, company, user):
        from database.models import InvoiceStatus
        from services.purchase_service import confirm_tally_import
        from services.tally_connector import FileExportConnector

        invoice, items = self._confirmed(session, company, user)
        post_to_tally(
            session, company, user, invoice, FileExportConnector(), run_preflight=False
        )
        product = session.query(Product).filter(Product.id == items[0].product_id).one()
        before = product.current_stock

        ok, message = confirm_tally_import(session, company, user, invoice)
        assert ok, message
        session.refresh(product)
        assert product.current_stock == before + items[0].quantity
        assert invoice.status == InvoiceStatus.POSTED.value

    def test_confirming_twice_does_not_double_stock(self, session, company, user):
        from services.purchase_service import confirm_tally_import
        from services.tally_connector import FileExportConnector

        invoice, items = self._confirmed(session, company, user)
        post_to_tally(
            session, company, user, invoice, FileExportConnector(), run_preflight=False
        )
        confirm_tally_import(session, company, user, invoice)
        product = session.query(Product).filter(Product.id == items[0].product_id).one()
        after_first = product.current_stock

        ok, _ = confirm_tally_import(session, company, user, invoice)
        session.refresh(product)
        assert not ok
        assert product.current_stock == after_first

    def test_cannot_confirm_an_invoice_that_was_never_exported(
        self, session, company, user
    ):
        from services.purchase_service import confirm_tally_import

        invoice, _ = self._confirmed(session, company, user)
        ok, message = confirm_tally_import(session, company, user, invoice)
        assert not ok
        assert "exported" in message.lower()

    def test_operator_cannot_confirm_an_import(self, session, company, user):
        from database.models import Role, User as UserModel
        from services.purchase_service import confirm_tally_import
        from services.tally_connector import FileExportConnector

        invoice, _ = self._confirmed(session, company, user)
        post_to_tally(
            session, company, user, invoice, FileExportConnector(), run_preflight=False
        )
        operator = UserModel(
            company_id=company.id, full_name="Op3", email="op3@test.local",
            password_hash="x", role=Role.OPERATOR.value,
        )
        session.add(operator)
        session.commit()
        ok, _ = confirm_tally_import(session, company, operator, invoice)
        assert not ok

    def test_re_exporting_does_not_duplicate_stock(self, session, company, user):
        from services.tally_connector import FileExportConnector

        invoice, items = self._confirmed(session, company, user)
        connector = FileExportConnector()
        post_to_tally(session, company, user, invoice, connector, run_preflight=False)
        post_to_tally(session, company, user, invoice, connector, run_preflight=False)

        product = session.query(Product).filter(Product.id == items[0].product_id).one()
        session.refresh(product)
        assert product.current_stock == 100.0   # untouched by either export

    def test_live_post_still_moves_stock_immediately(self, session, company, user):
        """The fix must not change HTTP or mock mode."""
        from database.models import InvoiceStatus

        invoice, items = self._confirmed(session, company, user)
        result, transaction = post_to_tally(
            session, company, user, invoice,
            MockTallyConnector({"company": company.name}), run_preflight=False,
        )
        assert result.success
        assert invoice.status == InvoiceStatus.POSTED.value
        assert transaction.stock_applied
