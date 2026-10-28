"""Tests for the demo seeder's flags and its company delete."""
import pytest
from sqlalchemy import func, select

import seed_demo
from database.models import Base, Company, Invoice, Product, User


class TestDeleteOrder:
    """The delete order must come from the schema, not a hand-written list.

    audit_logs points at invoices, so clearing invoices first raises
    'FOREIGN KEY constraint failed'. This guards that ordering.
    """

    def test_every_company_scoped_table_is_covered(self):
        ordered = [t.name for t in reversed(Base.metadata.sorted_tables)]
        scoped = [
            t.name for t in Base.metadata.sorted_tables
            if t.columns.get("company_id") is not None
        ]
        assert set(scoped).issubset(set(ordered))

    def test_children_are_deleted_before_their_parents(self):
        order = [t.name for t in reversed(Base.metadata.sorted_tables)]
        position = {name: index for index, name in enumerate(order)}
        for table in Base.metadata.sorted_tables:
            for key in table.foreign_keys:
                parent = key.column.table.name
                if parent == table.name:      # self-reference, e.g. duplicate_of_id
                    continue
                assert position[table.name] < position[parent], (
                    f"{table.name} must be deleted before {parent}"
                )

    def test_audit_logs_precede_invoices(self):
        order = [t.name for t in reversed(Base.metadata.sorted_tables)]
        assert order.index("audit_logs") < order.index("invoices")


class TestDeleteDemoCompany:
    def test_returns_false_when_absent(self, session):
        assert seed_demo.delete_demo_company(session) is False

    def test_removes_the_company_and_its_rows(self, session):
        company = Company(name=seed_demo.DEMO_COMPANY)
        session.add(company)
        session.commit()
        session.add_all([
            User(company_id=company.id, full_name="A", email="seedtest@x.local",
                 password_hash="x", role="Admin"),
            Product(company_id=company.id, name="Widget", unit="PCS"),
            Invoice(company_id=company.id, supplier_name="S", status="Posted"),
        ])
        session.commit()
        company_id = company.id

        assert seed_demo.delete_demo_company(session) is True
        assert session.query(Company).filter(Company.id == company_id).one_or_none() is None
        for model in (User, Product, Invoice):
            assert session.query(model).filter(model.company_id == company_id).count() == 0

    def test_leaves_other_companies_untouched(self, session, other_company):
        demo = Company(name=seed_demo.DEMO_COMPANY)
        session.add(demo)
        session.commit()
        keeper = Product(company_id=other_company.id, name="Real Product", unit="PCS")
        session.add(keeper)
        session.commit()

        seed_demo.delete_demo_company(session)
        assert session.query(Product).filter(Product.id == keeper.id).one_or_none()
        assert session.query(Company).filter(Company.id == other_company.id).one_or_none()

    def test_no_orphans_left_behind(self, session):
        company = Company(name=seed_demo.DEMO_COMPANY)
        session.add(company)
        session.commit()
        session.add(Invoice(company_id=company.id, supplier_name="S", status="Posted"))
        session.commit()

        seed_demo.delete_demo_company(session)
        live = {row[0] for row in session.execute(select(Company.id)).all()}
        for table in Base.metadata.sorted_tables:
            column = table.columns.get("company_id")
            if column is None:
                continue
            for row in session.execute(select(column)).all():
                assert row[0] in live, f"orphan row in {table.name}"


class TestProductDefinitions:
    def test_seed_list_shape(self):
        for entry in seed_demo.PRODUCTS:
            assert len(entry) == 8
            name, sku, barcode, hsn, unit, gst, rate, stock = entry
            assert name and sku and unit
            assert stock >= 0

    def test_opening_stock_is_present_by_default(self):
        assert any(entry[7] > 0 for entry in seed_demo.PRODUCTS)


class TestDeleteCompanyHelper:
    """delete_company is shared by the seeder and the smoke tests, so the
    FK ordering only has to be right in one place."""

    def test_removes_everything_including_users_and_audit_logs(self, session):
        from database.database import delete_company
        from database.models import AuditLog, Company, Invoice, User

        company = Company(name="Throwaway Co")
        session.add(company)
        session.commit()
        user = User(
            company_id=company.id, full_name="U", email="throwaway@x.local",
            password_hash="x", role="Admin",
        )
        session.add(user)
        session.commit()
        invoice = Invoice(company_id=company.id, supplier_name="S", status="Posted")
        session.add(invoice)
        session.commit()
        # audit_logs reference BOTH users and invoices — the ordering trap
        session.add(AuditLog(
            company_id=company.id, user_id=user.id, invoice_id=invoice.id,
            action="TEST", detail="x",
        ))
        session.commit()
        company_id = company.id

        delete_company(session, company)   # must not raise

        assert session.query(Company).filter(Company.id == company_id).one_or_none() is None
        for model in (User, Invoice, AuditLog):
            assert session.query(model).filter(model.company_id == company_id).count() == 0
