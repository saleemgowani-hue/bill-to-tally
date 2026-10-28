"""Tests for services.excel_import and purchase_service.ingest_excel."""
import io

import pandas as pd
import pytest

from database.models import InvoiceStatus
from services.excel_import import ExcelImportError, build_invoices, read_table, sample_template_bytes
from services.purchase_service import ingest_excel


def _xlsx_bytes(rows, columns=None) -> bytes:
    df = pd.DataFrame(rows, columns=columns)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    return buf.getvalue()


ROWS_TWO_INVOICES = [
    {
        "Supplier": "ABC Traders", "Invoice No": "INV-1", "Invoice Date": "01-09-2026",
        "Item": "Rice 25kg", "Qty": 10, "Rate": 1200, "GST%": 5,
    },
    {
        "Supplier": "ABC Traders", "Invoice No": "INV-1", "Invoice Date": "01-09-2026",
        "Item": "Oil 15L", "Qty": 5, "Rate": 1800, "GST%": 5,
    },
    {
        "Supplier": "XYZ Distributors", "Invoice No": "B-99", "Invoice Date": "02-09-2026",
        "Item": "Sugar 50kg", "Qty": 4, "Rate": 2000, "GST%": 5,
    },
]


def test_build_invoices_groups_by_supplier_and_invoice_number():
    df = pd.DataFrame(ROWS_TWO_INVOICES)
    groups = build_invoices(df)
    assert len(groups) == 2
    first = groups[0].data
    assert first["supplier"]["name"] == "ABC Traders"
    assert first["invoice"]["invoice_number"] == "INV-1"
    assert len(first["items"]) == 2
    second = groups[1].data
    assert second["supplier"]["name"] == "XYZ Distributors"
    assert len(second["items"]) == 1


def test_build_invoices_computes_missing_taxable_value_and_total():
    df = pd.DataFrame([
        {"Item": "Widget", "Qty": 3, "Rate": 100, "Discount": 10},
    ])
    groups = build_invoices(df)
    item = groups[0].data["items"][0]
    # 3*100 - 10 = 290
    assert item["taxable_value"] == pytest.approx(290.0)
    assert item["total"] == pytest.approx(290.0)  # no gst/cess given


def test_build_invoices_does_not_overwrite_provided_numbers():
    df = pd.DataFrame([
        {"Item": "Widget", "Qty": 3, "Rate": 100, "Taxable Value": 250, "Total": 275},
    ])
    groups = build_invoices(df)
    item = groups[0].data["items"][0]
    assert item["taxable_value"] == pytest.approx(250.0)
    assert item["total"] == pytest.approx(275.0)


def test_build_invoices_computes_grand_total_from_items():
    df = pd.DataFrame([
        {"Supplier": "ABC", "Invoice No": "I-1", "Item": "A", "Qty": 1, "Rate": 100},
        {"Supplier": "ABC", "Invoice No": "I-1", "Item": "B", "Qty": 2, "Rate": 50},
    ])
    groups = build_invoices(df)
    assert groups[0].data["grand_total"] == pytest.approx(200.0)


def test_build_invoices_requires_item_column():
    df = pd.DataFrame([{"Supplier": "ABC", "Qty": 1, "Rate": 1}])
    with pytest.raises(ExcelImportError):
        build_invoices(df)


def test_build_invoices_skips_blank_rows():
    df = pd.DataFrame([
        {"Item": "Widget", "Qty": 1, "Rate": 10},
        {"Item": None, "Qty": None, "Rate": None},
    ])
    groups = build_invoices(df)
    assert len(groups) == 1
    assert len(groups[0].data["items"]) == 1


def test_build_invoices_no_grouping_columns_is_one_invoice():
    df = pd.DataFrame([
        {"Item": "Widget A", "Qty": 1, "Rate": 10},
        {"Item": "Widget B", "Qty": 2, "Rate": 20},
    ])
    groups = build_invoices(df)
    assert len(groups) == 1
    assert len(groups[0].data["items"]) == 2
    assert "supplier name" in groups[0].warnings[0].lower() or groups[0].warnings


def test_read_table_rejects_junk_bytes():
    with pytest.raises(ExcelImportError):
        read_table(b"not an excel file", "bad.xlsx")


def test_sample_template_round_trips():
    raw = sample_template_bytes()
    df = read_table(raw, "template.xlsx")
    groups = build_invoices(df)
    assert len(groups) >= 1
    assert groups[0].data["items"]


def test_ingest_excel_creates_invoices_and_matches_products(session, company, user):
    from tests.conftest import make_product

    make_product(session, company, "Rice 25kg", gst_rate=5.0)

    raw = _xlsx_bytes(ROWS_TWO_INVOICES)
    results = ingest_excel(session, company, user, "purchases.xlsx", raw)

    assert len(results) == 2
    statuses = {r.invoice.status for r in results}
    assert statuses == {InvoiceStatus.REVIEW_REQUIRED.value}

    first = next(r for r in results if r.invoice.supplier_name == "ABC Traders")
    assert first.invoice.invoice_number == "INV-1"
    assert len(first.invoice.items) == 2
    # The pre-existing "Rice 25kg" product should have matched by name.
    matched_names = {item.match_status for item in first.invoice.items}
    assert "MATCH FOUND" in matched_names or "POSSIBLE MATCH" in matched_names


def test_ingest_excel_detects_duplicate_on_reupload(session, company, user):
    raw = _xlsx_bytes(ROWS_TWO_INVOICES)
    ingest_excel(session, company, user, "purchases.xlsx", raw)
    results_again = ingest_excel(session, company, user, "purchases.xlsx", raw)

    dupes = [r for r in results_again if r.invoice.status == InvoiceStatus.DUPLICATE.value]
    assert len(dupes) == 2
