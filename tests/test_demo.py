"""Tests for demo-account auto-cleanup."""
import datetime as dt

import pytest

from PIL import Image

from database.models import Invoice, InvoiceItem, Product, Supplier
from services import demo_service
from services.ai_invoice_parser import InvoiceParser
from services.ocr_service import MockOCRProvider
from services.purchase_service import ingest_bill
from tests.conftest import make_product


def age_rows(session, model, ids, minutes):
    """Backdate created_at so we can test the retention window without waiting."""
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    for row in session.query(model).filter(model.id.in_(ids)).all():
        row.created_at = when
    session.commit()


@pytest.fixture
def timer_on(monkeypatch):
    """Switch on the optional auto-wipe timer.

    It is off by default — a demo is cleared deliberately between prospects —
    so the tests that exercise the timer have to ask for it.
    """
    from utils.config import settings

    monkeypatch.setattr(settings, "DEMO_RETENTION_MINUTES", 60)
    return 60


def mark_demo(session, company, baseline_minutes_ago=1440):
    """Flag as demo, with the baseline pushed back to mimic a demo that was
    installed a while ago. Only data created AFTER the baseline is swept, so a
    baseline set 'now' would protect everything and prove nothing."""
    demo_service.mark_as_demo(session, company)
    company.demo_baseline_at = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=baseline_minutes_ago)
    )
    session.commit()


def ingest(session, company, user, data=b"\xff\xd8\xffdemo"):
    return ingest_bill(
        session, company, user, "bill.jpg", data, [Image.new("RGB", (400, 600), "white")],
        InvoiceParser(MockOCRProvider()), store_image=False,
    )


class TestMarking:
    def test_company_is_flagged(self, session, company):
        demo_service.mark_as_demo(session, company)
        assert demo_service.is_demo(company)
        assert company.demo_baseline_at is not None

    def test_normal_company_is_not_demo(self, session, company):
        assert not demo_service.is_demo(company)

    def test_sweep_ignores_non_demo_companies(self, session, company, user):
        result = ingest(session, company, user)
        invoice_id = result.invoice.id
        age_rows(session, Invoice, [invoice_id], 500)
        demo_service.sweep_company(session, company)
        assert session.query(Invoice).filter(Invoice.id == invoice_id).one_or_none()


class TestRetentionWindow:
    def test_recent_data_survives(self, session, company, user, timer_on):
        mark_demo(session, company)
        result = ingest(session, company, user)
        demo_service.sweep_company(session, company)
        assert session.query(Invoice).filter(Invoice.id == result.invoice.id).one_or_none()

    def test_data_older_than_the_window_is_deleted(self, session, company, user, timer_on):
        mark_demo(session, company)
        result = ingest(session, company, user)
        invoice_id = result.invoice.id
        age_rows(session, Invoice, [invoice_id], 61)

        report = demo_service.sweep_company(session, company)
        assert report.ran
        assert session.query(Invoice).filter(Invoice.id == invoice_id).one_or_none() is None
        assert report.deleted.get("invoices") == 1

    def test_child_rows_go_with_the_invoice(self, session, company, user, timer_on):
        mark_demo(session, company)
        result = ingest(session, company, user)
        invoice_id = result.invoice.id
        assert session.query(InvoiceItem).filter(
            InvoiceItem.invoice_id == invoice_id
        ).count() > 0

        age_rows(session, Invoice, [invoice_id], 61)
        demo_service.sweep_company(session, company)
        assert session.query(InvoiceItem).filter(
            InvoiceItem.invoice_id == invoice_id
        ).count() == 0

    def test_boundary_just_under_the_window(self, session, company, user, timer_on):
        mark_demo(session, company)
        result = ingest(session, company, user)
        age_rows(session, Invoice, [result.invoice.id], 59)
        demo_service.sweep_company(session, company)
        assert session.query(Invoice).filter(Invoice.id == result.invoice.id).one_or_none()


class TestSeededDataIsProtected:
    def test_seeded_products_survive(self, session, company, user):
        product = make_product(session, company, "Seeded Product", current_stock=100.0)
        demo_service.mark_as_demo(session, company)

        age_rows(session, Product, [product.id], 500)
        demo_service.sweep_company(session, company, force=True)
        assert session.query(Product).filter(Product.id == product.id).one_or_none()

    def test_visitor_created_products_are_removed(self, session, company, user, timer_on):
        mark_demo(session, company)
        new_product = make_product(session, company, "Added During Demo")
        product_id = new_product.id
        age_rows(session, Product, [product_id], 61)

        demo_service.sweep_company(session, company)
        assert session.query(Product).filter(
            Product.id == product_id
        ).one_or_none() is None

    def test_stock_is_restored_to_baseline(self, session, company, user):
        product = make_product(session, company, "Stocked Item", current_stock=100.0)
        demo_service.mark_as_demo(session, company)

        product.current_stock = 250.0     # a visitor posted a bill
        session.commit()

        demo_service.sweep_company(session, company, force=True)
        session.refresh(product)
        assert product.current_stock == 100.0

    def test_users_are_never_deleted(self, session, company, user):
        demo_service.mark_as_demo(session, company)
        demo_service.sweep_company(session, company, force=True)
        from database.models import User

        assert session.query(User).filter(User.id == user.id).one_or_none()

    def test_company_is_never_deleted(self, session, company):
        demo_service.mark_as_demo(session, company)
        demo_service.sweep_company(session, company, force=True)
        from database.models import Company

        assert session.query(Company).filter(Company.id == company.id).one_or_none()


