"""A bill the customer entered must be visible, or the app must say where it is.

Three complaints from the same afternoon, all of them about things the screen
would not explain:

  * "1 bill balaji ka add kiya but report me show nahi ho raha" — the bill was
    real, the report said "No posted bills yet", and nothing on screen said
    which button would move it along.
  * "supplier me shreeji distributor ki entry dikh rahi hai jo upload kiya hi
    nahi" — a seeded sample looked identical to something uploaded.
  * "connection se excel sheet ka option remove ho gaya" — an old build, with
    no way to tell it apart from the current one.

These tests pin the three answers: reports count exported bills as entered and
name what is waiting, sample rows are labelled and removable, and the build
carries a version anyone can read off the screen.
"""
import datetime as dt

import pytest

from database.models import (
    ACCEPTED_STATUSES,
    NEXT_STEP,
    PENDING_STATUSES,
    RECORDED_STATUSES,
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    Product,
    Supplier,
)
from services import demo_service
from tests.conftest import make_product


def add_invoice(session, company, status, *, supplier_name="Balaji Traders",
                number="B-1", total=1000.0, supplier_id=None):
    invoice = Invoice(
        company_id=company.id,
        supplier_name=supplier_name,
        supplier_id=supplier_id,
        invoice_number=number,
        invoice_date=dt.date.today().isoformat(),
        grand_total=total,
        status=status,
    )
    session.add(invoice)
    session.commit()
    return invoice


class TestRecordedStatuses:
    """Which bills count as entered."""

    def test_posted_counts(self):
        assert InvoiceStatus.POSTED.value in RECORDED_STATUSES

    def test_exported_counts(self):
        """An XML customer has confirmed the bill and written the voucher.

        If Exported did not count, every customer on the XML route would see
        empty reports for bills they had already entered.
        """
        assert InvoiceStatus.EXPORTED.value in RECORDED_STATUSES

    def test_unchecked_bills_do_not_count(self):
        for status in (InvoiceStatus.PROCESSING, InvoiceStatus.REVIEW_REQUIRED,
                       InvoiceStatus.FAILED, InvoiceStatus.DUPLICATE,
                       InvoiceStatus.CANCELLED):
            assert status.value not in RECORDED_STATUSES

    def test_confirmed_is_exportable_but_not_reportable(self):
        """Confirmed means the figures are final but nothing was sent.

        Fine to put in a spreadsheet, wrong to count in the books.
        """
        assert InvoiceStatus.CONFIRMED.value in ACCEPTED_STATUSES
        assert InvoiceStatus.CONFIRMED.value not in RECORDED_STATUSES

    def test_accepted_is_a_superset(self):
        assert set(RECORDED_STATUSES) <= set(ACCEPTED_STATUSES)

    def test_every_pending_status_has_a_next_step(self):
        """A screen that says a bill is waiting must say what to press."""
        for status in PENDING_STATUSES:
            assert NEXT_STEP.get(status), f"no next step for {status}"

    def test_every_status_is_either_recorded_or_explained(self):
        for status in InvoiceStatus:
            assert status.value in RECORDED_STATUSES or status.value in NEXT_STEP, (
                f"{status.value} would leave the user with no explanation"
            )


class TestReportQueries:
    def test_exported_bill_reaches_the_report(self, session, company):
        add_invoice(session, company, InvoiceStatus.EXPORTED.value)
        from app_pages.reports import _recorded_invoices

        assert len(_recorded_invoices(session, company)) == 1

    def test_bill_awaiting_review_stays_out(self, session, company):
        add_invoice(session, company, InvoiceStatus.REVIEW_REQUIRED.value)
        from app_pages.reports import _recorded_invoices

        assert _recorded_invoices(session, company) == []

    def test_another_company_is_never_included(self, session, company, other_company):
        add_invoice(session, other_company, InvoiceStatus.POSTED.value)
        from app_pages.reports import _recorded_invoices

        assert _recorded_invoices(session, company) == []

    def test_status_counts_name_what_is_waiting(self, session, company):
        add_invoice(session, company, InvoiceStatus.REVIEW_REQUIRED.value, number="R-1")
        add_invoice(session, company, InvoiceStatus.CONFIRMED.value, number="C-1")
        add_invoice(session, company, InvoiceStatus.POSTED.value, number="P-1")
        from app_pages.reports import _status_counts

        counts = _status_counts(session, company)
        assert counts[InvoiceStatus.REVIEW_REQUIRED.value] == 1
        assert counts[InvoiceStatus.CONFIRMED.value] == 1
        assert counts[InvoiceStatus.POSTED.value] == 1

    def test_status_counts_are_per_company(self, session, company, other_company):
        add_invoice(session, other_company, InvoiceStatus.POSTED.value)
        from app_pages.reports import _status_counts

        assert _status_counts(session, company) == {}


