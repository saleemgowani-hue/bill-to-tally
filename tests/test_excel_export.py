"""Tests for the Excel output used by customers who do not run Tally."""
from pathlib import Path

import openpyxl
import pytest

from database.models import Invoice, InvoiceItem, InvoiceStatus, Product
from services.excel_export import (
    build_bill_workbook,
    build_register_workbook,
    safe_name,
)
from services.tally_connector import (
    CONNECTOR_LABELS,
    ExcelConnector,
    VoucherPayload,
    get_connector,
)
from tests.conftest import make_product


@pytest.fixture
def payload():
    return VoucherPayload(
        company_name="Novelty Store",
        voucher_type="Purchase",
        supplier_ledger="Maa Sharda Agencies",
        purchase_ledger="Purchase Account",
        invoice_number="MSA/26-27/0138",
        invoice_date="2026-09-01",
        narration="",
        godown="Main Location",
        round_off_ledger="Round Off",
        round_off_amount=-0.04,
        tax_lines=[{"ledger": "CGST Input", "amount": 551.52}],
        items=[
            {
                "stock_item": "Amul Butter 500 gm", "quantity": 12, "unit": "PCS",
                "rate": 265.0, "discount": 180.0, "taxable_value": 3000.0,
                "hsn": "04051000", "gst_rate": 12.0, "cgst": 180.0, "sgst": 180.0,
                "igst": 0.0, "cess": 0.0, "total": 3360.0, "godown": "Main Location",
            },
            {
                "stock_item": "Britannia Marie Gold 250 gm", "quantity": 40,
                "unit": "PCS", "rate": 30.0, "discount": 0.0, "taxable_value": 1200.0,
                "hsn": "19053100", "gst_rate": 18.0, "cgst": 108.0, "sgst": 108.0,
                "igst": 0.0, "cess": 0.0, "total": 1416.0, "godown": "Main Location",
            },
        ],
        grand_total=4776.0,
        reference_id="ref-1",
    )


class TestFilenames:
    def test_slashes_are_stripped(self):
        assert "/" not in safe_name("MSA/26-27/0138")

    def test_empty_falls_back(self):
        assert safe_name("") == "export"
        assert safe_name("!!!", "bill") == "bill"


class TestBillWorkbook:
    def test_writes_a_readable_file(self, payload, tmp_path):
        path = Path(build_bill_workbook(payload, destination=tmp_path))
        assert path.exists() and path.suffix == ".xlsx"
        workbook = openpyxl.load_workbook(path)
        assert workbook.active.title == "Purchase Bill"

    def test_carries_the_supplier_and_invoice(self, payload, tmp_path):
        path = build_bill_workbook(payload, destination=tmp_path)
        text = _all_text(path)
        assert "Maa Sharda Agencies" in text
        assert "MSA/26-27/0138" in text

    def test_every_line_appears(self, payload, tmp_path):
        path = build_bill_workbook(payload, destination=tmp_path)
        text = _all_text(path)
        for item in payload.items:
            assert item["stock_item"] in text

    def test_per_line_tax_is_written(self, payload, tmp_path):
        """The bug this guards: a Tally payload carries tax at voucher level,
        so a sheet built naively from it shows 0 in every tax column."""
        path = build_bill_workbook(payload, destination=tmp_path)
        sheet = openpyxl.load_workbook(path).active
        headers, first = _table(sheet)
        assert first[headers.index("CGST")] == 180.0
        assert first[headers.index("SGST")] == 180.0
        assert first[headers.index("GST %")] == 12.0
        assert first[headers.index("Discount")] == 180.0
        assert first[headers.index("Total")] == 3360.0

    def test_totals_are_formulas_not_baked_numbers(self, payload, tmp_path):
        """A customer editing a quantity should see the sheet re-add itself."""
        path = build_bill_workbook(payload, destination=tmp_path)
        sheet = openpyxl.load_workbook(path).active      # formulas, not values
        formulas = [
            cell.value for row in sheet.iter_rows() for cell in row
            if isinstance(cell.value, str) and cell.value.startswith("=SUM(")
        ]
        assert formulas, "totals should be SUM formulas"

    def test_says_stock_is_already_updated(self, payload, tmp_path):
        """Nobody should go looking for something to import."""
        path = build_bill_workbook(payload, destination=tmp_path)
        assert "nothing to import" in _all_text(path).lower()

    def test_handles_a_bill_with_no_items(self, payload, tmp_path):
        payload.items = []
        path = build_bill_workbook(payload, destination=tmp_path)
        assert Path(path).exists()