class TestCountdown:
    def test_none_when_nothing_to_wipe(self, session, company):
        demo_service.mark_as_demo(session, company)
        assert demo_service.minutes_until_wipe(session, company) is None

    def test_counts_down_from_the_window(self, session, company, user, timer_on):
        mark_demo(session, company)
        ingest(session, company, user)
        remaining = demo_service.minutes_until_wipe(session, company)
        assert remaining is not None and 55 <= remaining <= 60

    def test_zero_when_overdue(self, session, company, user, timer_on):
        mark_demo(session, company)
        result = ingest(session, company, user)
        age_rows(session, Invoice, [result.invoice.id], 90)
        assert demo_service.minutes_until_wipe(session, company) == 0


class TestTenantSafety:
    def test_sweeping_a_demo_leaves_other_companies_alone(
        self, session, company, other_company, user
    ):
        demo_service.mark_as_demo(session, company)
        keeper = make_product(session, other_company, "Real Customer Product")
        age_rows(session, Product, [keeper.id], 500)

        demo_service.sweep_company(session, company, force=True)
        assert session.query(Product).filter(Product.id == keeper.id).one_or_none()

    def test_sweep_all_only_touches_demo_companies(
        self, session, company, other_company, user, timer_on
    ):
        demo_service.mark_as_demo(session, company)
        result = ingest(session, other_company, user_for(session, other_company))
        age_rows(session, Invoice, [result.invoice.id], 500)

        demo_service.sweep_all(session)
        assert session.query(Invoice).filter(Invoice.id == result.invoice.id).one_or_none()


def user_for(session, company):
    from database.models import User

    return session.query(User).filter(User.company_id == company.id).first()


class TestAutoWipeIsOffByDefault:
    """A countdown that empties the screen mid-presentation is worse than no
    housekeeping at all, so the timer ships switched off."""

    def test_default_is_no_timer(self):
        from utils.config import settings

        assert settings.DEMO_RETENTION_MINUTES == 0
        assert demo_service.auto_wipe_enabled() is False

    def test_env_example_ships_with_the_timer_off(self):
        from pathlib import Path

        example = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
        assert "DEMO_RETENTION_MINUTES=0" in example

    def test_zero_retention_never_sweeps_on_a_timer(self, session, company, user):
        """Retention 0 must mean 'never', not 'delete everything now'."""
        mark_demo(session, company)
        result = ingest(session, company, user)
        demo_service.sweep_company(session, company)          # timed sweep
        assert session.query(Invoice).filter(Invoice.id == result.invoice.id).one_or_none()

    def test_sweep_all_does_nothing_with_the_timer_off(self, session, company, user):
        mark_demo(session, company)
        result = ingest(session, company, user)
        age_rows(session, Invoice, [result.invoice.id], 5000)
        demo_service.sweep_all(session)
        assert session.query(Invoice).filter(Invoice.id == result.invoice.id).one_or_none()

    def test_no_countdown_is_shown(self, session, company, user):
        mark_demo(session, company)
        ingest(session, company, user)
        assert demo_service.minutes_until_wipe(session, company) is None


