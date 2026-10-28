"""Shared pytest fixtures. Every test runs against a throwaway SQLite file."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the app at a temporary database BEFORE any app module is imported.
_TMP = tempfile.mkdtemp(prefix="billtotally_tests_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["DATA_DIR"] = _TMP
os.environ["TALLY_TEST_MODE"] = "true"
os.environ["OCR_PROVIDER"] = "mock"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["LICENSE_SIGNING_SECRET"] = "test-licence-signing-secret-value"

from database.database import create_company_with_admin, get_session, init_db  # noqa: E402
from database.models import Base, Company, Product, User  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_db()
    yield


@pytest.fixture
def session():
    db = get_session()
    yield db
    db.rollback()
    db.close()


@pytest.fixture
def user(session):
    import uuid

    email = f"admin_{uuid.uuid4().hex[:8]}@test.local"
    return create_company_with_admin(
        session, f"Test Co {uuid.uuid4().hex[:6]}", "Test Admin", email, "Passw0rd123",
        gstin="23ABCDE1234F1Z5",
    )


@pytest.fixture
def company(session, user):
    return session.query(Company).filter(Company.id == user.company_id).one()


@pytest.fixture
def other_company(session):
    import uuid

    other = create_company_with_admin(
        session, f"Other Co {uuid.uuid4().hex[:6]}", "Other Admin",
        f"other_{uuid.uuid4().hex[:8]}@test.local", "Passw0rd123",
    )
    return session.query(Company).filter(Company.id == other.company_id).one()


def make_product(session, company, name, **kwargs) -> Product:
    product = Product(
        company_id=company.id,
        name=name,
        unit=kwargs.pop("unit", "PCS"),
        gst_rate=kwargs.pop("gst_rate", 18.0),
        current_stock=kwargs.pop("current_stock", 0.0),
        **kwargs,
    )
    session.add(product)
    session.commit()
    return product
