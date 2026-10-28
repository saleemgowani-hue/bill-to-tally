"""
Sales: the mirror of Upload Bill + Review Bills + Purchase History, for
sales invoices instead of purchase bills. Photo or Excel in, GST/arithmetic
validation and product matching (same engine as Purchase), a review screen,
then Tally posting (Sales voucher) or XML export — stock decreases instead
of increasing.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import func

from app_pages.upload_bill import build_parser, first_load_notice
from database.database import get_setting
from database.models import (
    AuditLog,
    InvoiceStatus,
    MatchStatus,
    OCRResult,
    Product,
    SalesInvoice,
    SalesInvoiceItem,
    SalesTransaction,
    User,
)
from services import audit_service
from services.excel_import import ExcelImportError, sample_template_bytes
from services.invoice_validator import InvoiceValidator
from services.ocr_service import OCRError, PROVIDER_LABELS
from services.product_matcher import ProductMatcher
from services.sales_service import (
    build_sales_voucher_payload,
    cancel_sales_invoice,
    confirm_sales_invoice,
    confirm_sales_tally_import,
    ingest_sales_bill,
    ingest_sales_excel,
    recalculate_sales_totals,
    revalidate_sales,
    unresolved_sales_items,
)
from services.tally_connector import build_voucher_xml, get_connector
from utils.config import settings
from utils.image_tools import to_images
from utils.security import safe_filename, validate_upload
from utils.ui import (
    brand_header,
    confidence_light,
    friendly_error,
    match_badge,
    require_login,
    rupees,
    session_db,
    show_issues,
    status_badge,
    test_mode_banner,
)

OPEN_STATUSES = [
    InvoiceStatus.REVIEW_REQUIRED.value,
    InvoiceStatus.PROCESSING.value,
    InvoiceStatus.CONFIRMED.value,
    InvoiceStatus.EXPORTED.value,
    InvoiceStatus.DUPLICATE.value,
    InvoiceStatus.FAILED.value,
]
ALL_STATUSES = [s.value for s in InvoiceStatus]


def render() -> None:
    user, company = require_login()
    db = session_db()
    brand_header(f"{company.name} · Sales")

    tab_upload, tab_review, tab_history = st.tabs(
        ["📤 Upload", "🔎 Review & Post", "📚 History"]
    )
    with tab_upload:
        _upload_tab(db, company, user)
    with tab_review:
        _review_tab(db, company, user)
    with tab_history:
        _history_tab(db, company, user)


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
def _upload_tab(db, company, user) -> None:
    sub_photo, sub_excel = st.tabs(["📷 Bill photo / PDF", "📊 Excel / CSV"])
    with sub_photo:
        _photo_upload(db, company, user)
    with sub_excel:
        _excel_upload(db, company, user)


def _photo_upload(db, company, user) -> None:
    parser, provider_name = build_parser(db, company)
    st.caption(
        f"Extraction engine: **{PROVIDER_LABELS.get(provider_name, provider_name)}** "
        "(same as Purchase — change it under Settings → AI / OCR)."
    )
    first_load_notice(provider_name)
    if provider_name == "mock":
        st.warning(
            "The mock engine is active — it returns fixed sample data for testing, "
            "not a real reading of your bill.", icon="🧪",
        )

    files = st.file_uploader(
        "Sales invoices (JPG, PNG, WEBP or PDF — multiple allowed)",
        type=["jpg", "jpeg", "png", "webp", "pdf"],
        accept_multiple_files=True,
        key="sales_bill_uploader",
    )
    if not files:
        st.info("Drop one or more sales invoice photos/PDFs here.")
        return

    if not st.button("Extract all invoices", type="primary", key="sales_extract_all"):
        return

    progress = st.progress(0.0, text="Starting…")
    for index, uploaded in enumerate(files, start=1):
        progress.progress((index - 1) / len(files), text=f"Processing {uploaded.name}")
        data = uploaded.getvalue()
        ok, message = validate_upload(uploaded.name, data)
        if not ok:
            st.error(f"{uploaded.name}: {message}")
            continue
        try:
            pages = to_images(uploaded.name, data)
        except Exception as exc:
            st.error(f"{uploaded.name}: could not read the file ({exc}).")
            continue
        if not pages:
            st.error(f"{uploaded.name}: no readable pages found.")
            continue

        try:
            result = ingest_sales_bill(
                db, company, user, uploaded.name, data, pages, parser, InvoiceValidator()
            )
        except OCRError as exc:
            audit_service.record_error(db, company.id, "ocr", str(exc), exc)
            st.error(f"**{uploaded.name}: the OCR engine could not process this bill.**\n\n{exc}")
            continue
        except Exception as exc:
            audit_service.record_error(db, company.id, "ingest", str(exc), exc)
            friendly_error("Extraction", exc)
            continue

        _render_ingest_summary(uploaded.name, result)
    progress.progress(1.0, text="Done")


def _excel_upload(db, company, user) -> None:
    st.caption(
        "Same importer as Purchase, aimed at customers instead of suppliers. "
        "Required column: Item Name. Rows are grouped into bills by "
        "Customer + Invoice Number."
    )
    st.download_button(
        "⬇️ Download sample template",
        data=sample_template_bytes(),
        file_name="sales_import_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="sales_template_dl",
    )

    file = st.file_uploader(
        "Sales sheet (XLSX, XLS or CSV)", type=["xlsx", "xls", "csv"], key="sales_excel_uploader",
    )
    if not file:
        return
    raw = file.getvalue()
    filename = safe_filename(file.name)
    st.markdown(f"**{filename}** · {len(raw)/1024:.0f} KB")

    if not st.button("Import", type="primary", key="sales_excel_import_btn"):
        return
    try:
        results = ingest_sales_excel(db, company, user, filename, raw, InvoiceValidator())
    except ExcelImportError as exc:
        st.error(f"**Could not import this sheet.**\n\n{exc}")
        return
    except Exception as exc:
        audit_service.record_error(db, company.id, "sales_excel_import", str(exc), exc)
        friendly_error("Excel import", exc)
        return

    st.success(f"Imported {len(results)} invoice(s) from '{filename}'.")
    for result in results:
        for warning in result.warnings:
            st.info(warning)
        _render_ingest_summary(filename, result)


def _render_ingest_summary(filename, result) -> None:
    invoice = result.invoice
    st.session_state.review_sales_invoice_id = invoice.id
    st.markdown(
        f"**{filename} → {invoice.customer_name or 'Unknown'} / "
        f"{invoice.invoice_number or 'no invoice number'}** → "
        f"{status_badge(invoice.status)}",
        unsafe_allow_html=True,
    )

    if result.duplicate_reason:
        st.error(f"⚠️ Possible duplicate invoice detected. {result.duplicate_reason}")

    cols = st.columns(4)
    cols[0].metric("Customer", (invoice.customer_name or "—")[:22])
    cols[1].metric("Invoice no.", invoice.invoice_number or "—")
    cols[2].metric("Items", len(invoice.items))
    cols[3].metric(
        "Total", rupees(invoice.grand_total),
        f"{confidence_light(invoice.overall_confidence)} {(invoice.overall_confidence or 0):.0%}",
    )

    errors = len(result.report.errors)
    warnings_count = len(result.report.warnings)
    if errors:
        st.error(f"{errors} blocking issue(s) and {warnings_count} warning(s) need review.")
    elif warnings_count:
        st.warning(f"{warnings_count} field(s) need a quick check.")
    else:
        st.success("Arithmetic and GST checks passed. Verify and confirm.")

    if st.button("Open in Review & Post", key=f"open_sales_{invoice.id}"):
        st.session_state.review_sales_invoice_id = invoice.id
        st.rerun()


# --------------------------------------------------------------------------
# Review & Post
# --------------------------------------------------------------------------
def _review_tab(db, company, user) -> None:
    open_invoices = (
        db.query(SalesInvoice)
        .filter(SalesInvoice.company_id == company.id, SalesInvoice.status.in_(OPEN_STATUSES))
        .order_by(SalesInvoice.created_at.desc())
        .all()
    )
    if not open_invoices:
        st.info("No sales invoices are waiting for review right now.")
        return

    default_id = st.session_state.get("review_sales_invoice_id")
    ids = [i.id for i in open_invoices]
    index = ids.index(default_id) if default_id in ids else 0
    chosen_id = st.selectbox(
        "Open an invoice",
        ids, index=index,
        format_func=lambda i: f"#{i} · " + next(
            f"{x.customer_name or 'Unknown'} / {x.invoice_number or 'no number'} "
            f"({x.status})" for x in open_invoices if x.id == i
        ),
        key="sales_review_select",
    )
    st.session_state.review_sales_invoice_id = chosen_id
    invoice = next(i for i in open_invoices if i.id == chosen_id)
    _render_invoice(db, company, user, invoice)


def _render_invoice(db, company, user, invoice: SalesInvoice) -> None:
    items = (
        db.query(SalesInvoiceItem)
        .filter(SalesInvoiceItem.invoice_id == invoice.id)
        .order_by(SalesInvoiceItem.id).all()
    )
    report = revalidate_sales(db, company, invoice)

    if invoice.status == InvoiceStatus.DUPLICATE.value:
        original = (
            db.query(SalesInvoice).filter(SalesInvoice.id == invoice.duplicate_of_id).one_or_none()
            if invoice.duplicate_of_id else None
        )
        st.error(
            "**Possible duplicate invoice detected.** "
            + (
                f"It looks like bill #{original.id} — {original.customer_name} / "
                f"{original.invoice_number} ({rupees(original.grand_total)})."
                if original else ""
            )
        )
        cols = st.columns(3)
        if cols[0].button("It is a duplicate — cancel this one", type="primary", key=f"sales_dupe_cancel_{invoice.id}"):
            cancel_sales_invoice(db, company, user, invoice, "Confirmed duplicate by user")
            st.rerun()
        if cols[1].button("Not a duplicate — continue review", key=f"sales_dupe_continue_{invoice.id}"):
            invoice.status = InvoiceStatus.REVIEW_REQUIRED.value
            db.commit()
            audit_service.record(
                db, company.id, "SALES_DUPLICATE_OVERRIDDEN",
                "User confirmed this is a distinct bill", user=user, sales_invoice_id=invoice.id,
            )
            st.rerun()
        if original and cols[2].button("Open the original", key=f"sales_dupe_open_{invoice.id}"):
            st.session_state.review_sales_invoice_id = original.id
            st.rerun()
        st.divider()

    st.markdown(
        f"### #{invoice.id} · {invoice.customer_name or 'Unknown'} "
        f"&nbsp;{status_badge(invoice.status)}",
        unsafe_allow_html=True,
    )
    top_left, top_right = st.columns([3, 2])

    with top_left:
        with st.expander("Invoice header", expanded=True):
            col1, col2, col3 = st.columns(3)
            customer_name = col1.text_input("Customer", invoice.customer_name or "", key=f"sh_cust_{invoice.id}")
            invoice_number = col2.text_input("Invoice no.", invoice.invoice_number or "", key=f"sh_num_{invoice.id}")
            invoice_date = col3.text_input("Date (YYYY-MM-DD)", invoice.invoice_date or "", key=f"sh_date_{invoice.id}")
            col4, col5 = st.columns(2)
            customer_gstin = col4.text_input("Customer GSTIN", invoice.customer_gstin or "", key=f"sh_gstin_{invoice.id}")
            due_date = col5.text_input("Due date", invoice.due_date or "", key=f"sh_due_{invoice.id}")
            if st.button("Save header", key=f"sh_save_{invoice.id}"):
                for field, old, new in (
                    ("customer", invoice.customer_name, customer_name),
                    ("gstin", invoice.customer_gstin, customer_gstin),
                    ("invoice_number", invoice.invoice_number, invoice_number),
                    ("invoice_date", invoice.invoice_date, invoice_date),
                    ("due_date", invoice.due_date, due_date),
                ):
                    audit_service.field_change(
                        db, company.id, user, None, field, old, new, sales_invoice_id=invoice.id,
                    )
                invoice.customer_name = customer_name[:200]
                invoice.customer_gstin = customer_gstin.upper()[:20]
                invoice.invoice_number = invoice_number[:80]
                invoice.invoice_date = invoice_date
                invoice.due_date = due_date
                db.commit()
                st.success("Header saved.")
                st.rerun()

    with top_right:
        image_path = Path(invoice.source_file or "")
        if image_path.exists() and image_path.is_file():
            st.image(str(image_path), caption="Uploaded bill", width="stretch")
            folder = image_path.parent
            extra = sorted(folder.glob("page_*.jpg"))[1:]
            if extra:
                with st.expander(f"{len(extra)} more page(s)"):
                    for page in extra:
                        st.image(str(page), width="stretch")
        else:
            st.info("No stored image for this bill (Excel import or storage disabled).")
        st.markdown(
            f"**Extraction:** `{invoice.ocr_provider or '—'}`  \n"
            f"**Confidence:** {confidence_light(invoice.overall_confidence)} "
            f"{(invoice.overall_confidence or 0):.0%}  \n"
            f"**Tax scope:** {invoice.tax_scope or 'UNKNOWN'}"
        )
        ocr_row = (
            db.query(OCRResult)
            .filter(OCRResult.sales_invoice_id == invoice.id)
            .order_by(OCRResult.id.desc()).first()
        )
        if ocr_row and (ocr_row.raw_text or "").strip():
            with st.expander("Raw text the OCR engine actually read"):
                st.caption(
                    "Useful when a text-only engine (Tesseract/PaddleOCR) misses "
                    "fields — compare this against the bill to see what it read."
                )
                st.code(ocr_row.raw_text[:6000], language=None)

    st.markdown("#### Validation")
    show_issues(report)
    for note_line in (invoice.notes or "").splitlines():
        if note_line.startswith("Auto-filled:"):
            st.info(note_line, icon="ℹ️")
    if abs(report.difference) > 0.009:
        st.caption(
            f"Calculated total {rupees(report.calculated_total)} vs "
            f"printed total {rupees(report.extracted_total)} — "
            f"difference {rupees(report.difference)}. Nothing was auto-corrected."
        )

    st.markdown("#### Line items")
    st.caption("🟢 high confidence · 🟡 verify · 🔴 mandatory review")

    matcher = ProductMatcher(db, company.id)
    products = (
        db.query(Product)
        .filter(Product.company_id == company.id, Product.is_active.is_(True))
        .order_by(Product.name).all()
    )
    product_options = {
        p.id: f"{p.name} (stock {p.current_stock or 0:g} {p.unit or ''})" for p in products
    }

    for position, item in enumerate(items):
        _render_line(db, company, user, invoice, item, position, matcher, product_options, report)

    with st.expander("➕ Add a line item"):
        _add_item_form(db, company, user, invoice, product_options)

    st.markdown("#### Totals")
    cols = st.columns(6)
    cols[0].metric("Taxable", rupees(report.calculated_subtotal))
    cols[1].metric("Discount", rupees(invoice.discount_total))
    cols[2].metric("GST", rupees(report.calculated_tax))
    cols[3].metric("Cess", rupees(report.calculated_cess))
    round_off = cols[4].number_input(
        "Round off", value=float(invoice.round_off or 0.0), step=0.01, format="%.2f", key=f"sales_ro_{invoice.id}"
    )
    grand_total = cols[5].number_input(
        "Grand total", value=float(invoice.grand_total or 0.0), step=0.01, format="%.2f", key=f"sales_gt_{invoice.id}"
    )
    if st.button("Save totals", key=f"sales_save_totals_{invoice.id}"):
        audit_service.field_change(
            db, company.id, user, None, "round_off", invoice.round_off, round_off,
            sales_invoice_id=invoice.id,
        )
        audit_service.field_change(
            db, company.id, user, None, "grand_total", invoice.grand_total, grand_total,
            sales_invoice_id=invoice.id,
        )
        invoice.round_off = round_off
        invoice.grand_total = grand_total
        db.commit()
        st.rerun()

    st.divider()
    _action_bar(db, company, user, invoice, report)


def _render_line(db, company, user, invoice, item, position, matcher, product_options, report):
    tone = confidence_light(item.confidence or 0)
    try:
        warnings = json.loads(item.line_warnings or "[]")
    except (ValueError, TypeError):
        warnings = []

    header = (
        f"{tone} **{item.raw_name or '(no name)'}** — "
        f"{item.quantity or 0:g} {item.unit or ''} × {rupees(item.rate)} = "
        f"{rupees(item.total)}"
    )
    with st.expander(header, expanded=bool(warnings) or item.product_id is None):
        st.markdown(
            match_badge(item.match_status)
            + (f" &nbsp;<span class='muted'>{item.match_reason or ''} "
               f"({(item.match_score or 0):.0%})</span>" if item.match_reason else ""),
            unsafe_allow_html=True,
        )
        for warning in warnings:
            st.warning(warning)

        cols = st.columns([3, 1, 1, 1.2, 1, 1])
        name = cols[0].text_input("Product name", item.raw_name or "", key=f"sn_{item.id}")
        quantity = cols[1].number_input(
            "Qty", value=float(item.quantity or 0), min_value=0.0, step=1.0, key=f"sq_{item.id}"
        )
        unit = cols[2].text_input("Unit", item.unit or "", key=f"su_{item.id}")
        rate = cols[3].number_input(
            "Rate", value=float(item.rate or 0), min_value=0.0, step=0.5, key=f"sr_{item.id}"
        )
        discount = cols[4].number_input(
            "Discount", value=float(item.discount or 0), min_value=0.0, step=0.5, key=f"sd_{item.id}"
        )
        gst_rate = cols[5].number_input(
            "GST %", value=float(item.gst_rate or 0), min_value=0.0, max_value=40.0,
            step=0.5, key=f"sg_{item.id}",
        )

        cols2 = st.columns([1.2, 1, 1, 1, 1, 1.4])
        hsn = cols2[0].text_input("HSN/SAC", item.hsn or "", key=f"sh_{item.id}")
        cgst = cols2[1].number_input("CGST", value=float(item.cgst or 0), step=0.01, format="%.2f", key=f"scg_{item.id}")
        sgst = cols2[2].number_input("SGST", value=float(item.sgst or 0), step=0.01, format="%.2f", key=f"ssg_{item.id}")
        igst = cols2[3].number_input("IGST", value=float(item.igst or 0), step=0.01, format="%.2f", key=f"sig_{item.id}")
        cess = cols2[4].number_input("Cess", value=float(item.cess or 0), step=0.01, format="%.2f", key=f"sce_{item.id}")
        barcode = cols2[5].text_input("Barcode", item.barcode or "", key=f"sb_{item.id}")

        taxable = round(quantity * rate - discount, 2)
        line_total = round(taxable + cgst + sgst + igst + cess, 2)
        st.caption(
            f"Taxable {rupees(taxable)} · line total {rupees(line_total)} "
            f"(currently stored: {rupees(item.total)})"
        )

        st.markdown("**Inventory mapping**")
        options = [None] + list(product_options.keys())
        current_index = options.index(item.product_id) if item.product_id in options else 0
        selected = st.selectbox(
            "Linked product", options, index=current_index,
            format_func=lambda pid: "— not linked —" if pid is None else product_options[pid],
            key=f"sp_{item.id}",
        )

        if item.match_status == MatchStatus.POSSIBLE.value and item.product_id is None:
            st.caption("A close match was found but was not applied automatically. Pick it above to accept.")

        map_cols = st.columns(4)
        save_mapping = map_cols[0].checkbox(
            "Remember this mapping", value=True, key=f"srm_{item.id}",
            help="Next time this description appears on a sales bill, it maps instantly.",
        )
        if map_cols[1].button("💾 Save line", key=f"ssave_{item.id}", type="primary"):
            for field, old, new in (
                ("product name", item.raw_name, name),
                ("quantity", item.quantity, quantity),
                ("rate", item.rate, rate),
                ("discount", item.discount, discount),
                ("GST%", item.gst_rate, gst_rate),
                ("linked product", item.product_id, selected),
            ):
                audit_service.field_change(
                    db, company.id, user, None, f"line {position+1} {field}", old, new,
                    sales_invoice_id=invoice.id,
                )
            item.raw_name = name
            item.quantity = quantity
            item.unit = unit.upper()
            item.rate = rate
            item.discount = discount
            item.gst_rate = gst_rate
            item.hsn = hsn
            item.cgst, item.sgst, item.igst, item.cess = cgst, sgst, igst, cess
            item.barcode = barcode
            item.taxable_value = taxable
            item.total = line_total
            item.edited_by_user = True
            item.confidence = 1.0
            if selected:
                item.product_id = selected
                item.match_status = MatchStatus.MATCHED.value
                item.match_score = 1.0
                item.match_reason = "confirmed by user"
                if save_mapping:
                    # supplier_id is left unset here: ProductAlias.supplier_id is a
                    # strict FK into the Suppliers table, so a Customer id can't be
                    # stored there. The alias still works — it just isn't scoped to
                    # this particular customer the way a purchase alias is scoped
                    # to a supplier.
                    matcher.remember_mapping(name, selected, user.id, None, barcode)
                if barcode:
                    matcher.link_barcode(selected, barcode)
            else:
                item.product_id = None
            db.commit()
            recalculate_sales_totals(db, invoice)
            st.success("Line saved.")
            st.rerun()

        if map_cols[2].button("🔄 Re-match", key=f"srematch_{item.id}"):
            matcher.refresh()
            result = matcher.match(
                {
                    "raw_name": name, "normalized_name": name, "hsn": hsn,
                    "barcode": barcode, "unit": unit, "gst_rate": gst_rate,
                },
                None,
            )
            item.match_status = result.status
            item.match_score = result.score
            item.match_reason = (result.reason or "")[:250]
            if result.is_confident:
                item.product_id = result.product_id
            db.commit()
            st.info(
                f"{result.status} — {result.reason} ({result.score:.0%})"
                + (
                    "  \nCandidates: "
                    + ", ".join(f"{c.product_name} ({c.score:.0%})" for c in result.candidates[:3])
                    if result.candidates else ""
                )
            )
            st.rerun()

        if map_cols[3].button("🗑️ Delete line", key=f"sdel_{item.id}"):
            audit_service.record(
                db, company.id, "SALES_LINE_DELETED",
                f"Removed '{item.raw_name}' ({item.quantity} × {item.rate})",
                user=user, sales_invoice_id=invoice.id,
            )
            db.delete(item)
            db.commit()
            recalculate_sales_totals(db, invoice)
            st.rerun()

        if item.product_id is None:
            st.markdown("---")
            new_cols = st.columns([3, 1])
            new_cols[0].caption(
                "Not in your product master yet. Creating it here adds it to "
                "Products with zero opening stock."
            )
            if new_cols[1].button("➕ Create product", key=f"screate_{item.id}"):
                # rate=0 here on purpose: create_product_from_item stores its
                # "rate" argument as purchase_rate, which a sale figure must
                # never populate. The sale rate is set separately just below.
                product = matcher.create_product_from_item(
                    {
                        "normalized_name": name, "raw_name": name, "hsn": hsn,
                        "barcode": barcode, "unit": unit or "PCS",
                        "gst_rate": gst_rate, "rate": 0,
                    },
                    user.id,
                )
                product.sale_rate = rate
                item.product_id = product.id
                item.match_status = MatchStatus.MATCHED.value
                item.match_score = 1.0
                item.match_reason = "new product created by user"
                db.commit()
                matcher.remember_mapping(name, product.id, user.id, None, barcode)
                audit_service.record(
                    db, company.id, "SALES_PRODUCT_CREATED",
                    f"Created '{product.name}' from sales bill line", user=user, sales_invoice_id=invoice.id,
                )
                st.success(f"'{product.name}' added to your product master.")
                st.rerun()


def _add_item_form(db, company, user, invoice, product_options):
    with st.form(f"sales_add_item_{invoice.id}"):
        cols = st.columns([3, 1, 1, 1.2, 1, 1])
        name = cols[0].text_input("Product")
        quantity = cols[1].number_input("Qty", min_value=0.0, step=1.0)
        unit = cols[2].text_input("Unit", value="PCS")
        rate = cols[3].number_input("Rate", min_value=0.0, step=0.5)
        discount = cols[4].number_input("Discount", min_value=0.0, step=0.5)
        gst_rate = cols[5].number_input("GST %", min_value=0.0, max_value=40.0, step=0.5)
        product_id = st.selectbox(
            "Link to product",
            [None] + list(product_options.keys()),
            format_func=lambda pid: "— not linked —" if pid is None else product_options[pid],
        )
        if st.form_submit_button("Add line", type="primary"):
            if not name.strip() or quantity <= 0:
                st.error("A product name and a quantity above zero are required.")
                return
            taxable = round(quantity * rate - discount, 2)
            tax = round(taxable * gst_rate / 100, 2)
            half = round(tax / 2, 2)
            db.add(
                SalesInvoiceItem(
                    invoice_id=invoice.id,
                    company_id=company.id,
                    product_id=product_id,
                    raw_name=name[:250],
                    normalized_name=name[:250],
                    quantity=quantity,
                    unit=unit.upper()[:20],
                    rate=rate,
                    discount=discount,
                    gst_rate=gst_rate,
                    taxable_value=taxable,
                    cgst=half if invoice.tax_scope != "INTER-STATE" else 0.0,
                    sgst=half if invoice.tax_scope != "INTER-STATE" else 0.0,
                    igst=tax if invoice.tax_scope == "INTER-STATE" else 0.0,
                    total=round(taxable + tax, 2),
                    confidence=1.0,
                    match_status=(
                        MatchStatus.MATCHED.value if product_id else MatchStatus.NEW.value
                    ),
                    match_reason="added manually",
                    edited_by_user=True,
                )
            )
            db.commit()
            recalculate_sales_totals(db, invoice)
            audit_service.record(
                db, company.id, "SALES_LINE_ADDED", f"Added '{name}' manually",
                user=user, sales_invoice_id=invoice.id,
            )
            st.rerun()


def _action_bar(db, company, user, invoice, report) -> None:
    pending = unresolved_sales_items(db, invoice.id)
    st.markdown("#### Confirm and post")

    if invoice.status == InvoiceStatus.POSTED.value:
        st.success("This invoice is already posted and its stock applied.")
        return

    if invoice.status == InvoiceStatus.EXPORTED.value:
        st.info("XML was generated for this invoice. Import it in Tally, then confirm below.")
        if st.button("✅ I have imported this in Tally", key=f"confirm_import_{invoice.id}"):
            ok, message = confirm_sales_tally_import(db, company, user, invoice)
            (st.success if ok else st.error)(message)
            if ok:
                st.rerun()
        return

    if pending:
        st.warning(
            f"{len(pending)} line(s) still have no linked product. Map them "
            "in the table above before confirming."
        )

    override_reason = ""
    if report.is_blocking:
        st.error(f"{len(report.errors)} blocking issue(s). Fix them, or give a reason to override.")
        override_reason = st.text_input("Override reason", key=f"sales_ovr_{invoice.id}")

    cols = st.columns([1.4, 1.4, 1.4])
    if invoice.status != InvoiceStatus.CONFIRMED.value:
        if cols[0].button(
            "✅ CONFIRM SALE", type="primary", disabled=bool(pending), key=f"confirm_{invoice.id}"
        ):
            ok, message = confirm_sales_invoice(db, company, user, invoice, override_reason.strip())
            (st.success if ok else st.error)(message)
            if ok:
                st.rerun()
    else:
        cols[0].success("Confirmed")

    if cols[1].button("❌ CANCEL INVOICE", key=f"cancel_{invoice.id}"):
        st.session_state[f"sales_confirm_cancel_{invoice.id}"] = True

    if st.session_state.get(f"sales_confirm_cancel_{invoice.id}"):
        with st.container(border=True):
            reason = st.text_input("Reason", key=f"sales_cancel_reason_{invoice.id}")
            c1, c2 = st.columns(2)
            if c1.button("Yes, cancel it", key=f"sales_do_cancel_{invoice.id}"):
                ok, message = cancel_sales_invoice(
                    db, company, user, invoice, reason or "no reason given"
                )
                (st.success if ok else st.error)(message)
                st.session_state[f"sales_confirm_cancel_{invoice.id}"] = False
                st.rerun()
            if c2.button("Keep it", key=f"sales_keep_{invoice.id}"):
                st.session_state[f"sales_confirm_cancel_{invoice.id}"] = False
                st.rerun()

    if invoice.status == InvoiceStatus.CONFIRMED.value:
        st.divider()
        _tally_section(db, company, user, invoice)


def _tally_section(db, company, user, invoice) -> None:
    from services.sales_service import post_sales_to_tally

    mode = get_setting(db, company.id, "tally_mode", "mock")
    test_mode = get_setting(db, company.id, "tally_test_mode", str(settings.TALLY_TEST_MODE))
    is_test = str(test_mode).lower() in ("1", "true", "yes", "on")
    connector = get_connector(mode, {
        "host": get_setting(db, company.id, "tally_host", settings.TALLY_HOST),
        "port": get_setting(db, company.id, "tally_port", settings.TALLY_PORT),
        "company": company.tally_company_name or settings.TALLY_COMPANY,
        "test_mode": is_test,
    })

    st.markdown("##### Tally voucher")
    test_mode_banner(is_test, f"Connector: {connector.name}")

    payload = build_sales_voucher_payload(db, company, invoice)
    preview = st.columns(4)
    preview[0].metric("Voucher type", payload.voucher_type)
    preview[1].metric("Customer ledger", payload.supplier_ledger[:20])
    preview[2].metric("Sales ledger", payload.purchase_ledger[:20])
    preview[3].metric("Amount", rupees(payload.grand_total))

    with st.expander("Exact XML that will be sent to Tally"):
        st.code(build_voucher_xml(payload), language="xml")

    cols = st.columns(3)
    if cols[0].button("🔎 Run Tally pre-check", key=f"preflight_{invoice.id}"):
        with st.spinner("Checking company, ledgers, stock items and units…"):
            problems = connector.preflight(payload)
        if problems:
            st.error("Pre-check found problems:\n\n- " + "\n- ".join(problems))
        else:
            st.success("All referenced masters exist. Safe to post.")

    if cols[1].button("📄 Generate XML file only", key=f"genxml_{invoice.id}"):
        path = connector.export_xml(payload)
        st.success(f"Saved to `{path}`")
        st.download_button(
            "Download XML", data=Path(path).read_bytes(), file_name=Path(path).name,
            mime="application/xml", key=f"dlxml_{invoice.id}",
        )

    if cols[2].button(
        "🚀 POST TO TALLY & UPDATE STOCK", type="primary",
        disabled=not user.can_post_to_tally, key=f"post_{invoice.id}",
    ):
        with st.spinner("Posting…"):
            result, transaction = post_sales_to_tally(db, company, user, invoice, connector)
        if result.success:
            st.success(result.message)
            for warning in result.warnings:
                st.info(warning)
            if result.export_path:
                st.caption(f"XML saved to `{result.export_path}`")
            st.rerun()
        else:
            st.error(result.message)
            st.caption(
                "Nothing was written to your books and stock was not changed. "
                "Fix the issue and try again, or generate an XML file to import manually."
            )

    if not user.can_post_to_tally:
        st.caption("Your role can review and edit, but not post. Ask a Manager or Admin.")


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------
def _history_tab(db, company, user) -> None:
    invoices = (
        db.query(SalesInvoice)
        .filter(SalesInvoice.company_id == company.id)
        .order_by(SalesInvoice.created_at.desc()).all()
    )
    if not invoices:
        st.info("No sales invoices processed yet.")
        return

    users = {u.id: u.full_name for u in db.query(User).filter(User.company_id == company.id).all()}
    transactions = {
        t.invoice_id: t
        for t in db.query(SalesTransaction).filter(SalesTransaction.company_id == company.id).all()
    }

    with st.expander("Filters", expanded=False):
        col1, col2 = st.columns(2)
        statuses = col1.multiselect("Status", ALL_STATUSES, default=[], key="sales_hist_status")
        customers = sorted({i.customer_name for i in invoices if i.customer_name})
        customer_filter = col2.multiselect(
            "Customer", customers, default=[], key="sales_hist_customer"
        )
        search = st.text_input("Search invoice no. / customer", key="sales_hist_search")

    def keep(invoice: SalesInvoice) -> bool:
        if statuses and invoice.status not in statuses:
            return False
        if customer_filter and invoice.customer_name not in customer_filter:
            return False
        if search.strip():
            needle = search.strip().lower()
            haystack = f"{invoice.invoice_number or ''} {invoice.customer_name or ''}".lower()
            if needle not in haystack:
                return False
        return True

    filtered = [i for i in invoices if keep(i)]
    st.caption(f"{len(filtered)} of {len(invoices)} invoice(s) match.")

    item_counts = dict(
        db.query(SalesInvoiceItem.invoice_id, func.count(SalesInvoiceItem.id))
        .filter(SalesInvoiceItem.company_id == company.id)
        .group_by(SalesInvoiceItem.invoice_id).all()
    )
    frame = pd.DataFrame([
        {
            "ID": i.id, "Date": i.invoice_date or "", "Customer": i.customer_name or "",
            "Invoice no.": i.invoice_number or "", "Items": item_counts.get(i.id, 0),
            "Amount": i.grand_total or 0, "Status": i.status,
            "Tally": (transactions[i.id].mode if i.id in transactions else "—"),
            "User": users.get(i.created_by, ""),
            "Created at": i.created_at.strftime("%d-%m-%Y %H:%M") if i.created_at else "",
        }
        for i in filtered
    ])
    if frame.empty:
        st.info("No invoices match these filters.")
        return

    st.dataframe(frame, hide_index=True, width="stretch", height=380)
    st.download_button(
        "⬇️ Export this view (CSV)", data=frame.to_csv(index=False).encode(),
        file_name=f"sales_history_{dt.date.today()}.csv", mime="text/csv", key="sales_hist_csv",
    )

    st.divider()
    chosen = st.selectbox(
        "Open an invoice", [i.id for i in filtered],
        format_func=lambda i: f"#{i} · " + next(
            f"{x.customer_name or 'Unknown'} / {x.invoice_number or 'no number'}"
            for x in filtered if x.id == i
        ),
        key="sales_hist_open",
    )
    _history_detail(db, chosen, transactions.get(chosen), users)


def _history_detail(db, invoice_id: int, transaction, users) -> None:
    invoice = db.query(SalesInvoice).filter(SalesInvoice.id == invoice_id).one()
    items = (
        db.query(SalesInvoiceItem).filter(SalesInvoiceItem.invoice_id == invoice.id)
        .order_by(SalesInvoiceItem.id).all()
    )
    st.markdown(
        f"### #{invoice.id} · {invoice.customer_name or 'Unknown'} "
        f"&nbsp;{status_badge(invoice.status)}",
        unsafe_allow_html=True,
    )
    cols = st.columns(5)
    cols[0].metric("Invoice no.", invoice.invoice_number or "—")
    cols[1].metric("Date", invoice.invoice_date or "—")
    cols[2].metric("Taxable", rupees(invoice.subtotal))
    cols[3].metric("GST", rupees(invoice.tax_total))
    cols[4].metric("Grand total", rupees(invoice.grand_total))

    st.dataframe(pd.DataFrame([
        {
            "Product": i.raw_name, "Qty": i.quantity, "Unit": i.unit, "Rate": i.rate,
            "Discount": i.discount, "GST %": i.gst_rate, "Taxable": i.taxable_value,
            "Total": i.total, "Match": i.match_status,
        }
        for i in items
    ]), hide_index=True, width="stretch")

    if transaction:
        st.markdown("#### Tally transaction")
        cols = st.columns(4)
        cols[0].metric("Mode", transaction.mode or "—")
        cols[1].metric("Reference", (transaction.tally_voucher_id or "—")[:18])
        cols[2].metric("Stock applied", "Yes" if transaction.stock_applied else "No")
        cols[3].metric(
            "Posted",
            transaction.created_at.strftime("%d-%m-%Y %H:%M") if transaction.created_at else "—",
        )
        exported = Path(transaction.export_path) if transaction.export_path else None
        if exported and exported.exists():
            st.download_button(
                "⬇️ Download the voucher XML file", data=exported.read_bytes(),
                file_name=exported.name, mime="application/xml", key=f"sales_dl_{invoice.id}",
            )
        elif transaction.request_xml:
            st.download_button(
                "⬇️ Download the voucher XML", data=transaction.request_xml.encode("utf-8"),
                file_name=f"sales_voucher_{invoice.invoice_number or invoice.id}.xml".replace("/", "-"),
                mime="application/xml", key=f"sales_dl2_{invoice.id}",
            )
        if transaction.request_xml:
            with st.expander("Exact XML sent to Tally"):
                st.code(transaction.request_xml[:8000], language="xml")

    st.markdown("#### Audit trail")
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.company_id == invoice.company_id, AuditLog.sales_invoice_id == invoice.id)
        .order_by(AuditLog.created_at.asc()).all()
    )
    if logs:
        for entry in logs:
            st.markdown(
                f"- `{entry.created_at:%d-%m-%Y %H:%M}` **{entry.action}** — "
                f"{entry.detail or ''} <span class='muted'>({entry.user_name or 'system'})</span>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("No audit entries for this invoice.")