class TestSupplierTotals:
    """The supplier list used the same Posted-only filter as the reports."""

    def test_exported_bill_shows_against_its_supplier(self, session, company):
        supplier = Supplier(company_id=company.id, name="Balaji Traders")
        session.add(supplier)
        session.commit()
        add_invoice(
            session, company, InvoiceStatus.EXPORTED.value,
            supplier_id=supplier.id, total=2500.0,
        )
        totals = {}
        for invoice in session.query(Invoice).filter(
            Invoice.company_id == company.id
        ).all():
            if invoice.supplier_id and invoice.status in RECORDED_STATUSES:
                totals[invoice.supplier_id] = totals.get(invoice.supplier_id, 0) + (
                    invoice.grand_total or 0
                )
        assert totals[supplier.id] == 2500.0


# --------------------------------------------------------------------------
# Sample data
# --------------------------------------------------------------------------
def make_demo(session, company):
    demo_service.mark_as_demo(session, company)
    return company


class TestSampleLabelling:
    def test_a_real_company_has_no_samples(self, session, company):
        """Nothing on a licensed account may ever be called removable."""
        ids = demo_service.sample_ids(session, company)
        assert ids == {"products": set(), "suppliers": set()}

    def test_recorded_ids_come_back(self, session, company):
        product = make_product(session, company, "Parle-G Gold 800 gm")
        supplier = Supplier(company_id=company.id, name="Shreeji Distributors")
        session.add(supplier)
        session.commit()
        make_demo(session, company)
        demo_service.record_sample_data(
            session, company,
            product_ids=[product.id], supplier_ids=[supplier.id],
        )
        ids = demo_service.sample_ids(session, company)
        assert ids["products"] == {product.id}
        assert ids["suppliers"] == {supplier.id}

    def test_a_demo_seeded_by_an_older_build_still_labels_its_samples(
        self, session, company
    ):
        """No recorded ids, so fall back to what existed at the baseline.

        Without this, an installation set up before this change would show no
        sample labels until it was re-seeded — and the customer asking where
        Shreeji came from is on exactly such an installation.
        """
        supplier = Supplier(company_id=company.id, name="Shreeji Distributors")
        session.add(supplier)
        session.commit()
        make_demo(session, company)          # records stock, not ids
        from database.database import set_setting

        set_setting(session, company.id, demo_service.SAMPLE_IDS_KEY, "")

        ids = demo_service.sample_ids(session, company)
        assert supplier.id in ids["suppliers"]

    def test_a_supplier_created_after_the_baseline_is_not_a_sample(
        self, session, company
    ):
        make_demo(session, company)
        from database.database import set_setting

        set_setting(session, company.id, demo_service.SAMPLE_IDS_KEY, "")
        later = Supplier(company_id=company.id, name="Balaji Traders")
        later.created_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
        session.add(later)
        session.commit()

        assert later.id not in demo_service.sample_ids(session, company)["suppliers"]

    def test_summary_counts_what_is_left(self, session, company):
        product = make_product(session, company, "Tata Salt 1 kg")
        make_demo(session, company)
        demo_service.record_sample_data(session, company, product_ids=[product.id])
        assert demo_service.sample_data_summary(session, company) == {
            "sample products": 1
        }