class TestManualClear:
    """The button used between prospects."""

    def test_clears_bills_entered_during_the_demo(self, session, company, user):
        mark_demo(session, company)
        result = ingest(session, company, user)
        invoice_id = result.invoice.id

        report = demo_service.clear_demo_data(session, company, user)
        assert report.ran
        assert session.query(Invoice).filter(Invoice.id == invoice_id).one_or_none() is None

    def test_clears_immediately_with_no_waiting(self, session, company, user):
        """Data entered seconds ago must go — the demo just finished."""
        mark_demo(session, company)
        result = ingest(session, company, user)
        invoice_id = result.invoice.id
        demo_service.clear_demo_data(session, company, user)
        assert session.query(Invoice).filter(
            Invoice.id == invoice_id
        ).one_or_none() is None

    def test_seeded_products_survive_and_reset_to_opening_stock(self, session, company, user):
        # Mirrors seed_demo.py: products created, then the demo baseline set,
        # so the seeded master sits *before* the baseline and is protected.
        product = make_product(session, company, "Seeded Item", current_stock=120.0)
        demo_service.mark_as_demo(session, company)
        product.current_stock = 260.0          # a demo posting moved it
        session.commit()

        report = demo_service.clear_demo_data(session, company, user)
        session.refresh(product)
        assert session.query(Product).filter(Product.id == product.id).one_or_none()
        assert product.current_stock == 120.0
        assert report.stock_reset >= 1

    def test_users_and_company_survive(self, session, company, user):
        from database.models import Company, User

        mark_demo(session, company)
        ingest(session, company, user)
        demo_service.clear_demo_data(session, company, user)
        assert session.query(User).filter(User.id == user.id).one_or_none()
        assert session.query(Company).filter(Company.id == company.id).one_or_none()

    def test_licence_survives(self, session, company, user):
        from services import license_service

        license_service.activate(
            session, company, license_service.generate_key("YEARLY"), activated_by=user.id
        )
        plan = company.license_plan
        expiry = demo_service._aware(company.license_expires_at)
        mark_demo(session, company)
        demo_service.clear_demo_data(session, company, user)

        assert company.license_plan == plan
        # Compared through _aware() because the value is timezone-aware while
        # it is the one just assigned in memory, and naive once it has been
        # read back from SQLite, which has no datetime type. Both describe the
        # same instant; every caller that does arithmetic on it normalises the
        # same way.
        assert demo_service._aware(company.license_expires_at) == expiry

    def test_is_audited(self, session, company, user):
        from database.models import AuditLog

        mark_demo(session, company)
        ingest(session, company, user)
        demo_service.clear_demo_data(session, company, user)
        actions = {
            row.action for row in session.query(AuditLog).filter(
                AuditLog.company_id == company.id
            ).all()
        }
        assert "DEMO_DATA_CLEARED" in actions

    def test_clearing_twice_is_harmless(self, session, company, user):
        mark_demo(session, company)
        ingest(session, company, user)
        demo_service.clear_demo_data(session, company, user)
        second = demo_service.clear_demo_data(session, company, user)
        assert second.total_deleted == 0


class TestRealAccountsAreUntouchable:
    """The guard that matters: a paying customer's books must be unreachable
    from the demo-clearing code, whatever the caller does."""

    def test_clearing_a_real_company_raises(self, session, company, user):
        assert not demo_service.is_demo(company)
        with pytest.raises(demo_service.NotADemoCompany):
            demo_service.clear_demo_data(session, company, user)

    def test_a_real_companys_bills_survive_the_attempt(self, session, company, user):
        result = ingest(session, company, user)
        with pytest.raises(demo_service.NotADemoCompany):
            demo_service.clear_demo_data(session, company, user)
        assert session.query(Invoice).filter(Invoice.id == result.invoice.id).one_or_none()

    def test_the_error_names_the_company(self, session, company, user):
        with pytest.raises(demo_service.NotADemoCompany, match=company.name):
            demo_service.clear_demo_data(session, company, user)

    def test_none_company_raises(self, session):
        with pytest.raises(demo_service.NotADemoCompany):
            demo_service.clear_demo_data(session, None, None)

    def test_clearing_a_demo_leaves_a_real_company_alone(
        self, session, company, other_company, user
    ):
        """The two live in one database. Clearing one must not reach the other."""
        real_user = user_for(session, other_company)
        real_bill = ingest(session, other_company, real_user)
        keeper = make_product(session, other_company, "Real Stock", current_stock=45.0)

        mark_demo(session, company)
        demo_bill = ingest(session, company, user)
        demo_invoice_id = demo_bill.invoice.id
        demo_service.clear_demo_data(session, company, user)

        assert session.query(Invoice).filter(
            Invoice.id == demo_invoice_id
        ).one_or_none() is None
        assert session.query(Invoice).filter(Invoice.id == real_bill.invoice.id).one_or_none()
        session.refresh(keeper)
        assert keeper.current_stock == 45.0

    def test_signing_up_never_creates_a_demo_company(self, session):
        """A customer's own company must never carry the demo flag."""
        import uuid

        from database.database import create_company_with_admin
        from database.models import Company

        owner = create_company_with_admin(
            session, f"Real Shop {uuid.uuid4().hex[:6]}", "Owner",
            f"{uuid.uuid4().hex[:8]}@shop.local", "Passw0rd123",
        )
        fresh = session.query(Company).filter(Company.id == owner.company_id).one()
        assert not demo_service.is_demo(fresh)
        with pytest.raises(demo_service.NotADemoCompany):
            demo_service.clear_demo_data(session, fresh, owner)


class TestSummary:
    def test_counts_what_would_be_removed(self, session, company, user):
        mark_demo(session, company)
        ingest(session, company, user)
        summary = demo_service.demo_data_summary(session, company)
        assert summary.get("bills") == 1

    def test_empty_for_a_fresh_demo(self, session, company):
        mark_demo(session, company)
        assert demo_service.demo_data_summary(session, company) == {}

    def test_empty_for_a_real_company(self, session, company, user):
        ingest(session, company, user)
        assert demo_service.demo_data_summary(session, company) == {}