class TestExcelConnector:
    def test_registered(self):
        assert isinstance(get_connector("excel"), ExcelConnector)
        assert "excel" in CONNECTOR_LABELS

    def test_label_says_no_tally(self):
        assert "no tally" in CONNECTOR_LABELS["excel"].lower()

    def test_connection_always_works(self):
        result = ExcelConnector().test_connection()
        assert result.success and result.mode == "EXCEL"

    def test_preflight_has_nothing_to_check(self, payload):
        assert ExcelConnector().preflight(payload) == []

    def test_post_writes_a_workbook(self, payload):
        result = ExcelConnector().post_voucher(payload)
        assert result.success
        assert result.mode == "EXCEL"
        assert Path(result.export_path).exists()

    def test_mode_is_not_xml_export(self, payload):
        """XML_EXPORT means 'stock waits for a manual import'. Excel must not
        use it, or every bill would strand awaiting an import nobody does."""
        assert ExcelConnector().post_voucher(payload).mode != "XML_EXPORT"


class TestStockMovesImmediately:
    """With no Tally there is no external book to wait for — this app is it."""

    def _confirmed_invoice(self, session, company, user):
        from PIL import Image

        from services.ai_invoice_parser import InvoiceParser
        from services.ocr_service import MockOCRProvider
        from services.purchase_service import confirm_invoice, ingest_bill

        result = ingest_bill(
            session, company, user, "b.jpg", b"\xff\xd8\xffexcel",
            [Image.new("RGB", (50, 50), "white")],
            InvoiceParser(MockOCRProvider()), store_image=False,
        )
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
        ok, message = confirm_invoice(session, company, user, invoice, "test")
        assert ok, message
        return invoice, items

    def test_posting_in_excel_mode_updates_stock(self, session, company, user):
        from services.purchase_service import post_to_tally

        invoice, items = self._confirmed_invoice(session, company, user)
        product = session.query(Product).filter(Product.id == items[0].product_id).one()
        before = product.current_stock

        result, transaction = post_to_tally(
            session, company, user, invoice, ExcelConnector(), run_preflight=False
        )
        assert result.success
        assert transaction.stock_applied
        session.refresh(product)
        assert product.current_stock == before + items[0].quantity

    def test_invoice_is_posted_not_left_exported(self, session, company, user):
        from services.purchase_service import post_to_tally

        invoice, _ = self._confirmed_invoice(session, company, user)
        post_to_tally(
            session, company, user, invoice, ExcelConnector(), run_preflight=False
        )
        assert invoice.status == InvoiceStatus.POSTED.value
        assert invoice.status != InvoiceStatus.EXPORTED.value

    def test_posting_twice_does_not_double_stock(self, session, company, user):
        from services.purchase_service import post_to_tally

        invoice, items = self._confirmed_invoice(session, company, user)
        post_to_tally(
            session, company, user, invoice, ExcelConnector(), run_preflight=False
        )
        product = session.query(Product).filter(Product.id == items[0].product_id).one()
        after_first = product.current_stock

        invoice.status = InvoiceStatus.CONFIRMED.value          # simulate a retry
        session.commit()
        result, _ = post_to_tally(
            session, company, user, invoice, ExcelConnector(), run_preflight=False
        )
        session.refresh(product)
        assert not result.success
        assert product.current_stock == after_first


