"""Verifies the data-layer operations the new per-line Sales review UI
performs (audit_service.field_change with sales_invoice_id, product creation
without corrupting purchase_rate, remember_mapping without a supplier FK)."""
import io
import pandas as pd
import pytest

from database.models import AuditLog, MatchStatus, Product
from services import audit_service
from services.product_matcher import ProductMatcher
from services.sales_service import ingest_sales_excel, recalculate_sales_totals


def _xlsx(rows):
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def test_field_change_writes_sales_invoice_id_not_invoice_id(session, company, user):
    rows = [{"Customer": "Retail Mart", "Invoice No": "SI-1", "Item": "Widget", "Qty": 1, "Rate": 100}]
    invoice = ingest_sales_excel(session, company, user, "s.xlsx", _xlsx(rows))[0].invoice

    audit_service.field_change(
        session, company.id, user, None, "customer", "Old Name", "New Name",
        sales_invoice_id=invoice.id,
    )
    entry = (
        session.query(AuditLog)
        .filter(AuditLog.sales_invoice_id == invoice.id, AuditLog.action == "FIELD_EDITED")
        .one()
    )
    assert entry.invoice_id is None
    assert "New Name" in entry.detail


def test_create_product_from_sales_line_does_not_set_purchase_rate(session, company, user):
    matcher = ProductMatcher(session, company.id)
    product = matcher.create_product_from_item(
        {"normalized_name": "New Widget", "raw_name": "New Widget", "hsn": "1234",
         "barcode": "", "unit": "PCS", "gst_rate": 18, "rate": 0},
        user.id,
    )
    product.sale_rate = 250.0
    session.commit()

    fetched = session.query(Product).filter(Product.id == product.id).one()
    assert fetched.purchase_rate == 0.0
    assert fetched.sale_rate == 250.0


def test_remember_mapping_without_supplier_id_does_not_raise(session, company, user):
    matcher = ProductMatcher(session, company.id)
    product = matcher.create_product_from_item(
        {"normalized_name": "Widget B", "raw_name": "Widget B", "unit": "PCS", "rate": 0}, user.id,
    )
    # supplier_id=None must not violate the ProductAlias FK.
    alias = matcher.remember_mapping("Widget B raw name", product.id, user.id, None, "")
    assert alias.supplier_id is None
    assert alias.product_id == product.id


def test_manual_line_add_and_recalculate(session, company, user):
    from database.models import SalesInvoiceItem

    rows = [{"Customer": "Retail Mart", "Invoice No": "SI-2", "Item": "Widget", "Qty": 1, "Rate": 100}]
    invoice = ingest_sales_excel(session, company, user, "s2.xlsx", _xlsx(rows))[0].invoice

    session.add(SalesInvoiceItem(
        invoice_id=invoice.id, company_id=company.id, raw_name="Manual Item",
        normalized_name="Manual Item", quantity=2, unit="PCS", rate=50, discount=0,
        gst_rate=5, taxable_value=100, cgst=2.5, sgst=2.5, igst=0, total=105,
        confidence=1.0, match_status=MatchStatus.NEW.value, match_reason="added manually",
        edited_by_user=True,
    ))
    session.commit()
    recalculate_sales_totals(session, invoice)

    items = session.query(SalesInvoiceItem).filter(SalesInvoiceItem.invoice_id == invoice.id).all()
    assert len(items) == 2
    assert invoice.subtotal == pytest.approx(sum(i.taxable_value for i in items))
