"""
Database abstraction layer.

SQLite for local/desktop installs, PostgreSQL for the SaaS deployment. Only the
DATABASE_URL changes; no application code is aware of the engine.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from database.models import (
    AuditLog,
    Base,
    Company,
    Role,
    Setting,
    TallyMapping,
    User,
)
from utils.config import settings
from utils.logging import get_logger
from utils.security import hash_password

log = get_logger("database")

_engine = None
_SessionFactory = None


DEFAULT_TALLY_MAPPINGS = [
    ("purchase_ledger", "Purchase Account", "Ledger the purchase value is debited to"),
    ("voucher_type", "Purchase", "Tally voucher type name"),
    ("cgst_ledger", "CGST Input", "Input CGST ledger"),
    ("sgst_ledger", "SGST Input", "Input SGST ledger"),
    ("igst_ledger", "IGST Input", "Input IGST ledger"),
    ("cess_ledger", "Cess Input", "Input Cess ledger"),
    ("round_off_ledger", "Round Off", "Round-off ledger"),
    ("discount_ledger", "Discount Received", "Ledger for bill-level discount"),
    ("default_godown", "Main Location", "Godown / location for stock inward"),
    ("default_unit", "PCS", "Unit used when a new stock item is created"),
    ("supplier_ledger_group", "Sundry Creditors", "Group for auto-created suppliers"),
    ("stock_group", "Primary", "Stock group for auto-created stock items"),
]


def get_engine():
    global _engine
    if _engine is None:
        url = settings.DATABASE_URL
        kwargs = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, **kwargs)

        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()
        log.info("Database engine ready (%s)", url.split("://", 1)[0])
    return _engine


def get_session_factory():
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Session:
    """Caller-managed session (used by Streamlit pages)."""
    return get_session_factory()()


def scoped_query(session: Session, model, company_id: int):
    """
    Every tenant-owned query goes through here.

    Raises if the model has no company_id column so a developer cannot
    accidentally leak data between customers.
    """
    if not hasattr(model, "company_id"):
        raise AttributeError(f"{model.__name__} is not a tenant-scoped model")
    if not company_id:
        raise ValueError("company_id is required for tenant-scoped queries")
    return session.query(model).filter(model.company_id == company_id)


def _add_missing_columns() -> None:
    """Additively bring an existing database up to the current model.

    A customer upgrading the app must not lose their data, and this project
    ships no migration framework. `create_all` creates missing *tables* but
    never missing *columns*, so we add those by hand. Additive only: nothing
    is ever dropped, renamed or retyped here. Anything more involved than a
    new nullable column needs a real migration.
    """
    engine = get_engine()
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not (column.nullable or column.default is not None):
                log.warning(
                    "Column %s.%s is missing and is NOT NULL — skipping automatic "
                    "upgrade. Migrate this table manually.", table.name, column.name
                )
                continue
            ddl = column.type.compile(engine.dialect)
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')
                    )
                log.info("Added missing column %s.%s", table.name, column.name)
            except SQLAlchemyError as exc:
                log.warning(
                    "Could not add column %s.%s automatically: %s",
                    table.name, column.name, exc,
                )


def init_db() -> None:
    Base.metadata.create_all(get_engine())
    _add_missing_columns()
    log.info("Schema created/verified")


def seed_tally_mappings(session: Session, company_id: int) -> None:
    existing = {
        m.mapping_key
        for m in session.query(TallyMapping)
        .filter(TallyMapping.company_id == company_id)
        .all()
    }
    for key, value, desc in DEFAULT_TALLY_MAPPINGS:
        if key not in existing:
            session.add(
                TallyMapping(
                    company_id=company_id,
                    mapping_key=key,
                    mapping_value=value,
                    description=desc,
                )
            )
    session.commit()


def create_company_with_admin(
    session: Session,
    company_name: str,
    full_name: str,
    email: str,
    password: str,
    gstin: str = "",
    state: str = "",
) -> User:
    company = Company(name=company_name, gstin=gstin or None, state=state or None)
    session.add(company)
    session.flush()

    user = User(
        company_id=company.id,
        full_name=full_name,
        email=email.strip().lower(),
        password_hash=hash_password(password),
        role=Role.ADMIN.value,
    )
    session.add(user)
    session.flush()
    seed_tally_mappings(session, company.id)
    session.add(
        AuditLog(
            company_id=company.id,
            user_id=user.id,
            user_name=full_name,
            action="COMPANY_CREATED",
            detail=f"Company '{company_name}' registered with admin {email}",
        )
    )
    session.commit()
    log.info("Company %s created (id=%s)", company_name, company.id)
    return user


def get_setting(session: Session, company_id: int, key: str, default=None):
    row = (
        session.query(Setting)
        .filter(Setting.company_id == company_id, Setting.key == key)
        .one_or_none()
    )
    return row.value if row else default


def set_setting(
    session: Session, company_id: int, key: str, value, is_secret: bool = False
) -> None:
    row = (
        session.query(Setting)
        .filter(Setting.company_id == company_id, Setting.key == key)
        .one_or_none()
    )
    if row is None:
        row = Setting(company_id=company_id, key=key, is_secret=is_secret)
        session.add(row)
    row.value = "" if value is None else str(value)
    row.is_secret = is_secret
    session.commit()


def get_mapping(session: Session, company_id: int, key: str, default: str = "") -> str:
    row = (
        session.query(TallyMapping)
        .filter(TallyMapping.company_id == company_id, TallyMapping.mapping_key == key)
        .one_or_none()
    )
    return (row.mapping_value if row and row.mapping_value else default) or default


def all_mappings(session: Session, company_id: int) -> dict:
    rows = (
        session.query(TallyMapping)
        .filter(TallyMapping.company_id == company_id)
        .order_by(TallyMapping.mapping_key)
        .all()
    )
    return {r.mapping_key: (r.mapping_value or "") for r in rows}


def delete_company(session: Session, company: Company) -> None:
    """Delete a company and every row belonging to it.

    The order comes from SQLAlchemy's topological sort of the foreign keys,
    reversed so children go first. Writing that order by hand is how you get
    'FOREIGN KEY constraint failed' the day someone adds a table — audit_logs
    point at both invoices and users, for instance.
    """
    from sqlalchemy import delete as sa_delete

    company_id = company.id
    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "companies":
            continue
        column = table.columns.get("company_id")
        if column is None:
            continue
        session.execute(sa_delete(table).where(column == company_id))
    session.delete(company)
    session.commit()
    log.info("Company %s deleted with all its data", company_id)