class TestRegisterWorkbook:
    def _bills(self, session, company, user, count=2):
        from PIL import Image

        from services.ai_invoice_parser import InvoiceParser
        from services.ocr_service import MockOCRProvider
        from services.purchase_service import ingest_bill

        invoices = []
        for index in range(count):
            result = ingest_bill(
                session, company, user, f"b{index}.jpg",
                f"payload-{index}".encode(),
                [Image.new("RGB", (50, 50), "white")],
                InvoiceParser(MockOCRProvider()), store_image=False,
            )
            invoices.append(result.invoice)
        items = {
            invoice.id: session.query(InvoiceItem).filter(
                InvoiceItem.invoice_id == invoice.id
            ).all()
            for invoice in invoices
        }
        return invoices, items

    def test_has_every_sheet(self, session, company, user, tmp_path):
        invoices, items = self._bills(session, company, user)
        products = [make_product(session, company, "Widget", current_stock=10.0)]
        path = build_register_workbook(
            company, invoices, items, products, destination=tmp_path
        )
        names = openpyxl.load_workbook(path).sheetnames
        assert names == [
            "Purchase Register", "Item Detail", "GST Summary",
            "Supplier Summary", "Stock Position",
        ]

    def test_stock_sheet_omitted_when_not_wanted(self, session, company, user, tmp_path):
        invoices, items = self._bills(session, company, user, count=1)
        path = build_register_workbook(company, invoices, items, None, destination=tmp_path)
        assert "Stock Position" not in openpyxl.load_workbook(path).sheetnames

    def test_one_row_per_bill(self, session, company, user, tmp_path):
        invoices, items = self._bills(session, company, user, count=2)
        path = build_register_workbook(company, invoices, items, [], destination=tmp_path)
        sheet = openpyxl.load_workbook(path)["Purchase Register"]
        headers, _ = _table(sheet)
        data = _data_rows(sheet, headers)
        assert len(data) == 2

    def test_item_detail_has_a_row_per_line(self, session, company, user, tmp_path):
        invoices, items = self._bills(session, company, user, count=2)
        expected = sum(len(lines) for lines in items.values())
        path = build_register_workbook(company, invoices, items, [], destination=tmp_path)
        sheet = openpyxl.load_workbook(path)["Item Detail"]
        headers, _ = _table(sheet)
        assert len(_data_rows(sheet, headers)) == expected

    def test_gst_summary_groups_by_rate(self, session, company, user, tmp_path):
        invoices, items = self._bills(session, company, user, count=1)
        rates = {
            round(float(line.gst_rate or 0), 2)
            for lines in items.values() for line in lines
        }
        path = build_register_workbook(company, invoices, items, [], destination=tmp_path)
        sheet = openpyxl.load_workbook(path)["GST Summary"]
        headers, _ = _table(sheet)
        assert len(_data_rows(sheet, headers)) == len(rates)

    def test_totals_are_formulas(self, session, company, user, tmp_path):
        invoices, items = self._bills(session, company, user, count=2)
        path = build_register_workbook(company, invoices, items, [], destination=tmp_path)
        sheet = openpyxl.load_workbook(path)["Purchase Register"]
        formulas = [
            cell.value for row in sheet.iter_rows() for cell in row
            if isinstance(cell.value, str) and cell.value.startswith("=SUM(")
        ]
        assert formulas

    def test_survives_an_empty_period(self, company, tmp_path):
        """A customer picking a month with no bills must not get a crash."""
        path = build_register_workbook(company, [], {}, [], destination=tmp_path)
        assert Path(path).exists()

    def test_names_the_company(self, session, company, user, tmp_path):
        invoices, items = self._bills(session, company, user, count=1)
        path = build_register_workbook(company, invoices, items, [], destination=tmp_path)
        assert company.name in _all_text(path)

    def test_gst_sheet_disclaims_being_a_return(self, session, company, user, tmp_path):
        invoices, items = self._bills(session, company, user, count=1)
        path = build_register_workbook(company, invoices, items, [], destination=tmp_path)
        assert "statutory return" in _all_text(path).lower()



class TestSupplierSummary:
    """The supplier-wise report: one row per supplier, biggest first."""

    def _two_suppliers(self, session, company, user):
        made = []
        for name, taxable, total in (
            ("Alpha Traders", 1000.0, 1180.0),
            ("Beta Agencies", 5000.0, 5900.0),
            ("Alpha Traders", 2000.0, 2360.0),
        ):
            invoice = Invoice(
                company_id=company.id, supplier_name=name,
                supplier_gstin="22AAAAA0000A1Z5",
                invoice_number=f"{name[:3]}-{total:.0f}",
                invoice_date="2026-09-01", status="Posted",
                subtotal=taxable, grand_total=total, cess_total=0.0, round_off=0.0,
            )
            session.add(invoice)
            session.commit()
            session.add(InvoiceItem(
                company_id=company.id, invoice_id=invoice.id, raw_name="Item",
                quantity=1, rate=taxable, taxable_value=taxable,
                cgst=(total - taxable) / 2, sgst=(total - taxable) / 2,
                igst=0.0, cess=0.0, gst_rate=18.0, total=total,
            ))
            session.commit()
            made.append(invoice)
        items = {
            invoice.id: session.query(InvoiceItem).filter(
                InvoiceItem.invoice_id == invoice.id
            ).all()
            for invoice in made
        }
        return made, items

    def test_one_row_per_supplier(self, session, company, user, tmp_path):
        invoices, items = self._two_suppliers(session, company, user)
        path = build_register_workbook(company, invoices, items, [], destination=tmp_path)
        sheet = openpyxl.load_workbook(path)["Supplier Summary"]
        assert len(_data_rows(sheet)) == 2          # three bills, two suppliers

    def test_bills_are_counted_per_supplier(self, session, company, user, tmp_path):
        invoices, items = self._two_suppliers(session, company, user)
        path = build_register_workbook(company, invoices, items, [], destination=tmp_path)
        sheet = openpyxl.load_workbook(path)["Supplier Summary"]
        headers, _ = _table(sheet)
        by_name = {row[0]: row for row in _data_rows(sheet)}
        assert by_name["Alpha Traders"][headers.index("Bills")] == 2
        assert by_name["Beta Agencies"][headers.index("Bills")] == 1

    def test_totals_are_added_per_supplier(self, session, company, user, tmp_path):
        invoices, items = self._two_suppliers(session, company, user)
        path = build_register_workbook(company, invoices, items, [], destination=tmp_path)
        sheet = openpyxl.load_workbook(path)["Supplier Summary"]
        headers, _ = _table(sheet)
        by_name = {row[0]: row for row in _data_rows(sheet)}
        assert by_name["Alpha Traders"][headers.index("Grand Total")] == 3540.0
        assert by_name["Beta Agencies"][headers.index("Grand Total")] == 5900.0

    def test_biggest_supplier_is_listed_first(self, session, company, user, tmp_path):
        invoices, items = self._two_suppliers(session, company, user)
        path = build_register_workbook(company, invoices, items, [], destination=tmp_path)
        sheet = openpyxl.load_workbook(path)["Supplier Summary"]
        assert _data_rows(sheet)[0][0] == "Beta Agencies"     # 5900 beats 3540


