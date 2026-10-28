"""
Review Bills — the human verification screen.

This is where accounting accuracy is protected. No entry leaves this page
without an explicit human confirmation.
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from database.database import get_mapping, get_setting
from database.models import (
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    MatchStatus,
    OCRResult,
    Product,
)
from services import audit_service
from services.product_matcher import ProductMatcher
from services.purchase_service import (
    build_voucher_payload,
    cancel_invoice,
    confirm_invoice,
    post_to_tally,
    recalculate_totals,
    revalidate,
    unresolved_items,
)
from services.tally_connector import build_purchase_voucher_xml, get_connector
from utils.config import settings
from utils.ui import (
    brand_header,
    confidence_light,
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


def render() -> None:
    user, company = require_login()
    db = session_db()
    brand_header(f"{company.name} · Review Bills")

    invoices = (
        db.query(Invoice)
        .filter(Invoice.company_id == company.id, Invoice.status.in_(OPEN_STATUSES))
        .order_by(Invoice.created_at.desc())
        .all()
    )
    if not invoices:
        st.success("Nothing is waiting for review. Upload a bill to get started.")
        return

    labels = {
        inv.id: (
            f"#{inv.id} · {inv.supplier_name or 'Unknown'} · "
            f"{inv.invoice_number or 'no number'} · {rupees(inv.grand_total)} · {inv.status}"
        )
        for inv in invoices
    }
    preselect = st.session_state.get("review_invoice_id")
    ids = [inv.id for inv in invoices]
    index = ids.index(preselect) if preselect in ids else 0
    chosen = st.selectbox(
        "Bill to verify", ids, index=index, format_func=lambda i: labels[i]
    )
    st.session_state.review_invoice_id = chosen

    invoice = db.query(Invoice).filter(Invoice.id == chosen).one()
    _render_invoice(db, company, user, invoice)


# --------------------------------------------------------------------------
def _render_invoice(db, company, user, invoice: Invoice) -> None:
    report = revalidate(db, company, invoice)
    items = (
        db.query(InvoiceItem)
        .filter(InvoiceItem.invoice_id == invoice.id)
        .order_by(InvoiceItem.id)
        .all()
    )

    if invoice.status == InvoiceStatus.DUPLICATE.value:
        original = (
            db.query(Invoice).filter(Invoice.id == invoice.duplicate_of_id).one_or_none()
            if invoice.duplicate_of_id
            else None
        )
        st.error(
            "**Possible duplicate invoice detected.** "
            + (
                f"It looks like bill #{original.id} — {original.supplier_name} / "
                f"{original.invoice_number} ({rupees(original.grand_total)})."
                if original
                else ""
            )
        )
        cols = st.columns(3)
        if cols[0].button("It is a duplicate — cancel this one", type="primary"):
            cancel_invoice(db, company, user, invoice, "Confirmed duplicate by user")
            st.rerun()
        if cols[1].button("Not a duplicate — continue review"):
            invoice.status = InvoiceStatus.REVIEW_REQUIRED.value
            db.commit()
            audit_service.record(
                db, company.id, "DUPLICATE_OVERRIDDEN",
                "User confirmed this is a distinct bill", user=user, invoice_id=invoice.id,
            )
            st.rerun()
        if original and cols[2].button("Open the original"):
            st.session_state.review_invoice_id = original.id
            st.rerun()
        st.divider()

    # ---- header ----------------------------------------------------
    st.markdown(
        f"### Bill #{invoice.id} &nbsp; {status_badge(invoice.status)}",
        unsafe_allow_html=True,
    )
    top_left, top_right = st.columns([3, 2])

    with top_left:
        with st.expander("Invoice header", expanded=True):
            col1, col2 = st.columns(2)
            supplier_name = col1.text_input("Supplier", invoice.supplier_name or "")
            gstin = col2.text_input("Supplier GSTIN", invoice.supplier_gstin or "")
            col3, col4, col5 = st.columns(3)
            invoice_number = col3.text_input("Invoice no.", invoice.invoice_number or "")
            invoice_date = col4.text_input(
                "Invoice date (YYYY-MM-DD)", invoice.invoice_date or ""
            )
            due_date = col5.text_input("Due date", invoice.due_date or "")
            if st.button("Save header"):
                for field, old, new in (
                    ("supplier", invoice.supplier_name, supplier_name),
                    ("gstin", invoice.supplier_gstin, gstin),
                    ("invoice_number", invoice.invoice_number, invoice_number),
                    ("invoice_date", invoice.invoice_date, invoice_date),
                    ("due_date", invoice.due_date, due_date),
                ):
                    audit_service.field_change(
                        db, company.id, user, invoice.id, field, old, new
                    )
                invoice.supplier_name = supplier_name
                invoice.supplier_gstin = gstin.upper()
                invoice.invoice_number = invoice_number
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
            st.info("No stored image for this bill (manual entry or storage disabled).")
        st.markdown(
            f"**Extraction:** `{invoice.ocr_provider or '—'}`  \n"
            f"**Confidence:** {confidence_light(invoice.overall_confidence)} "
            f"{(invoice.overall_confidence or 0):.0%}  \n"
            f"**Tax scope:** {invoice.tax_scope or 'UNKNOWN'}"
        )
        ocr_row = (
            db.query(OCRResult)
            .filter(OCRResult.invoice_id == invoice.id)
            .order_by(OCRResult.id.desc()).first()
        )
        if ocr_row and (ocr_row.raw_text or "").strip():
            with st.expander("Raw text the OCR engine actually read"):
                st.caption(
                    "Useful when a text-only engine (Tesseract/PaddleOCR) misses "
                    "fields — compare this against the bill to see what it read."
                )
                st.code(ocr_row.raw_text[:6000], language=None)

    # ---- validation --------------------------------------------------
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

    # ---- line items --------------------------------------------------
    st.markdown("#### Line items")
    st.caption("🟢 high confidence · 🟡 verify · 🔴 mandatory review")

    matcher = ProductMatcher(db, company.id)
    products = (
        db.query(Product)
        .filter(Product.company_id == company.id, Product.is_active.is_(True))
        .order_by(Product.name)
        .all()
    )
    product_options = {p.id: f"{p.name} (stock {p.current_stock or 0:g} {p.unit or ''})"
                       for p in products}

    for position, item in enumerate(items):
        _render_line(
            db, company, user, invoice, item, position, matcher, product_options, report
        )

    with st.expander("➕ Add a line item"):
        _add_item_form(db, company, user, invoice, product_options)

    # ---- totals ------------------------------------------------------
    st.markdown("#### Totals")
    cols = st.columns(6)
    cols[0].metric("Taxable", rupees(report.calculated_subtotal))
    cols[1].metric("Discount", rupees(invoice.discount_total))
    cols[2].metric("GST", rupees(report.calculated_tax))
    cols[3].metric("Cess", rupees(report.calculated_cess))
    round_off = cols[4].number_input(
        "Round off", value=float(invoice.round_off or 0.0), step=0.01, format="%.2f"
    )
    grand_total = cols[5].number_input(
        "Grand total", value=float(invoice.grand_total or 0.0), step=0.01, format="%.2f"
    )
    if st.button("Save totals"):
        audit_service.field_change(
            db, company.id, user, invoice.id, "round_off", invoice.round_off, round_off
        )
        audit_service.field_change(
            db, company.id, user, invoice.id, "grand_total", invoice.grand_total, grand_total
        )
        invoice.round_off = round_off
        invoice.grand_total = grand_total
        db.commit()
        st.rerun()

    st.divider()
    _action_bar(db, company, user, invoice, report)


# --------------------------------------------------------------------------
def _render_line(db, company, user, invoice, item, position, matcher, product_options, report):
    tone = confidence_light(item.confidence or 0)
    warnings = []
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
        name = cols[0].text_input("Product name", item.raw_name or "", key=f"n_{item.id}")
        quantity = cols[1].number_input(
            "Qty", value=float(item.quantity or 0), min_value=0.0, step=1.0, key=f"q_{item.id}"
        )
        unit = cols[2].text_input("Unit", item.unit or "", key=f"u_{item.id}")
        rate = cols[3].number_input(
            "Rate", value=float(item.rate or 0), min_value=0.0, step=0.5, key=f"r_{item.id}"
        )
        discount = cols[4].number_input(
            "Discount", value=float(item.discount or 0), min_value=0.0, step=0.5, key=f"d_{item.id}"
        )
        gst_rate = cols[5].number_input(
            "GST %", value=float(item.gst_rate or 0), min_value=0.0, max_value=40.0,
            step=0.5, key=f"g_{item.id}",
        )

        cols2 = st.columns([1.2, 1, 1, 1, 1, 1.4])
        hsn = cols2[0].text_input("HSN/SAC", item.hsn or "", key=f"h_{item.id}")
        cgst = cols2[1].number_input("CGST", value=float(item.cgst or 0), step=0.01, format="%.2f", key=f"cg_{item.id}")
        sgst = cols2[2].number_input("SGST", value=float(item.sgst or 0), step=0.01, format="%.2f", key=f"sg_{item.id}")
        igst = cols2[3].number_input("IGST", value=float(item.igst or 0), step=0.01, format="%.2f", key=f"ig_{item.id}")
        cess = cols2[4].number_input("Cess", value=float(item.cess or 0), step=0.01, format="%.2f", key=f"ce_{item.id}")
        barcode = cols2[5].text_input("Barcode", item.barcode or "", key=f"b_{item.id}")

        taxable = round(quantity * rate - discount, 2)
        line_total = round(taxable + cgst + sgst + igst + cess, 2)
        st.caption(
            f"Taxable {rupees(taxable)} · line total {rupees(line_total)} "
            f"(currently stored: {rupees(item.total)})"
        )

        # -- product mapping ------------------------------------------
        st.markdown("**Inventory mapping**")
        options = [None] + list(product_options.keys())
        current_index = options.index(item.product_id) if item.product_id in options else 0
        selected = st.selectbox(
            "Linked product",
            options,
            index=current_index,
            format_func=lambda pid: "— not linked —" if pid is None else product_options[pid],
            key=f"p_{item.id}",
        )

        if item.match_status == MatchStatus.POSSIBLE.value and item.product_id is None:
            st.caption("A close match was found but was not applied automatically. Pick it above to accept.")

        map_cols = st.columns(4)
        save_mapping = map_cols[0].checkbox(
            "Remember this mapping", value=True, key=f"rm_{item.id}",
            help="Next time this supplier prints the same name, it maps instantly.",
        )
        if map_cols[1].button("💾 Save line", key=f"save_{item.id}", type="primary"):
            for field, old, new in (
                ("product name", item.raw_name, name),
                ("quantity", item.quantity, quantity),
                ("rate", item.rate, rate),
                ("discount", item.discount, discount),
                ("GST%", item.gst_rate, gst_rate),
                ("linked product", item.product_id, selected),
            ):
                audit_service.field_change(
                    db, company.id, user, invoice.id, f"line {position+1} {field}", old, new
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
            # Saving is the human verification step. Record it so the line is
            # no longer blocked on the AI's own confidence score — the original
            # score is kept in the OCR result and the audit trail.
            item.edited_by_user = True
            item.confidence = 1.0
            if selected:
                item.product_id = selected
                item.match_status = MatchStatus.MATCHED.value
                item.match_score = 1.0
                item.match_reason = "confirmed by user"
                if save_mapping:
                    matcher.remember_mapping(
                        name, selected, user.id, invoice.supplier_id, barcode
                    )
                if barcode:
                    matcher.link_barcode(selected, barcode)
            else:
                item.product_id = None
            db.commit()
            recalculate_totals(db, invoice)
            st.success("Line saved.")
            st.rerun()

        if map_cols[2].button("🔄 Re-match", key=f"rematch_{item.id}"):
            matcher.refresh()
            result = matcher.match(
                {
                    "raw_name": name, "normalized_name": name, "hsn": hsn,
                    "barcode": barcode, "unit": unit, "gst_rate": gst_rate,
                },
                invoice.supplier_id,
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
                    if result.candidates
                    else ""
                )
            )
            st.rerun()

        if map_cols[3].button("🗑️ Delete line", key=f"del_{item.id}"):
            audit_service.record(
                db, company.id, "LINE_DELETED",
                f"Removed '{item.raw_name}' ({item.quantity} × {item.rate})",
                user=user, invoice_id=invoice.id,
            )
            db.delete(item)
            db.commit()
            recalculate_totals(db, invoice)
            st.rerun()

        if item.product_id is None:
            st.markdown("---")
            new_cols = st.columns([3, 1])
            new_cols[0].caption(
                "Not in your product master yet. Creating it here adds it to "
                "Products with zero opening stock."
            )
            if new_cols[1].button("➕ Create product", key=f"create_{item.id}"):
                product = matcher.create_product_from_item(
                    {
                        "normalized_name": name, "raw_name": name, "hsn": hsn,
                        "barcode": barcode, "unit": unit or "PCS",
                        "gst_rate": gst_rate, "rate": rate,
                    },
                    user.id,
                )
                item.product_id = product.id
                item.match_status = MatchStatus.MATCHED.value
                item.match_score = 1.0
                item.match_reason = "new product created by user"
                db.commit()
                matcher.remember_mapping(name, product.id, user.id, invoice.supplier_id, barcode)
                audit_service.record(
                    db, company.id, "PRODUCT_CREATED",
                    f"Created '{product.name}' from bill line", user=user, invoice_id=invoice.id,
                )
                st.success(f"'{product.name}' added to your product master.")
                st.rerun()


def _add_item_form(db, company, user, invoice, product_options):
    with st.form(f"add_item_{invoice.id}"):
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
                InvoiceItem(
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
            recalculate_totals(db, invoice)
            audit_service.record(
                db, company.id, "LINE_ADDED", f"Added '{name}' manually",
                user=user, invoice_id=invoice.id,
            )
            st.rerun()


# --------------------------------------------------------------------------

def _awaiting_import_panel(db, company, user, invoice) -> None:
    """Shown when the XML exists but nobody has confirmed the Tally import.

    Generating a file is not the same as Tally accepting it, so stock waits
    here rather than moving on an assumption.
    """
    from pathlib import Path

    from database.models import PurchaseTransaction
    from services.purchase_service import confirm_tally_import, idempotency_key

    st.warning(
        "**XML generated — not yet in Tally.** Stock has NOT been updated for "
        "this bill. Import the file in Tally, check the voucher, then confirm "
        "below.",
        icon="📥",
    )

    transaction = (
        db.query(PurchaseTransaction)
        .filter(
            PurchaseTransaction.company_id == company.id,
            PurchaseTransaction.idempotency_key == idempotency_key(company.id, invoice),
        )
        .one_or_none()
    )

    st.markdown(
        "1. Open Tally with **{}** loaded\n"
        "2. **Gateway of Tally → Import → Vouchers**\n"
        "3. Select the XML file below\n"
        "4. Check the voucher in **Day Book** against the paper bill\n"
        "5. Come back and press the green button".format(
            company.tally_company_name or company.name
        )
    )

    if transaction is not None:
        if transaction.export_path and Path(transaction.export_path).exists():
            st.download_button(
                "⬇️ Download the XML again",
                data=Path(transaction.export_path).read_bytes(),
                file_name=Path(transaction.export_path).name,
                mime="application/xml",
                key=f"redl_{invoice.id}",
            )
        elif transaction.request_xml:
            st.download_button(
                "⬇️ Download the XML again",
                data=transaction.request_xml.encode("utf-8"),
                file_name=f"voucher_{invoice.invoice_number or invoice.id}.xml".replace(
                    "/", "-"
                ),
                mime="application/xml",
                key=f"redl_{invoice.id}",
            )

    st.divider()
    cols = st.columns([2, 2])
    if cols[0].button(
        "✅ I have imported this in Tally",
        key=f"confirm_import_{invoice.id}",
        type="primary",
        width="stretch",
        help="Only press this after you have seen the voucher in Tally's Day Book.",
    ):
        ok, message = confirm_tally_import(db, company, user, invoice)
        if ok:
            st.success(message)
            st.rerun()
        else:
            st.error(message)

    if cols[1].button(
        "↩️ Import failed — send it back",
        key=f"unexport_{invoice.id}",
        width="stretch",
        help="Returns the bill to Confirmed so you can fix it and export again.",
    ):
        invoice.status = InvoiceStatus.CONFIRMED.value
        db.commit()
        audit_service.record(
            db, company.id, "TALLY_EXPORT_REVERTED",
            "User reported the Tally import did not succeed; bill returned to Confirmed",
            user=user, invoice_id=invoice.id,
        )
        st.info("Back to Confirmed. Fix the problem in Tally, then export again.")
        st.rerun()


def _action_bar(db, company, user, invoice, report):
    pending = unresolved_items(db, invoice.id)
    st.markdown("#### Confirm and post")

    if invoice.status == InvoiceStatus.POSTED.value:
        st.success("This bill is already posted and its stock applied.")
        return

    if invoice.status == InvoiceStatus.EXPORTED.value:
        _awaiting_import_panel(db, company, user, invoice)
        return

    if pending:
        st.warning(
            f"{len(pending)} line(s) still have no linked product. Map or create "
            "them before confirming — stock cannot move without a product."
        )

    override_reason = ""
    if report.is_blocking:
        st.error(
            f"{len(report.errors)} blocking issue(s). Fix them, or record a written "
            "reason to override — the reason is stored in the audit trail."
        )
        override_reason = st.text_input(
            "Override reason (required to confirm with unresolved errors)",
            key=f"ovr_{invoice.id}",
        )

    cols = st.columns([1.4, 1.4, 1.4, 1.4])

    if invoice.status != InvoiceStatus.CONFIRMED.value:
        if cols[0].button("✅ CONFIRM PURCHASE", type="primary", disabled=bool(pending)):
            ok, message = confirm_invoice(
                db, company, user, invoice, override_reason.strip()
            )
            (st.success if ok else st.error)(message)
            if ok:
                st.rerun()
    else:
        cols[0].success("Confirmed")

    if cols[1].button("🔄 Re-validate"):
        st.rerun()

    if cols[2].button("❌ CANCEL BILL"):
        st.session_state[f"confirm_cancel_{invoice.id}"] = True

    if st.session_state.get(f"confirm_cancel_{invoice.id}"):
        with st.container(border=True):
            st.warning("Cancel this bill? It will not be posted and no stock will move.")
            reason = st.text_input("Reason", key=f"cancel_reason_{invoice.id}")
            confirm_cols = st.columns(2)
            if confirm_cols[0].button("Yes, cancel it", key=f"do_cancel_{invoice.id}"):
                ok, message = cancel_invoice(db, company, user, invoice, reason or "no reason given")
                (st.success if ok else st.error)(message)
                st.session_state[f"confirm_cancel_{invoice.id}"] = False
                st.rerun()
            if confirm_cols[1].button("Keep it", key=f"keep_{invoice.id}"):
                st.session_state[f"confirm_cancel_{invoice.id}"] = False
                st.rerun()

    if invoice.status == InvoiceStatus.CONFIRMED.value:
        st.divider()
        _tally_section(db, company, user, invoice)


def _tally_section(db, company, user, invoice):
    mode = get_setting(db, company.id, "tally_mode", "mock")
    test_mode = get_setting(db, company.id, "tally_test_mode", str(settings.TALLY_TEST_MODE))
    is_test = str(test_mode).lower() in ("1", "true", "yes", "on")
    connector = get_connector(
        mode,
        {
            "host": get_setting(db, company.id, "tally_host", settings.TALLY_HOST),
            "port": get_setting(db, company.id, "tally_port", settings.TALLY_PORT),
            "company": company.tally_company_name or settings.TALLY_COMPANY,
            "test_mode": is_test,
        },
    )

    st.markdown("##### Tally voucher")
    test_mode_banner(is_test, f"Connector: {connector.name}")

    payload = build_voucher_payload(db, company, invoice)
    preview = st.columns(4)
    preview[0].metric("Voucher type", payload.voucher_type)
    preview[1].metric("Supplier ledger", payload.supplier_ledger[:20])
    preview[2].metric("Purchase ledger", payload.purchase_ledger[:20])
    preview[3].metric("Amount", rupees(payload.grand_total))

    with st.expander("Exact XML that will be sent to Tally"):
        st.code(build_purchase_voucher_xml(payload), language="xml")

    cols = st.columns(3)
    if cols[0].button("🔎 Run Tally pre-check"):
        with st.spinner("Checking company, ledgers, stock items and units…"):
            problems = connector.preflight(payload)
        if problems:
            st.error("Pre-check found problems:\n\n- " + "\n- ".join(problems))
        else:
            st.success("All referenced masters exist. Safe to post.")

    if cols[1].button("📄 Generate XML file only"):
        path = connector.export_xml(payload)
        st.success(f"Saved to `{path}`")
        st.download_button(
            "Download XML",
            data=Path(path).read_bytes(),
            file_name=Path(path).name,
            mime="application/xml",
        )

    if cols[2].button(
        "🚀 POST TO TALLY & UPDATE STOCK",
        type="primary",
        disabled=not user.can_post_to_tally,
    ):
        with st.spinner("Posting…"):
            result, transaction = post_to_tally(db, company, user, invoice, connector)
        if result.success:
            st.success(result.message)
            for warning in result.warnings:
                st.info(warning)
            if result.export_path:
                st.caption(f"XML saved to `{result.export_path}`")
            if result.voucher_id:
                st.caption(f"Reference: `{result.voucher_id}`")
            st.rerun()
        else:
            st.error(result.message)
            st.caption(
                "Nothing was written to your books and stock was not changed. "
                "Fix the issue and try again, or generate an XML file to import manually."
            )

    if not user.can_post_to_tally:
        st.caption("Your role can review and edit, but not post. Ask a Manager or Admin.")