class TestRemoveSampleData:
    def test_refuses_on_a_real_company(self, session, company):
        """The guard that matters: a licensed customer's master is permanent."""
        product = make_product(session, company, "Tata Salt 1 kg")
        with pytest.raises(demo_service.NotADemoCompany):
            demo_service.remove_sample_data(session, company)
        assert session.query(Product).filter(Product.id == product.id).one()

    def test_refuses_without_a_company(self, session):
        with pytest.raises(demo_service.NotADemoCompany):
            demo_service.remove_sample_data(session, None)

    def test_removes_the_seeded_supplier(self, session, company):
        supplier = Supplier(company_id=company.id, name="Shreeji Distributors")
        session.add(supplier)
        session.commit()
        make_demo(session, company)
        demo_service.record_sample_data(session, company, supplier_ids=[supplier.id])

        demo_service.remove_sample_data(session, company)

        assert session.query(Supplier).filter(
            Supplier.id == supplier.id
        ).one_or_none() is None

    def test_removes_the_seeded_products(self, session, company):
        product = make_product(session, company, "Maggi Noodles 70 gm",
                               current_stock=200.0)
        make_demo(session, company)
        demo_service.record_sample_data(session, company, product_ids=[product.id])

        demo_service.remove_sample_data(session, company)

        assert session.query(Product).filter(
            Product.id == product.id
        ).one_or_none() is None

    def test_keeps_a_sample_a_bill_refers_to(self, session, company):
        """Deleting it would leave a posted bill pointing at nothing."""
        supplier = Supplier(company_id=company.id, name="Shreeji Distributors")
        session.add(supplier)
        session.commit()
        make_demo(session, company)
        demo_service.record_sample_data(session, company, supplier_ids=[supplier.id])
        add_invoice(session, company, InvoiceStatus.POSTED.value,
                    supplier_id=supplier.id)

        removed = demo_service.remove_sample_data(session, company)

        assert session.query(Supplier).filter(Supplier.id == supplier.id).one()
        assert removed.get("kept (used by a bill)") == 1

    def test_keeps_a_product_a_bill_line_refers_to(self, session, company):
        product = make_product(session, company, "Parle-G Gold 800 gm")
        make_demo(session, company)
        demo_service.record_sample_data(session, company, product_ids=[product.id])
        invoice = add_invoice(session, company, InvoiceStatus.POSTED.value)
        session.add(InvoiceItem(
            company_id=company.id, invoice_id=invoice.id,
            product_id=product.id, raw_name="Parle-G", quantity=1, rate=95.0,
        ))
        session.commit()

        demo_service.remove_sample_data(session, company)

        assert session.query(Product).filter(Product.id == product.id).one()

    def test_leaves_other_companies_alone(self, session, company, other_company):
        theirs = make_product(session, other_company, "Their Product")
        mine = make_product(session, company, "My Sample")
        make_demo(session, company)
        # Even if the stored ids were wrong, the company filter must hold.
        demo_service.record_sample_data(
            session, company, product_ids=[mine.id, theirs.id]
        )

        demo_service.remove_sample_data(session, company)

        assert session.query(Product).filter(Product.id == theirs.id).one()

    def test_running_it_twice_is_harmless(self, session, company):
        product = make_product(session, company, "Colgate 100 gm")
        make_demo(session, company)
        demo_service.record_sample_data(session, company, product_ids=[product.id])
        demo_service.remove_sample_data(session, company)
        assert demo_service.remove_sample_data(session, company) == {}

    def test_the_stock_baseline_forgets_deleted_products(self, session, company):
        """A later clear must not try to reset a product that is gone."""
        product = make_product(session, company, "Fortune Oil 1 L",
                               current_stock=60.0)
        make_demo(session, company)
        demo_service.record_sample_data(session, company, product_ids=[product.id])
        demo_service.remove_sample_data(session, company)

        result = demo_service.clear_demo_data(session, company)
        assert result.stock_reset == 0

    def test_it_is_written_to_the_audit_trail(self, session, company):
        from database.models import AuditLog

        product = make_product(session, company, "Britannia Marie Gold")
        make_demo(session, company)
        demo_service.record_sample_data(session, company, product_ids=[product.id])
        demo_service.remove_sample_data(session, company)

        entries = session.query(AuditLog).filter(
            AuditLog.company_id == company.id,
            AuditLog.action == demo_service.CLEAR_ACTION,
        ).all()
        assert any("sample" in (e.detail or "").lower() for e in entries)


class TestBuildStamp:
    """A stale install must be identifiable without reading the source."""

    def test_version_and_build_date_exist(self):
        from utils.config import settings

        assert settings.APP_VERSION
        assert settings.BUILD_DATE

    def test_build_date_is_a_real_date(self):
        from utils.config import settings

        dt.date.fromisoformat(settings.BUILD_DATE)

    def test_changelog_leads_with_the_current_version(self):
        from utils.config import settings

        assert settings.CHANGELOG[0][0] == settings.APP_VERSION

    def test_the_sidebar_shows_the_build_date(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "app.py").read_text()
        assert "BUILD_DATE" in source, (
            "a customer on an old build has no way to see which build it is"
        )

    def test_settings_shows_the_build_date(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "app_pages" / "settings.py"
        ).read_text()
        assert "BUILD_DATE" in source
        assert "CHANGELOG" in source


class TestFeaturesShippedThisBuild:
    """Guard against the features in the changelog quietly disappearing."""

    def test_excel_is_offered_as_a_connection(self):
        from services.tally_connector import CONNECTOR_LABELS, CONNECTOR_REGISTRY

        assert "excel" in CONNECTOR_REGISTRY
        assert "excel" in CONNECTOR_LABELS

    def test_reports_has_an_excel_tab(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "app_pages" / "reports.py"
        ).read_text()
        assert "_excel_tab" in source
        assert "📗 Excel export" in source
