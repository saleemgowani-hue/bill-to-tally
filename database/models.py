"""
ORM models.

Multi-tenancy rule: every business table carries `company_id`. All queries in
the service layer MUST filter by the active company. See `database.database.
scoped_query` which enforces this.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class InvoiceStatus(str, Enum):
    PROCESSING = "Processing"
    REVIEW_REQUIRED = "Review Required"
    CONFIRMED = "Confirmed"
    READY = "Ready"
    # XML written for manual import, but Tally has not seen it yet.
    # Stock deliberately stays untouched until the user confirms the import.
    EXPORTED = "Exported"
    POSTED = "Posted"
    FAILED = "Failed"
    DUPLICATE = "Duplicate"
    CANCELLED = "Cancelled"


# Statuses that mean "this bill is part of the books". Reports, supplier
# totals and the Excel export all count these, and nothing else.
#
# Exported belongs here as much as Posted. A customer on the XML route has
# confirmed the bill and written the voucher file; the purchase happened. If
# reports waited for Posted, everyone using XML would see an empty report for
# bills they had already entered — which is exactly how this list came to be
# written down in one place instead of repeated per page.
RECORDED_STATUSES = (InvoiceStatus.EXPORTED.value, InvoiceStatus.POSTED.value)

# Confirmed by the user but not yet sent anywhere. Not in the books, but the
# figures are final, so the Excel export offers them.
ACCEPTED_STATUSES = (InvoiceStatus.CONFIRMED.value,) + RECORDED_STATUSES

# Waiting for the user to do something. Naming these lets a screen say which
# button is next instead of "nothing to report".
PENDING_STATUSES = (
    InvoiceStatus.PROCESSING.value,
    InvoiceStatus.REVIEW_REQUIRED.value,
    InvoiceStatus.CONFIRMED.value,
    InvoiceStatus.READY.value,
)

# What to do next with a bill sitting in each pending status.
NEXT_STEP = {
    InvoiceStatus.PROCESSING.value:
        "still being read — refresh in a moment",
    InvoiceStatus.REVIEW_REQUIRED.value:
        "open Review bills, fix the flagged lines, then Confirm",
    InvoiceStatus.CONFIRMED.value:
        "open Purchase history and press Post to Tally",
    InvoiceStatus.READY.value:
        "open Purchase history and press Post to Tally",
    InvoiceStatus.FAILED.value:
        "open the Error log to see why it failed",
    InvoiceStatus.DUPLICATE.value:
        "this bill number already exists — nothing to do",
    InvoiceStatus.CANCELLED.value:
        "cancelled, so it is left out on purpose",
}


class MatchStatus(str, Enum):
    MATCHED = "MATCH FOUND"
    POSSIBLE = "POSSIBLE MATCH"
    NEW = "NEW PRODUCT"
    NEW_BARCODE = "NEW BARCODE PRODUCT"


class Role(str, Enum):
    ADMIN = "Admin"
    MANAGER = "Manager"
    OPERATOR = "Operator"


# --------------------------------------------------------------------------
class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    gstin = Column(String(20))
    state = Column(String(80))
    state_code = Column(String(4))
    address = Column(Text)
    phone = Column(String(40))
    email = Column(String(160))
    tally_company_name = Column(String(200))
    created_at = Column(DateTime, default=utcnow)

    # Licensing
    license_plan = Column(String(20))            # YEARLY / MONTHLY / TRIAL
    license_expires_at = Column(DateTime)
    license_key_display = Column(String(40))     # masked, safe to show in the UI

    # Demo companies are wiped on a timer — see services/demo_service.py
    is_demo = Column(Boolean, default=False)
    demo_baseline_at = Column(DateTime)

    users = relationship("User", back_populates="company")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    full_name = Column(String(160), nullable=False)
    email = Column(String(160), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default=Role.OPERATOR.value, nullable=False)
    is_active = Column(Boolean, default=True)
    reset_token = Column(String(80))
    reset_expires_at = Column(DateTime)
    last_login_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)

    company = relationship("Company", back_populates="users")

    @property
    def can_confirm(self) -> bool:
        return self.role in (Role.ADMIN.value, Role.MANAGER.value)

    @property
    def can_post_to_tally(self) -> bool:
        return self.role in (Role.ADMIN.value, Role.MANAGER.value)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (Index("ix_supplier_company", "company_id", "name"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(200), nullable=False)
    gstin = Column(String(20))
    state = Column(String(80))
    state_code = Column(String(4))
    address = Column(Text)
    phone = Column(String(40))
    email = Column(String(160))
    tally_ledger_name = Column(String(200))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (Index("ix_product_company", "company_id", "name"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(250), nullable=False)
    sku = Column(String(80))
    barcode = Column(String(80))
    hsn = Column(String(20))
    unit = Column(String(20), default="PCS")
    pack_size = Column(String(40))
    brand = Column(String(120))
    gst_rate = Column(Float, default=0.0)
    purchase_rate = Column(Float, default=0.0)
    sale_rate = Column(Float, default=0.0)
    current_stock = Column(Float, default=0.0)
    tally_stock_item = Column(String(250))
    godown = Column(String(120))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class ProductAlias(Base):
    """A confirmed mapping between a name printed on a bill and a product."""

    __tablename__ = "product_aliases"
    __table_args__ = (
        Index("ix_alias_lookup", "company_id", "alias_normalized"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    alias_raw = Column(String(250), nullable=False)
    alias_normalized = Column(String(250), nullable=False)
    barcode = Column(String(80))
    times_used = Column(Integer, default=1)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=utcnow)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index("ix_invoice_dupe", "company_id", "supplier_name", "invoice_number"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))

    supplier_name = Column(String(200))
    supplier_gstin = Column(String(20))
    supplier_address = Column(Text)
    invoice_number = Column(String(80))
    invoice_date = Column(String(20))          # ISO string, kept as printed
    due_date = Column(String(20))

    subtotal = Column(Float, default=0.0)
    discount_total = Column(Float, default=0.0)
    tax_total = Column(Float, default=0.0)
    cess_total = Column(Float, default=0.0)
    round_off = Column(Float, default=0.0)
    grand_total = Column(Float, default=0.0)

    tax_scope = Column(String(20))             # INTRA-STATE / INTER-STATE / UNKNOWN
    overall_confidence = Column(Float, default=0.0)
    status = Column(String(30), default=InvoiceStatus.PROCESSING.value)

    source_file = Column(String(255))
    source_pages = Column(Integer, default=1)
    file_hash = Column(String(64))
    ocr_provider = Column(String(40))

    validation_json = Column(Text)             # serialized validation report
    duplicate_of_id = Column(Integer, ForeignKey("invoices.id"))
    notes = Column(Text)

    created_by = Column(Integer, ForeignKey("users.id"))
    confirmed_by = Column(Integer, ForeignKey("users.id"))
    confirmed_at = Column(DateTime)
    posted_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    items = relationship(
        "InvoiceItem", back_populates="invoice", cascade="all, delete-orphan"
    )


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"))

    raw_name = Column(String(250))
    normalized_name = Column(String(250))
    hsn = Column(String(20))
    barcode = Column(String(80))
    quantity = Column(Float, default=0.0)
    unit = Column(String(20))
    rate = Column(Float, default=0.0)
    discount = Column(Float, default=0.0)
    taxable_value = Column(Float, default=0.0)
    gst_rate = Column(Float, default=0.0)
    cgst = Column(Float, default=0.0)
    sgst = Column(Float, default=0.0)
    igst = Column(Float, default=0.0)
    cess = Column(Float, default=0.0)
    total = Column(Float, default=0.0)

    confidence = Column(Float, default=0.0)
    match_status = Column(String(30), default=MatchStatus.NEW.value)
    match_score = Column(Float, default=0.0)
    match_reason = Column(String(250))
    edited_by_user = Column(Boolean, default=False)
    line_warnings = Column(Text)

    invoice = relationship("Invoice", back_populates="items")


class PurchaseTransaction(Base):
    """One posted/imported purchase. Stock moves are tied to this row."""

    __tablename__ = "purchase_transactions"
    __table_args__ = (
        UniqueConstraint("company_id", "idempotency_key", name="uq_txn_idem"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    idempotency_key = Column(String(80), nullable=False)
    mode = Column(String(20))                  # POSTED / XML_EXPORT / TEST_MODE
    tally_voucher_id = Column(String(120))
    request_xml = Column(Text)  # exact XML sent to Tally — kept for audit
    tally_response = Column(Text)
    export_path = Column(String(255))
    stock_applied = Column(Boolean, default=False)
    amount = Column(Float, default=0.0)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=utcnow)


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    transaction_id = Column(Integer, ForeignKey("purchase_transactions.id"))
    invoice_id = Column(Integer, ForeignKey("invoices.id"))
    sales_transaction_id = Column(Integer, ForeignKey("sales_transactions.id"))
    sales_invoice_id = Column(Integer, ForeignKey("sales_invoices.id"))
    quantity_before = Column(Float, default=0.0)
    quantity_change = Column(Float, default=0.0)
    quantity_after = Column(Float, default=0.0)
    reason = Column(String(120))
    created_at = Column(DateTime, default=utcnow)


# --------------------------------------------------------------------------
# Sales — mirrors the Purchase tables above. Kept as separate tables rather
# than adding a "direction" flag to Invoice/InvoiceItem, so nothing about the
# purchase pipeline (already relied on by real installs) has to change shape.
# Same Product catalog is shared; stock moves the opposite way (see
# services/sales_service.py: apply_sales_stock decrements instead of
# increments).
# --------------------------------------------------------------------------
class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (Index("ix_customer_company", "company_id", "name"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(200), nullable=False)
    gstin = Column(String(20))
    state = Column(String(80))
    state_code = Column(String(4))
    address = Column(Text)
    phone = Column(String(40))
    email = Column(String(160))
    tally_ledger_name = Column(String(200))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


class SalesInvoice(Base):
    __tablename__ = "sales_invoices"
    __table_args__ = (
        Index("ix_sales_invoice_dupe", "company_id", "customer_name", "invoice_number"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"))

    customer_name = Column(String(200))
    customer_gstin = Column(String(20))
    customer_address = Column(Text)
    invoice_number = Column(String(80))
    invoice_date = Column(String(20))          # ISO string, kept as printed
    due_date = Column(String(20))

    subtotal = Column(Float, default=0.0)
    discount_total = Column(Float, default=0.0)
    tax_total = Column(Float, default=0.0)
    cess_total = Column(Float, default=0.0)
    round_off = Column(Float, default=0.0)
    grand_total = Column(Float, default=0.0)

    tax_scope = Column(String(20))             # INTRA-STATE / INTER-STATE / UNKNOWN
    overall_confidence = Column(Float, default=0.0)
    status = Column(String(30), default=InvoiceStatus.PROCESSING.value)

    source_file = Column(String(255))
    source_pages = Column(Integer, default=1)
    file_hash = Column(String(64))
    ocr_provider = Column(String(40))

    validation_json = Column(Text)             # serialized validation report
    duplicate_of_id = Column(Integer, ForeignKey("sales_invoices.id"))
    notes = Column(Text)

    created_by = Column(Integer, ForeignKey("users.id"))
    confirmed_by = Column(Integer, ForeignKey("users.id"))
    confirmed_at = Column(DateTime)
    posted_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    items = relationship(
        "SalesInvoiceItem", back_populates="invoice", cascade="all, delete-orphan"
    )


class SalesInvoiceItem(Base):
    __tablename__ = "sales_invoice_items"

    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("sales_invoices.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"))

    raw_name = Column(String(250))
    normalized_name = Column(String(250))
    hsn = Column(String(20))
    barcode = Column(String(80))
    quantity = Column(Float, default=0.0)
    unit = Column(String(20))
    rate = Column(Float, default=0.0)
    discount = Column(Float, default=0.0)
    taxable_value = Column(Float, default=0.0)
    gst_rate = Column(Float, default=0.0)
    cgst = Column(Float, default=0.0)
    sgst = Column(Float, default=0.0)
    igst = Column(Float, default=0.0)
    cess = Column(Float, default=0.0)
    total = Column(Float, default=0.0)

    confidence = Column(Float, default=0.0)
    match_status = Column(String(30), default=MatchStatus.NEW.value)
    match_score = Column(Float, default=0.0)
    match_reason = Column(String(250))
    edited_by_user = Column(Boolean, default=False)
    line_warnings = Column(Text)

    invoice = relationship("SalesInvoice", back_populates="items")


class SalesTransaction(Base):
    """One posted/imported sales voucher. Stock moves are tied to this row."""

    __tablename__ = "sales_transactions"
    __table_args__ = (
        UniqueConstraint("company_id", "idempotency_key", name="uq_sales_txn_idem"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    invoice_id = Column(Integer, ForeignKey("sales_invoices.id"), nullable=False)
    idempotency_key = Column(String(80), nullable=False)
    mode = Column(String(20))                  # POSTED / XML_EXPORT / TEST_MODE
    tally_voucher_id = Column(String(120))
    request_xml = Column(Text)  # exact XML sent to Tally — kept for audit
    tally_response = Column(Text)
    export_path = Column(String(255))
    stock_applied = Column(Boolean, default=False)
    amount = Column(Float, default=0.0)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=utcnow)


# --------------------------------------------------------------------------
# Bank reconciliation — imported bank-statement lines, matched against open
# Purchase/Sales invoices (or a general ledger for non-trade items like bank
# charges or salary), and posted to Tally as Payment/Receipt vouchers. No
# stock is affected by anything in this section.
# --------------------------------------------------------------------------
class BankAccount(Base):
    __tablename__ = "bank_accounts"
    __table_args__ = (Index("ix_bank_account_company", "company_id", "name"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(160), nullable=False)          # display name, e.g. "HDFC Current A/c"
    bank_name = Column(String(120))
    account_number = Column(String(40))
    ifsc = Column(String(20))
    tally_ledger_name = Column(String(200))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    __table_args__ = (
        Index("ix_bank_txn_company", "company_id", "bank_account_id", "txn_date"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    bank_account_id = Column(Integer, ForeignKey("bank_accounts.id"), nullable=False)

    txn_date = Column(String(20))               # ISO string, as printed/parsed
    narration = Column(Text)
    reference_no = Column(String(80))
    debit = Column(Float, default=0.0)           # money OUT of the bank
    credit = Column(Float, default=0.0)          # money IN to the bank
    balance_after = Column(Float)

    status = Column(String(20), default="UNMATCHED")
    # UNMATCHED / MATCHED / POSTED / IGNORED / DUPLICATE

    match_party_type = Column(String(10))        # "supplier" | "customer" | "ledger"
    match_party_id = Column(Integer)              # id in suppliers or customers, per match_party_type
    ledger_name = Column(String(200))             # used when match_party_type == "ledger"
    match_confidence = Column(Float, default=0.0)
    match_reason = Column(String(250))

    voucher_type = Column(String(20))             # "Payment" | "Receipt"
    idempotency_key = Column(String(80))
    mode = Column(String(20))                     # POSTED / XML_EXPORT / TEST_MODE
    tally_voucher_id = Column(String(120))
    request_xml = Column(Text)
    tally_response = Column(Text)
    export_path = Column(String(255))
    posted_at = Column(DateTime)

    row_hash = Column(String(64))                 # duplicate detection across re-uploads
    source_file = Column(String(255))
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=utcnow)

    allocations = relationship(
        "BankAllocation", back_populates="transaction", cascade="all, delete-orphan"
    )


class BankAllocation(Base):
    """Splits one bank line across one or more open invoices. Exactly one of
    invoice_id / sales_invoice_id is set per row."""

    __tablename__ = "bank_allocations"

    id = Column(Integer, primary_key=True)
    bank_transaction_id = Column(Integer, ForeignKey("bank_transactions.id"), nullable=False)
    invoice_id = Column(Integer, ForeignKey("invoices.id"))
    sales_invoice_id = Column(Integer, ForeignKey("sales_invoices.id"))
    amount = Column(Float, default=0.0)
    created_at = Column(DateTime, default=utcnow)

    transaction = relationship("BankTransaction", back_populates="allocations")


class TallyMapping(Base):
    """Configurable ledger / voucher-type mapping. Nothing is hard-coded."""

    __tablename__ = "tally_mappings"
    __table_args__ = (
        UniqueConstraint("company_id", "mapping_key", name="uq_mapping_key"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    mapping_key = Column(String(60), nullable=False)
    mapping_value = Column(String(200))
    description = Column(String(250))
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class OCRResult(Base):
    __tablename__ = "ocr_results"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    invoice_id = Column(Integer, ForeignKey("invoices.id"))
    sales_invoice_id = Column(Integer, ForeignKey("sales_invoices.id"))
    provider = Column(String(40))
    model = Column(String(80))
    raw_text = Column(Text)
    raw_json = Column(Text)
    repaired = Column(Boolean, default=False)
    attempts = Column(Integer, default=1)
    duration_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_company_time", "company_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    user_name = Column(String(160))
    invoice_id = Column(Integer, ForeignKey("invoices.id"))
    sales_invoice_id = Column(Integer, ForeignKey("sales_invoices.id"))
    action = Column(String(80), nullable=False)
    detail = Column(Text)
    created_at = Column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("company_id", "key", name="uq_setting_key"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    key = Column(String(80), nullable=False)
    value = Column(Text)
    is_secret = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class ProcessingError(Base):
    __tablename__ = "processing_errors"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    invoice_id = Column(Integer, ForeignKey("invoices.id"))
    sales_invoice_id = Column(Integer, ForeignKey("sales_invoices.id"))
    stage = Column(String(60))                 # upload / ocr / parse / match / tally
    message = Column(Text)
    traceback_text = Column(Text)
    resolved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)


class License(Base):
    """One activated licence key. The key itself is never stored — only its hash."""

    __tablename__ = "licenses"

    id = Column(Integer, primary_key=True)
    key_hash = Column(String(64), nullable=False, unique=True, index=True)
    key_display = Column(String(40))
    plan = Column(String(20), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    activated_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime)
    activated_by = Column(Integer, ForeignKey("users.id"))
    notes = Column(Text)
