"""
Excel output for businesses that do not use Tally.

Three jobs live here, because customers file their paperwork differently:

  * `build_bill_workbook` — one purchase bill as a workbook, produced when the
    company's posting mode is "excel". This is the customer's record of the
    bill, not something to be imported anywhere.

  * `build_register_workbook` — the purchase register over whatever set of
    bills is passed in (a date range, one supplier, a hand-picked few), with
    item detail, GST and supplier summaries, and the stock position. This is
    what a customer hands to their accountant.

  * `build_bill_bundle` — the same bills as one workbook each, zipped, for a
    customer who files invoices individually rather than reading a register.

  * `build_manifest_workbook` — a transporter's manifest/bilty (ManifestRow
    list from services.manifest_parser), one row per consignment. This has
    no GST/stock angle at all — it is a plain, reviewable table, because the
    source is handwritten and some cells may need a human's eyes before the
    sheet is trusted for anything.

A note on stock. When a company posts to Tally, Tally is the book of record and
this app waits for it to accept the voucher before moving stock. A company on
Excel has no such system: *this* app is the book of record, so stock moves the
moment the bill is posted. The workbook is a report of what happened, not a
gate the stock waits behind. Treating it as a gate would leave the customer
pressing "I have imported this" against a file nobody imports.

Totals are written as real formulas so the customer can edit a quantity and
watch the sheet re-add itself. Per-row values come from the app.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from utils.config import settings
from utils.logging import get_logger

log = get_logger("excel")

# ---- house style ---------------------------------------------------------
BRAND = "0B4A6F"
HEAD_FILL = PatternFill("solid", fgColor=BRAND)
HEAD_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="Arial", size=14, bold=True, color=BRAND)
SUB_FONT = Font(name="Arial", size=9, italic=True, color="667085")
BODY = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
TOTAL_FILL = PatternFill("solid", fgColor="EAECF0")
THIN = Side(style="thin", color="D0D5DD")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

MONEY = '#,##0.00;[Red](#,##0.00);"-"'
QTY = '#,##0.###;[Red](#,##0.###);"-"'
DATE_FMT = "DD-MM-YYYY"


def safe_name(text: str, fallback: str = "export") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (text or "").strip()).strip("_")
    return cleaned or fallback


def _header_block(sheet: Worksheet, title: str, subtitle: str, width: int) -> int:
    """Title rows shared by every sheet. Returns the row the table starts on."""
    sheet["A1"] = title
    sheet["A1"].font = TITLE_FONT
    sheet["A2"] = subtitle
    sheet["A2"].font = SUB_FONT
    sheet["A3"] = (
        f"{settings.COMPANY_BRAND} · {settings.APP_NAME} · "
        f"generated {dt.date.today():%d-%m-%Y}"
    )
    sheet["A3"].font = SUB_FONT
    return 5


def _write_table(
    sheet: Worksheet,
    headers: Sequence[str],
    rows: Iterable[Sequence],
    start_row: int,
    widths: Optional[dict] = None,
    formats: Optional[dict] = None,
) -> int:
    """Write a bordered table. Returns the row after the last data row."""
    for column, name in enumerate(headers, start=1):
        cell = sheet.cell(row=start_row, column=column, value=name)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    row_number = start_row
    for row_number, values in enumerate(rows, start=start_row + 1):
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_number, column=column, value=value)
            cell.font, cell.border = BODY, BORDER
            name = headers[column - 1]
            if formats and name in formats:
                cell.number_format = formats[name]

    for column, name in enumerate(headers, start=1):
        letter = get_column_letter(column)
        sheet.column_dimensions[letter].width = (widths or {}).get(name, 14)

    sheet.freeze_panes = f"A{start_row + 1}"
    return row_number + 1


def _total_row(
    sheet: Worksheet,
    row: int,
    label_column: int,
    label: str,
    sum_columns: Sequence[int],
    first_data_row: int,
    last_data_row: int,
    number_format: str = MONEY,
) -> None:
    """A totals row built from real SUM formulas, so edits re-add themselves."""
    cell = sheet.cell(row=row, column=label_column, value=label)
    cell.font, cell.fill, cell.border = BOLD, TOTAL_FILL, BORDER
    for column in sum_columns:
        letter = get_column_letter(column)
        total = sheet.cell(row=row, column=column)
        if last_data_row >= first_data_row:
            total.value = f"=SUM({letter}{first_data_row}:{letter}{last_data_row})"
        else:
            total.value = 0
        total.font, total.fill, total.border = BOLD, TOTAL_FILL, BORDER
        total.number_format = number_format


# --------------------------------------------------------------------------
# One bill
# --------------------------------------------------------------------------
def append_bank_ledger_row(payload, destination: Optional[Path] = None) -> str:
    """Append one bank Payment/Receipt entry to a running ledger workbook —
    the Excel-mode equivalent of build_bill_workbook, but for bank
    reconciliation entries, which have no stock effect at all."""
    from openpyxl import load_workbook

    directory = Path(destination or settings.EXPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bank_ledger.xlsx"

    headers = ["Date", "Voucher Type", "Bank Ledger", "Party Ledger", "Amount", "Narration", "Reference"]
    if path.exists():
        workbook = load_workbook(path)
        sheet = workbook.active
    else:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Bank Ledger"
        for col, header in enumerate(headers, start=1):
            cell = sheet.cell(row=1, column=col, value=header)
            cell.font = HEAD_FONT
            cell.fill = HEAD_FILL
        for col, width in enumerate((12, 12, 22, 22, 14, 40, 18), start=1):
            sheet.column_dimensions[get_column_letter(col)].width = width

    next_row = sheet.max_row + 1
    sheet.cell(row=next_row, column=1, value=payload.voucher_date)
    sheet.cell(row=next_row, column=2, value=payload.voucher_type)
    sheet.cell(row=next_row, column=3, value=payload.bank_ledger)
    sheet.cell(row=next_row, column=4, value=payload.party_ledger)
    sheet.cell(row=next_row, column=5, value=float(payload.amount or 0)).number_format = MONEY
    sheet.cell(row=next_row, column=6, value=payload.narration)
    sheet.cell(row=next_row, column=7, value=payload.voucher_number or payload.reference_id)

    workbook.save(path)
    log.info("Bank ledger row appended to %s", path)
    return str(path)


def build_bill_workbook(payload, destination: Optional[Path] = None) -> str:
    """One purchase bill as a workbook, from a VoucherPayload."""
    directory = Path(destination or settings.EXPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Purchase Bill"

    headers = ["#", "Product", "HSN", "Qty", "Unit", "Rate", "Discount",
               "Taxable", "GST %", "CGST", "SGST", "IGST", "Cess", "Total"]
    widths = {"Product": 34, "HSN": 12, "Qty": 9, "Unit": 8, "Rate": 11,
              "Discount": 11, "Taxable": 13, "GST %": 8, "CGST": 11,
              "SGST": 11, "IGST": 11, "Cess": 10, "Total": 13, "#": 5}
    formats = {name: MONEY for name in
               ("Rate", "Discount", "Taxable", "CGST", "SGST", "IGST", "Cess", "Total")}
    formats["Qty"] = QTY

    start = _header_block(
        sheet,
        f"Purchase Bill — {payload.supplier_ledger}",
        f"Invoice {payload.invoice_number or '—'} dated "
        f"{payload.invoice_date or '—'}",
        len(headers),
    )

    # Supplier / invoice block above the table
    facts = [
        ("Supplier", payload.supplier_ledger),
        ("Invoice number", payload.invoice_number or "—"),
        ("Invoice date", payload.invoice_date or "—"),
        ("Company", payload.company_name or "—"),
    ]
    for offset, (label, value) in enumerate(facts):
        sheet.cell(row=start + offset, column=1, value=label).font = BOLD
        sheet.cell(row=start + offset, column=2, value=value).font = BODY
    table_start = start + len(facts) + 1

    rows = []
    for index, item in enumerate(payload.items, start=1):
        rows.append([
            index,
            item.get("stock_item", ""),
            item.get("hsn", ""),
            float(item.get("quantity") or 0),
            item.get("unit", ""),
            float(item.get("rate") or 0),
            float(item.get("discount") or 0),
            float(item.get("taxable_value") or 0),
            float(item.get("gst_rate") or 0),
            float(item.get("cgst") or 0),
            float(item.get("sgst") or 0),
            float(item.get("igst") or 0),
            float(item.get("cess") or 0),
            float(item.get("total") or 0),
        ])

    after = _write_table(sheet, headers, rows, table_start, widths, formats)
    first_data, last_data = table_start + 1, table_start + len(rows)
    money_columns = [headers.index(name) + 1 for name in
                     ("Discount", "Taxable", "CGST", "SGST", "IGST", "Cess", "Total")]
    _total_row(sheet, after, 2, "TOTAL", money_columns, first_data, last_data)

    # Tax ledgers and the grand total, as the bill itself states them
    summary_row = after + 2
    sheet.cell(row=summary_row, column=headers.index("Total"), value="Round off").font = BODY
    sheet.cell(row=summary_row, column=headers.index("Total") + 1,
               value=float(payload.round_off_amount or 0)).number_format = MONEY
    grand_row = summary_row + 1
    label = sheet.cell(row=grand_row, column=headers.index("Total"), value="GRAND TOTAL")
    label.font, label.fill = BOLD, TOTAL_FILL
    total_cell = sheet.cell(row=grand_row, column=headers.index("Total") + 1,
                            value=float(payload.grand_total or 0))
    total_cell.font, total_cell.fill = BOLD, TOTAL_FILL
    total_cell.number_format = MONEY

    note_row = grand_row + 3
    sheet.cell(
        row=note_row, column=1,
        value="Stock in the app was updated when this bill was posted. "
              "This sheet is your record of the bill; there is nothing to import.",
    ).font = SUB_FONT

    filename = (
        f"purchase_{safe_name(payload.invoice_number, 'bill')}_"
        f"{dt.datetime.now():%Y%m%d%H%M%S}.xlsx"
    )
    path = directory / filename
    workbook.save(path)
    log.info("Bill workbook written to %s", path)
    return str(path)


# --------------------------------------------------------------------------
# The purchase register
# --------------------------------------------------------------------------
def build_register_workbook(
    company,
    invoices: List,
    items_by_invoice: dict,
    products: Optional[List] = None,
    destination: Optional[Path] = None,
) -> str:
    """Every bill over a period: register, item detail, GST summary, stock."""
    directory = Path(destination or settings.EXPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()

    # ---- 1. Purchase register --------------------------------------------
    register = workbook.active
    register.title = "Purchase Register"
    headers = ["Date", "Supplier", "GSTIN", "Invoice No", "Items",
               "Taxable", "CGST", "SGST", "IGST", "Cess", "Round Off",
               "Grand Total", "Status"]
    widths = {"Date": 12, "Supplier": 28, "GSTIN": 18, "Invoice No": 18,
              "Items": 7, "Taxable": 13, "CGST": 12, "SGST": 12, "IGST": 12,
              "Cess": 10, "Round Off": 11, "Grand Total": 14, "Status": 14}
    formats = {name: MONEY for name in
               ("Taxable", "CGST", "SGST", "IGST", "Cess", "Round Off", "Grand Total")}

    start = _header_block(
        register, f"Purchase Register — {company.name}",
        f"{len(invoices)} bill(s)", len(headers),
    )

    rows = []
    for invoice in invoices:
        lines = items_by_invoice.get(invoice.id, [])
        rows.append([
            invoice.invoice_date or "",
            invoice.supplier_name or "",
            invoice.supplier_gstin or "",
            invoice.invoice_number or "",
            len(lines),
            float(invoice.subtotal or 0),
            float(sum(line.cgst or 0 for line in lines)),
            float(sum(line.sgst or 0 for line in lines)),
            float(sum(line.igst or 0 for line in lines)),
            float(invoice.cess_total or 0),
            float(invoice.round_off or 0),
            float(invoice.grand_total or 0),
            invoice.status or "",
        ])

    after = _write_table(register, headers, rows, start, widths, formats)
    first_data, last_data = start + 1, start + len(rows)
    money_columns = [headers.index(name) + 1 for name in
                     ("Taxable", "CGST", "SGST", "IGST", "Cess", "Round Off", "Grand Total")]
    _total_row(register, after, 2, "TOTAL", money_columns, first_data, last_data)
    register.auto_filter.ref = (
        f"A{start}:{get_column_letter(len(headers))}{max(last_data, start)}"
    )

    # ---- 2. Item detail ---------------------------------------------------
    detail = workbook.create_sheet("Item Detail")
    headers = ["Date", "Supplier", "Invoice No", "Product", "HSN", "Qty",
               "Unit", "Rate", "Discount", "Taxable", "GST %",
               "CGST", "SGST", "IGST", "Total"]
    widths = {"Date": 12, "Supplier": 24, "Invoice No": 16, "Product": 32,
              "HSN": 11, "Qty": 9, "Unit": 7, "Rate": 11, "Discount": 10,
              "Taxable": 12, "GST %": 8, "CGST": 11, "SGST": 11, "IGST": 11,
              "Total": 13}
    formats = {name: MONEY for name in
               ("Rate", "Discount", "Taxable", "CGST", "SGST", "IGST", "Total")}
    formats["Qty"] = QTY

    start = _header_block(
        detail, f"Item Detail — {company.name}",
        "One row per line item across every bill", len(headers),
    )
    rows = []
    for invoice in invoices:
        for line in items_by_invoice.get(invoice.id, []):
            rows.append([
                invoice.invoice_date or "",
                invoice.supplier_name or "",
                invoice.invoice_number or "",
                line.raw_name or "",
                line.hsn or "",
                float(line.quantity or 0),
                line.unit or "",
                float(line.rate or 0),
                float(line.discount or 0),
                float(line.taxable_value or 0),
                float(line.gst_rate or 0),
                float(line.cgst or 0),
                float(line.sgst or 0),
                float(line.igst or 0),
                float(line.total or 0),
            ])
    after = _write_table(detail, headers, rows, start, widths, formats)
    first_data, last_data = start + 1, start + len(rows)
    money_columns = [headers.index(name) + 1 for name in
                     ("Discount", "Taxable", "CGST", "SGST", "IGST", "Total")]
    _total_row(detail, after, 3, "TOTAL", money_columns, first_data, last_data)
    detail.auto_filter.ref = (
        f"A{start}:{get_column_letter(len(headers))}{max(last_data, start)}"
    )

    # ---- 3. GST summary ---------------------------------------------------
    gst = workbook.create_sheet("GST Summary")
    headers = ["GST %", "Taxable", "CGST", "SGST", "IGST", "Cess", "Total Tax"]
    widths = {"GST %": 10, "Taxable": 15, "CGST": 14, "SGST": 14,
              "IGST": 14, "Cess": 12, "Total Tax": 15}
    start = _header_block(
        gst, f"GST Summary — {company.name}",
        "Grouped by tax rate. A working summary, not a statutory return.",
        len(headers),
    )

    buckets: dict = {}
    for invoice in invoices:
        for line in items_by_invoice.get(invoice.id, []):
            rate = round(float(line.gst_rate or 0), 2)
            bucket = buckets.setdefault(
                rate, {"taxable": 0.0, "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "cess": 0.0}
            )
            bucket["taxable"] += float(line.taxable_value or 0)
            bucket["cgst"] += float(line.cgst or 0)
            bucket["sgst"] += float(line.sgst or 0)
            bucket["igst"] += float(line.igst or 0)
            bucket["cess"] += float(line.cess or 0)

    rows = []
    for rate in sorted(buckets):
        bucket = buckets[rate]
        rows.append([
            rate, round(bucket["taxable"], 2), round(bucket["cgst"], 2),
            round(bucket["sgst"], 2), round(bucket["igst"], 2),
            round(bucket["cess"], 2), None,      # filled with a formula below
        ])
    formats = {name: MONEY for name in
               ("Taxable", "CGST", "SGST", "IGST", "Cess", "Total Tax")}
    after = _write_table(gst, headers, rows, start, widths, formats)

    # Total Tax per row as a formula, so an edited component re-adds itself.
    for offset in range(len(rows)):
        row = start + 1 + offset
        cell = gst.cell(row=row, column=7)
        cell.value = f"=SUM(C{row}:F{row})"
        cell.font, cell.border = BODY, BORDER
        cell.number_format = MONEY

    first_data, last_data = start + 1, start + len(rows)
    _total_row(gst, after, 1, "TOTAL", [2, 3, 4, 5, 6, 7], first_data, last_data)
    gst.cell(row=after + 2, column=1,
             value="Figures come from the bills recorded in this app. Check them "
                   "against your own records before filing anything.").font = SUB_FONT

    # ---- 4. Supplier summary ---------------------------------------------
    by_supplier = workbook.create_sheet("Supplier Summary")
    headers = ["Supplier", "GSTIN", "Bills", "Taxable", "CGST", "SGST",
               "IGST", "Cess", "Grand Total"]
    widths = {"Supplier": 32, "GSTIN": 18, "Bills": 8, "Taxable": 15,
              "CGST": 13, "SGST": 13, "IGST": 13, "Cess": 11, "Grand Total": 16}
    formats = {name: MONEY for name in
               ("Taxable", "CGST", "SGST", "IGST", "Cess", "Grand Total")}
    start = _header_block(
        by_supplier, f"Supplier Summary — {company.name}",
        "What you bought from each supplier over this period", len(headers),
    )

    totals: dict = {}
    for invoice in invoices:
        name = invoice.supplier_name or "Unknown"
        entry = totals.setdefault(name, {
            "gstin": invoice.supplier_gstin or "", "bills": 0, "taxable": 0.0,
            "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "cess": 0.0, "total": 0.0,
        })
        lines = items_by_invoice.get(invoice.id, [])
        entry["bills"] += 1
        entry["taxable"] += float(invoice.subtotal or 0)
        entry["cgst"] += float(sum(line.cgst or 0 for line in lines))
        entry["sgst"] += float(sum(line.sgst or 0 for line in lines))
        entry["igst"] += float(sum(line.igst or 0 for line in lines))
        entry["cess"] += float(invoice.cess_total or 0)
        entry["total"] += float(invoice.grand_total or 0)
        if not entry["gstin"] and invoice.supplier_gstin:
            entry["gstin"] = invoice.supplier_gstin

    rows = []
    for name in sorted(totals, key=lambda key: -totals[key]["total"]):
        entry = totals[name]
        rows.append([
            name, entry["gstin"], entry["bills"], round(entry["taxable"], 2),
            round(entry["cgst"], 2), round(entry["sgst"], 2),
            round(entry["igst"], 2), round(entry["cess"], 2),
            round(entry["total"], 2),
        ])
    after = _write_table(by_supplier, headers, rows, start, widths, formats)
    first_data, last_data = start + 1, start + len(rows)
    _total_row(by_supplier, after, 1, "TOTAL", [3, 4, 5, 6, 7, 8, 9],
               first_data, last_data)
    # Bill counts are whole numbers, not rupees.
    by_supplier.cell(row=after, column=3).number_format = "#,##0"
    by_supplier.auto_filter.ref = (
        f"A{start}:{get_column_letter(len(headers))}{max(last_data, start)}"
    )

    # ---- 5. Stock position ------------------------------------------------
    if products is not None:
        stock = workbook.create_sheet("Stock Position")
        headers = ["Product", "SKU", "Barcode", "HSN", "Unit",
                   "GST %", "Purchase Rate", "Stock", "Stock Value"]
        widths = {"Product": 34, "SKU": 12, "Barcode": 16, "HSN": 12,
                  "Unit": 8, "GST %": 8, "Purchase Rate": 14, "Stock": 11,
                  "Stock Value": 15}
        formats = {"Purchase Rate": MONEY, "Stock": QTY, "Stock Value": MONEY}
        start = _header_block(
            stock, f"Stock Position — {company.name}",
            "As held in this app right now", len(headers),
        )
        rows = []
        for product in products:
            rows.append([
                product.name or "", product.sku or "", product.barcode or "",
                product.hsn or "", product.unit or "",
                float(product.gst_rate or 0),
                float(product.purchase_rate or 0),
                float(product.current_stock or 0),
                None,                      # formula below
            ])
        after = _write_table(stock, headers, rows, start, widths, formats)
        for offset in range(len(rows)):
            row = start + 1 + offset
            cell = stock.cell(row=row, column=9)
            cell.value = f"=G{row}*H{row}"          # rate × stock
            cell.font, cell.border = BODY, BORDER
            cell.number_format = MONEY
        first_data, last_data = start + 1, start + len(rows)
        _total_row(stock, after, 1, "TOTAL", [9], first_data, last_data)
        stock.auto_filter.ref = (
            f"A{start}:{get_column_letter(len(headers))}{max(last_data, start)}"
        )

    filename = (
        f"purchase_register_{safe_name(company.name, 'company')}_"
        f"{dt.datetime.now():%Y%m%d%H%M%S}.xlsx"
    )
    path = directory / filename
    workbook.save(path)
    log.info("Register workbook written to %s (%d bills)", path, len(invoices))
    return str(path)


# --------------------------------------------------------------------------
# One file per bill
# --------------------------------------------------------------------------
def build_bill_bundle(
    company,
    payloads: Sequence,
    destination: Optional[Path] = None,
) -> str:
    """A ZIP holding one workbook per bill.

    For a customer who files bills individually — one sheet per supplier
    invoice, named so it can be found later — rather than reading a register.
    The workbooks are built in a scratch folder and only the ZIP is kept, so a
    hundred-bill export does not leave a hundred loose files behind.
    """
    import shutil
    import tempfile
    import zipfile

    directory = Path(destination or settings.EXPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)

    archive = directory / (
        f"bills_{safe_name(company.name, 'company')}_"
        f"{dt.datetime.now():%Y%m%d%H%M%S}.zip"
    )
    scratch = Path(tempfile.mkdtemp(prefix="billexport_"))
    try:
        written = []
        for payload in payloads:
            path = Path(build_bill_workbook(payload, destination=scratch))
            # Name each file so a folder of them sorts by supplier, then date.
            nice = (
                f"{safe_name(payload.supplier_ledger, 'supplier')[:40]}_"
                f"{safe_name(payload.invoice_number, 'bill')[:30]}_"
                f"{safe_name(payload.invoice_date, 'nodate')}.xlsx"
            )
            written.append((path, nice))

        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            used = set()
            for path, nice in written:
                # Two bills can share a supplier, number and date only if the
                # data is duplicated; keep both rather than silently dropping
                # one inside the archive.
                name, suffix = nice.rsplit(".", 1)
                candidate, counter = nice, 2
                while candidate in used:
                    candidate = f"{name}_{counter}.{suffix}"
                    counter += 1
                used.add(candidate)
                bundle.write(path, candidate)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    log.info("Bill bundle written to %s (%d bills)", archive, len(payloads))
    return str(archive)


# --------------------------------------------------------------------------
# Transport manifest / bilty
# --------------------------------------------------------------------------
DATE_DISPLAY = "DD/MM/YYYY"


def _date_value(text):
    """A manifest date as a real Excel date (shown DD/MM/YYYY, sortable and
    filterable), or the text itself when it is not a date we can read."""
    from services.manifest_parser import parse_date

    return parse_date(text) or (text or "")


def _date_label(text) -> str:
    from services.manifest_parser import normalize_date

    return normalize_date(text)


MANIFEST_HEADERS = [
    "S.No", "C.No.", "Station", "Consignee", "Weight",
    "No. of Pkg", "To Pay", "Paid", "Amount", "Confidence",
]
MANIFEST_WIDTHS = {
    "S.No": 6, "C.No.": 12, "Station": 14, "Consignee": 24, "Weight": 9,
    "No. of Pkg": 11, "To Pay": 11, "Paid": 10, "Amount": 11, "Confidence": 11,
}
LOW_CONFIDENCE_FILL = PatternFill("solid", fgColor="FFF2CC")
LOW_CONFIDENCE_THRESHOLD = 0.80


def _write_manifest_sheet(sheet: Worksheet, manifest) -> None:
    """Lay one manifest out on `sheet`: title block, note, table, totals."""
    subtitle_bits = []
    if manifest.manifest_date:
        subtitle_bits.append(f"Dated {_date_label(manifest.manifest_date)}")
    if manifest.from_station or manifest.to_station:
        subtitle_bits.append(f"{manifest.from_station or '—'} → {manifest.to_station or '—'}")
    start = _header_block(
        sheet,
        f"Manifest No. {manifest.manifest_no or '—'}",
        " · ".join(subtitle_bits) or "—",
        len(MANIFEST_HEADERS),
    )

    note_row = start
    sheet.cell(
        row=note_row, column=1,
        value="Yellow rows scored below 80% confidence from the handwriting — "
              "check these against the original bilty before using this sheet.",
    ).font = SUB_FONT
    table_start = note_row + 2

    rows = []
    for r in manifest.rows:
        rows.append([
            r.s_no, r.c_no, r.station, r.consignee, r.weight,
            r.no_of_pkg, r.to_pay, r.paid, r.amount,
            f"{r.confidence * 100:.0f}%" if r.confidence else "",
        ])

    after = _write_table(sheet, MANIFEST_HEADERS, rows, table_start, MANIFEST_WIDTHS)

    for offset, r in enumerate(manifest.rows):
        if r.confidence and r.confidence < LOW_CONFIDENCE_THRESHOLD:
            row_number = table_start + 1 + offset
            for column in range(1, len(MANIFEST_HEADERS) + 1):
                sheet.cell(row=row_number, column=column).fill = LOW_CONFIDENCE_FILL

    first_data, last_data = table_start + 1, table_start + len(rows)
    # Two different units in this row (weight vs. rupees), so write each
    # column's own SUM directly instead of reusing _total_row's single
    # label + single number-format shape, which would force both onto one
    # format and leave the label sitting between the two totals.
    label_cell = sheet.cell(row=after, column=1, value="TOTAL (numeric cells only)")
    label_cell.font, label_cell.fill, label_cell.border = BOLD, TOTAL_FILL, BORDER
    for column in range(2, len(MANIFEST_HEADERS) + 1):
        sheet.cell(row=after, column=column).fill = TOTAL_FILL
        sheet.cell(row=after, column=column).border = BORDER
    for name, number_format in (("Weight", QTY), ("To Pay", MONEY)):
        column = MANIFEST_HEADERS.index(name) + 1
        letter = get_column_letter(column)
        cell = sheet.cell(row=after, column=column)
        cell.value = f"=SUM({letter}{first_data}:{letter}{last_data})" if last_data >= first_data else 0
        cell.font, cell.fill, cell.border = BOLD, TOTAL_FILL, BORDER
        cell.number_format = number_format


def build_manifest_workbook(manifest, destination: Optional[Path] = None) -> str:
    """A transporter's manifest/bilty as a plain, reviewable Excel sheet.

    `manifest` is a services.manifest_parser.ParsedManifest. Rows below the
    confidence threshold are highlighted so a human checks them against the
    original before the figures go anywhere else — the source is
    handwritten, so this sheet is a first draft, not a certified record.
    """
    directory = Path(destination or settings.EXPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Manifest"
    _write_manifest_sheet(sheet, manifest)

    filename = (
        f"manifest_{safe_name(manifest.manifest_no, 'bilty')}_"
        f"{dt.datetime.now():%Y%m%d%H%M%S}.xlsx"
    )
    path = directory / filename
    workbook.save(path)
    log.info("Manifest workbook written to %s (%d rows)", path, len(manifest.rows))
    return str(path)


# --------------------------------------------------------------------------
# Several manifests, one workbook
# --------------------------------------------------------------------------
COMBINED_HEAD = ["Bill #", "Manifest No.", "Date", "From", "To"]
COMBINED_HEADERS = COMBINED_HEAD + MANIFEST_HEADERS
COMBINED_WIDTHS = {
    "Bill #": 7, "Manifest No.": 13, "Date": 12, "From": 14, "To": 14,
    **MANIFEST_WIDTHS,
}
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def _unique_sheet_name(label: str, used: set) -> str:
    """Excel sheet names: max 31 chars, none of []:*?/\\, unique per workbook."""
    base = _BAD_SHEET_CHARS.sub("-", label).strip(" '") or "Manifest"
    base = base[:31]
    name, counter = base, 2
    while name.lower() in used:
        suffix = f" ({counter})"
        name = base[: 31 - len(suffix)] + suffix
        counter += 1
    used.add(name.lower())
    return name


def build_multi_manifest_workbook(manifests: Sequence, destination: Optional[Path] = None) -> str:
    """Any number of manifests in ONE workbook.

    A single manifest produces exactly what build_manifest_workbook does, so
    "one bill, one Excel" is unchanged. Two or more produce:

      * "All Manifests" - every consignment from every bill in one flat,
        filterable table, each row tagged with its Bill # / Manifest No. /
        date / route, with grand totals;
      * "Summary" - one line per bill (rows, weight, to-pay), the totals
        coming from SUMIF over the All Manifests sheet so an edit there
        re-adds itself;
      * one sheet per bill, laid out exactly like the single-bill sheet.
    """
    manifests = list(manifests)
    if not manifests:
        raise ValueError("No manifests to export.")
    if len(manifests) == 1:
        return build_manifest_workbook(manifests[0], destination)

    directory = Path(destination or settings.EXPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    combined = workbook.active
    combined.title = "All Manifests"
    summary = workbook.create_sheet("Summary")
    used = {"all manifests", "summary"}

    # ---- All Manifests --------------------------------------------------
    start = _header_block(
        combined, "All Manifests",
        f"{len(manifests)} bills combined · {sum(len(m.rows) for m in manifests)} consignments",
        len(COMBINED_HEADERS),
    )
    combined.cell(
        row=start, column=1,
        value="Yellow rows scored below 80% confidence from the handwriting — "
              "check these against the original bilty before using this sheet.",
    ).font = SUB_FONT
    table_start = start + 2

    # S.No runs on from one bill to the next (1..6, then 7..12, ...) so the
    # combined sheet reads as one list. Each bill's own sheet keeps the number
    # printed on that paper.
    rows, low, serial = [], [], 0
    for number, manifest in enumerate(manifests, start=1):
        route = (manifest.from_station or "", manifest.to_station or "")
        for r in manifest.rows:
            serial += 1
            rows.append([
                number, manifest.manifest_no or "", _date_value(manifest.manifest_date),
                route[0], route[1],
                serial, r.c_no, r.station, r.consignee, r.weight,
                r.no_of_pkg, r.to_pay, r.paid, r.amount,
                f"{r.confidence * 100:.0f}%" if r.confidence else "",
            ])
            low.append(bool(r.confidence and r.confidence < LOW_CONFIDENCE_THRESHOLD))

    after = _write_table(combined, COMBINED_HEADERS, rows, table_start, COMBINED_WIDTHS,
                         {"Date": DATE_DISPLAY})
    for offset, is_low in enumerate(low):
        if is_low:
            for column in range(1, len(COMBINED_HEADERS) + 1):
                combined.cell(row=table_start + 1 + offset, column=column).fill = LOW_CONFIDENCE_FILL

    first_data, last_data = table_start + 1, table_start + len(rows)
    label = combined.cell(row=after, column=1, value="GRAND TOTAL (numeric cells only)")
    label.font, label.fill, label.border = BOLD, TOTAL_FILL, BORDER
    for column in range(2, len(COMBINED_HEADERS) + 1):
        combined.cell(row=after, column=column).fill = TOTAL_FILL
        combined.cell(row=after, column=column).border = BORDER
    col_of = {name: index + 1 for index, name in enumerate(COMBINED_HEADERS)}
    for name, number_format in (("Weight", QTY), ("To Pay", MONEY)):
        letter = get_column_letter(col_of[name])
        cell = combined.cell(row=after, column=col_of[name])
        cell.value = f"=SUM({letter}{first_data}:{letter}{last_data})" if last_data >= first_data else 0
        cell.font, cell.fill, cell.border = BOLD, TOTAL_FILL, BORDER
        cell.number_format = number_format
    combined.auto_filter.ref = (
        f"A{table_start}:{get_column_letter(len(COMBINED_HEADERS))}{max(last_data, table_start)}"
    )

    # ---- Summary ----------------------------------------------------------
    sum_headers = ["Bill #", "Manifest No.", "Date", "From", "To",
                   "Consignments", "Weight", "To Pay"]
    sum_widths = {"Bill #": 7, "Manifest No.": 13, "Date": 12, "From": 14,
                  "To": 14, "Consignments": 14, "Weight": 11, "To Pay": 13}
    sum_start = _header_block(
        summary, "Summary", f"One line per bill - {len(manifests)} bills", len(sum_headers)
    )
    bill_col = get_column_letter(col_of["Bill #"])
    rng = lambda name: (  # noqa: E731 - tiny local helper
        f"'All Manifests'!${get_column_letter(col_of[name])}${first_data}:"
        f"${get_column_letter(col_of[name])}${max(last_data, first_data)}"
    )
    bill_rng = (
        f"'All Manifests'!${bill_col}${first_data}:${bill_col}${max(last_data, first_data)}"
    )
    srows = []
    for number, manifest in enumerate(manifests, start=1):
        srows.append([
            number, manifest.manifest_no or "", _date_value(manifest.manifest_date),
            manifest.from_station or "", manifest.to_station or "",
            None, None, None,                                   # formulas below
        ])
    s_after = _write_table(summary, sum_headers, srows, sum_start, sum_widths,
                           {"Date": DATE_DISPLAY})
    for offset in range(len(manifests)):
        row = sum_start + 1 + offset
        formulas = (
            (6, f"=COUNTIF({bill_rng},A{row})", "#,##0"),
            (7, f"=SUMIF({bill_rng},A{row},{rng('Weight')})", QTY),
            (8, f"=SUMIF({bill_rng},A{row},{rng('To Pay')})", MONEY),
        )
        for column, formula, number_format in formulas:
            cell = summary.cell(row=row, column=column, value=formula)
            cell.font, cell.border = BODY, BORDER
            cell.number_format = number_format
    s_first, s_last = sum_start + 1, sum_start + len(manifests)
    _total_row(summary, s_after, 1, "TOTAL", [6, 7, 8], s_first, s_last)
    summary.cell(row=s_after, column=6).number_format = "#,##0"
    summary.cell(row=s_after, column=7).number_format = QTY
    for column in (2, 3, 4, 5):
        summary.cell(row=s_after, column=column).fill = TOTAL_FILL
        summary.cell(row=s_after, column=column).border = BORDER

    # ---- one sheet per bill ------------------------------------------------
    for number, manifest in enumerate(manifests, start=1):
        sheet = workbook.create_sheet(
            _unique_sheet_name(f"{number} - {manifest.manifest_no or 'bilty'}", used)
        )
        _write_manifest_sheet(sheet, manifest)

    filename = (
        f"manifests_{len(manifests)}bills_{dt.datetime.now():%Y%m%d%H%M%S}.xlsx"
    )
    path = directory / filename
    workbook.save(path)
    log.info("Combined manifest workbook written to %s (%d bills, %d rows)",
             path, len(manifests), len(rows))
    return str(path)