class TestBillBundle:
    """One file per bill, zipped."""

    def test_holds_one_workbook_per_bill(self, payload, company, tmp_path):
        import zipfile

        from services.excel_export import build_bill_bundle

        second = VoucherPayload(
            **{**payload.__dict__, "invoice_number": "SBT/2026/0412",
               "supplier_ledger": "Shree Balaji Traders"}
        )
        archive = build_bill_bundle(company, [payload, second], destination=tmp_path)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
        assert len(names) == 2
        assert all(name.endswith(".xlsx") for name in names)

    def test_files_are_named_by_supplier_and_invoice(self, payload, company, tmp_path):
        import zipfile

        from services.excel_export import build_bill_bundle

        archive = build_bill_bundle(company, [payload], destination=tmp_path)
        with zipfile.ZipFile(archive) as bundle:
            name = bundle.namelist()[0]
        assert "Maa_Sharda_Agencies" in name
        assert "MSA_26-27_0138" in name

    def test_identical_bills_do_not_overwrite_each_other(self, payload, company, tmp_path):
        """Two bills sharing supplier, number and date must both survive."""
        import zipfile

        from services.excel_export import build_bill_bundle

        archive = build_bill_bundle(company, [payload, payload], destination=tmp_path)
        with zipfile.ZipFile(archive) as bundle:
            assert len(bundle.namelist()) == 2

    def test_each_workbook_opens(self, payload, company, tmp_path):
        import zipfile

        from services.excel_export import build_bill_bundle

        archive = build_bill_bundle(company, [payload], destination=tmp_path)
        extract = tmp_path / "out"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extract)
        for path in extract.glob("*.xlsx"):
            assert openpyxl.load_workbook(path).active.title == "Purchase Bill"

    def test_leaves_no_loose_files_behind(self, payload, company, tmp_path):
        from services.excel_export import build_bill_bundle

        build_bill_bundle(company, [payload, payload], destination=tmp_path)
        assert not list(tmp_path.glob("*.xlsx")), "only the ZIP should remain"

    def test_empty_list_still_makes_an_archive(self, company, tmp_path):
        import zipfile

        from services.excel_export import build_bill_bundle

        archive = build_bill_bundle(company, [], destination=tmp_path)
        with zipfile.ZipFile(archive) as bundle:
            assert bundle.namelist() == []


# --------------------------------------------------------------------------
def _all_text(path) -> str:
    workbook = openpyxl.load_workbook(path)
    parts = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(values_only=True):
            parts += [str(value) for value in row if value is not None]
    return " ".join(parts)


def _header_row(sheet):
    """Index of the table's header row.

    Found by shape rather than by a list of known names: the header is the
    first row of three or more text cells. That rule also skips the two-cell
    "Supplier | Maa Sharda" fact rows above the table on a bill sheet.
    """
    for index, values in enumerate(sheet.iter_rows(values_only=True), start=1):
        cells = [value for value in values if value not in (None, "")]
        if len(cells) >= 3 and all(isinstance(value, str) for value in cells):
            return index, [value for value in values if value is not None]
    return None, None


def _table(sheet):
    """(headers, first data row) of the written table."""
    index, headers = _header_row(sheet)
    if index is None:
        return None, None
    for values in sheet.iter_rows(min_row=index + 1, values_only=True):
        if values and values[0] is not None:
            return headers, values
    return headers, None


def _data_rows(sheet, headers=None):
    """Rows between the header and the TOTAL row."""
    index, _ = _header_row(sheet)
    if index is None:
        return []
    rows = []
    for values in sheet.iter_rows(min_row=index + 1, values_only=True):
        if not values or all(value is None for value in values):
            continue
        if any(str(value).strip().upper() == "TOTAL"
               for value in values if value is not None):
            break
        rows.append(values)
    return rows
