"""Upload Bill: capture or upload, enhance the image, run extraction."""
from __future__ import annotations

import datetime as dt

import streamlit as st

from database.database import get_setting
from services import audit_service
from services.ai_invoice_parser import InvoiceParser
from services.invoice_validator import InvoiceValidator
from services.ocr_service import OCRError, PPStructureProvider, PROVIDER_LABELS, get_provider
from services.purchase_service import create_manual_invoice, ingest_bill
from utils.config import settings
from utils.image_tools import build_preview, read_barcodes, to_images
from utils.security import decrypt_secret, validate_upload
from utils.ui import (
    brand_header,
    confidence_light,
    friendly_error,
    require_login,
    rupees,
    session_db,
    status_badge,
)

PROVIDER_KEY_SETTING = {
    "gemini": "gemini_api_key",
    "openai": "openai_api_key",
    "claude": "anthropic_api_key",
}


def build_parser(db, company):
    """Assemble the configured OCR provider with the company's stored API key."""
    provider_name = get_setting(db, company.id, "ocr_provider", settings.OCR_PROVIDER)
    config = {}
    key_setting = PROVIDER_KEY_SETTING.get(provider_name)
    if key_setting:
        config["api_key"] = decrypt_secret(get_setting(db, company.id, key_setting, ""))
    model = get_setting(db, company.id, f"{provider_name}_model", "")
    if model:
        config["model"] = model
    if provider_name == "tesseract":
        config["tesseract_cmd"] = get_setting(db, company.id, "tesseract_cmd", "")
    if provider_name == "auto":
        # Auto/Hybrid has no engine or key of its own — it runs PP-StructureV3
        # first, then falls back to whichever AI vision engine has a key
        # configured. Use the one the user picked, else auto-detect the first
        # with a saved key, else default to Gemini (will raise a clear error
        # at use if that key is also missing).
        fallback = get_setting(db, company.id, "auto_fallback_provider", "")
        if not fallback:
            for candidate in ("gemini", "openai", "claude"):
                if get_setting(db, company.id, PROVIDER_KEY_SETTING[candidate], ""):
                    fallback = candidate
                    break
        fallback = fallback or "gemini"
        config["fallback_provider"] = fallback
        fb_key_setting = PROVIDER_KEY_SETTING.get(fallback)
        if fb_key_setting:
            config["api_key"] = decrypt_secret(get_setting(db, company.id, fb_key_setting, ""))
        fb_model = get_setting(db, company.id, f"{fallback}_model", "")
        if fb_model:
            config["model"] = fb_model
    st.session_state.ocr_provider = provider_name
    return InvoiceParser(get_provider(provider_name, config)), provider_name


def first_load_notice(provider_name: str) -> None:
    """Warn before a PP-StructureV3/Auto extraction that may block on a
    one-time, multi-minute model download with no progress shown in the
    browser — without this, a slow/first download looks identical to a
    hang. Shown only until the pipeline is actually cached for this run.
    """
    if provider_name in ("ppstructure", "auto") and PPStructureProvider._cached_pipeline is None:
        st.info(
            "First use of PP-StructureV3 in this session: it downloads its "
            "models (~1-2 GB) before reading anything, which can take several "
            "minutes depending on your internet connection — the page will "
            "look stuck on \"Processing…\" the whole time, that's normal. "
            "Progress prints to the black console/terminal window behind the "
            "app, not here. After this first download it's cached and future "
            "bills are fast.",
            icon="⏳",
        )


def render() -> None:
    user, company = require_login()
    db = session_db()

    brand_header(f"{company.name} · Upload Bill")
    parser, provider_name = build_parser(db, company)
    st.caption(
        f"Extraction engine: **{PROVIDER_LABELS.get(provider_name, provider_name)}**. "
        "Change it under Settings → AI / OCR."
    )
    first_load_notice(provider_name)
    if provider_name == "mock":
        st.warning(
            "The mock engine is active. It returns fixed sample data so you can "
            "test the workflow — it does not read your bill. Add an API key in "
            "Settings for real extraction.",
            icon="🧪",
        )

    tab_upload, tab_camera, tab_manual = st.tabs(
        ["📁 Upload files", "📷 Camera capture", "⌨️ Manual entry"]
    )

    with tab_upload:
        _upload_tab(db, company, user, parser)
    with tab_camera:
        _camera_tab(db, company, user, parser)
    with tab_manual:
        _manual_tab(db, company, user)


# --------------------------------------------------------------------------
def _image_controls(prefix: str) -> dict:
    with st.expander("Image adjustments", expanded=False):
        col1, col2, col3 = st.columns(3)
        rotation = col1.select_slider(
            "Rotate", [0, 90, 180, 270], value=0, key=f"{prefix}_rot"
        )
        contrast = col2.slider("Contrast", 0.5, 2.5, 1.0, 0.1, key=f"{prefix}_con")
        brightness = col3.slider("Brightness", 0.5, 2.0, 1.0, 0.1, key=f"{prefix}_bri")
        col4, col5 = st.columns(2)
        sharpness = col4.slider("Sharpness", 0.5, 3.0, 1.0, 0.1, key=f"{prefix}_shp")
        zoom = col5.slider(
            "Crop margin %", 0, 30, 0, 1, key=f"{prefix}_crop",
            help="Trims this percentage from every edge — useful to cut out the table or background.",
        )
        col6, col7, col8, col9 = st.columns(4)
        grayscale = col6.checkbox("Grayscale", key=f"{prefix}_gray")
        denoise = col7.checkbox("Reduce noise", key=f"{prefix}_dn")
        deskew = col8.checkbox("Auto-straighten", key=f"{prefix}_dsk")
        perspective = col9.checkbox(
            "Perspective fix", key=f"{prefix}_persp",
            help="Flattens a bill photographed at an angle.",
        )
    return {
        "rotation": rotation,
        "crop": (zoom, zoom, zoom, zoom) if zoom else None,
        "contrast": contrast,
        "brightness": brightness,
        "sharpness": sharpness,
        "grayscale": grayscale,
        "denoise": denoise,
        "deskew": deskew,
        "perspective": perspective,
    }


def _process(db, company, user, parser, filename, raw, images, show_preview=True):
    """Run one bill through the pipeline and render the outcome."""
    try:
        result = ingest_bill(
            db, company, user, filename, raw, images, parser, InvoiceValidator()
        )
    except OCRError as exc:
        audit_service.record_error(db, company.id, "ocr", str(exc), exc)
        st.error(f"**The OCR engine could not process this bill.**\n\n{exc}")
        st.caption(
            "Check your API key in Settings, or switch to Manual entry so you are "
            "not blocked."
        )
        return None
    except Exception as exc:
        audit_service.record_error(db, company.id, "ingest", str(exc), exc)
        friendly_error("Extraction", exc)
        return None

    invoice = result.invoice
    # Point the review screen at this bill straight away, not only when the
    # button below is pressed. Otherwise someone who uploads and then clicks
    # "Review Bills" in the sidebar lands on whatever they were looking at last.
    st.session_state.review_invoice_id = invoice.id
    st.markdown(
        f"**{filename}** → {status_badge(invoice.status)}", unsafe_allow_html=True
    )

    for warning in result.warnings:
        st.info(warning)

    if result.duplicate_reason:
        st.error(f"⚠️ Possible duplicate invoice detected. {result.duplicate_reason}")
        st.caption(
            "This bill was saved with status *Duplicate*. Open it in Review Bills "
            "to compare and decide."
        )

    cols = st.columns(4)
    cols[0].metric("Supplier", (invoice.supplier_name or "—")[:22])
    cols[1].metric("Invoice no.", invoice.invoice_number or "—")
    cols[2].metric("Items", len(result.parsed.items))
    cols[3].metric(
        "Total",
        rupees(invoice.grand_total),
        f"{confidence_light(invoice.overall_confidence)} "
        f"{(invoice.overall_confidence or 0):.0%}",
    )

    errors = len(result.report.errors)
    warnings = len(result.report.warnings)
    if errors:
        st.error(f"{errors} blocking issue(s) and {warnings} warning(s) need review.")
    elif warnings:
        st.warning(f"{warnings} field(s) need a quick check.")
    else:
        st.success("Arithmetic and GST checks passed. Verify and confirm.")

    if st.button("Open verification screen", key=f"open_{invoice.id}", type="primary"):
        st.session_state.review_invoice_id = invoice.id
        st.session_state.nav = "Review Bills"
        st.rerun()
    return result


def _upload_tab(db, company, user, parser):
    files = st.file_uploader(
        "Purchase bills (JPG, PNG, WEBP or PDF — multiple allowed)",
        type=["jpg", "jpeg", "png", "webp", "pdf"],
        accept_multiple_files=True,
        key="bill_uploader",
    )
    if not files:
        st.info(
            "Drop one or twenty bills here. Multi-page PDFs are treated as a single "
            "invoice; separate files are processed as separate invoices."
        )
        return

    controls = _image_controls("up")
    apply_edits = st.checkbox(
        "Apply the adjustments above before extraction", value=False, key="up_apply"
    )

    st.markdown(f"**{len(files)} file(s) ready.**")
    if not st.button("Extract all bills", type="primary", key="extract_all"):
        with st.expander("Preview first file"):
            first = files[0]
            data = first.getvalue()
            ok, message = validate_upload(first.name, data)
            if not ok:
                st.error(message)
            else:
                pages = to_images(first.name, data)
                if pages:
                    preview = build_preview(pages[0], **controls) if apply_edits else pages[0]
                    st.image(preview, caption=f"{first.name} — page 1", width=520)
                    codes = read_barcodes(preview)
                    if codes:
                        st.caption("Barcodes detected: " + ", ".join(codes))
        return

    progress = st.progress(0.0, text="Starting…")
    summary = {"ok": 0, "review": 0, "failed": 0, "duplicate": 0}

    for index, uploaded in enumerate(files, start=1):
        progress.progress(
            (index - 1) / len(files), text=f"Processing {uploaded.name} ({index}/{len(files)})"
        )
        data = uploaded.getvalue()
        ok, message = validate_upload(uploaded.name, data)
        if not ok:
            st.error(f"{uploaded.name}: {message}")
            summary["failed"] += 1
            continue
        try:
            pages = to_images(uploaded.name, data)
        except Exception as exc:
            st.error(f"{uploaded.name}: could not read the file ({exc}).")
            summary["failed"] += 1
            continue
        if not pages:
            st.error(
                f"{uploaded.name}: no pages could be rendered. For PDFs, install "
                "PyMuPDF (`pip install pymupdf`)."
            )
            summary["failed"] += 1
            continue
        if apply_edits:
            pages = [build_preview(p, **controls) for p in pages]

        with st.container(border=True):
            result = _process(db, company, user, parser, uploaded.name, data, pages)
        if result is None:
            summary["failed"] += 1
        elif result.duplicate_reason:
            summary["duplicate"] += 1
        elif result.report.errors:
            summary["review"] += 1
        else:
            summary["ok"] += 1

    progress.progress(1.0, text="Done")
    st.markdown("#### Batch summary")
    cols = st.columns(4)
    cols[0].metric("Uploaded", len(files))
    cols[1].metric("Extracted cleanly", summary["ok"])
    cols[2].metric("Need review", summary["review"] + summary["duplicate"])
    cols[3].metric("Failed", summary["failed"])


def _camera_tab(db, company, user, parser):
    st.caption("Hold the phone steady and fill the frame with the bill.")
    shot = st.camera_input("Capture bill", key="bill_camera")
    if shot is None:
        return
    data = shot.getvalue()
    controls = _image_controls("cam")
    pages = to_images("camera.jpg", data)
    if not pages:
        st.error("Could not read the captured image.")
        return
    preview = build_preview(pages[0], **controls)
    st.image(preview, caption="Preview", width=520)
    codes = read_barcodes(preview)
    if codes:
        st.caption("Barcodes detected: " + ", ".join(codes))
    if st.button("Extract this bill", type="primary", key="extract_camera"):
        filename = f"camera_{dt.datetime.now():%Y%m%d%H%M%S}.jpg"
        _process(db, company, user, parser, filename, data, [preview])


def _manual_tab(db, company, user):
    st.caption(
        "Use this when a bill is too damaged or faint for the AI. Everything you "
        "type here goes through the same validation and Tally flow."
    )
    with st.form("manual_entry"):
        col1, col2, col3 = st.columns(3)
        supplier_name = col1.text_input("Supplier name *")
        gstin = col2.text_input("Supplier GSTIN")
        invoice_number = col3.text_input("Invoice number *")
        col4, col5, col6 = st.columns(3)
        invoice_date = col4.date_input("Invoice date *", value=dt.date.today())
        grand_total = col5.number_input("Grand total (₹)", min_value=0.0, step=1.0)
        round_off = col6.number_input("Round off (₹)", value=0.0, step=0.01, format="%.2f")

        st.markdown("**Line items**")
        line_count = st.number_input("How many lines?", 1, 30, 3, key="manual_lines")
        lines = []
        for index in range(int(line_count)):
            cols = st.columns([3, 1, 1, 1.2, 1, 1, 1.2])
            name = cols[0].text_input("Product", key=f"m_name_{index}", label_visibility="collapsed" if index else "visible")
            quantity = cols[1].number_input("Qty", min_value=0.0, step=1.0, key=f"m_qty_{index}", label_visibility="collapsed" if index else "visible")
            unit = cols[2].text_input("Unit", value="PCS", key=f"m_unit_{index}", label_visibility="collapsed" if index else "visible")
            rate = cols[3].number_input("Rate", min_value=0.0, step=0.5, key=f"m_rate_{index}", label_visibility="collapsed" if index else "visible")
            discount = cols[4].number_input("Disc", min_value=0.0, step=0.5, key=f"m_disc_{index}", label_visibility="collapsed" if index else "visible")
            gst_rate = cols[5].number_input("GST%", min_value=0.0, max_value=40.0, step=1.0, key=f"m_gst_{index}", label_visibility="collapsed" if index else "visible")
            hsn = cols[6].text_input("HSN", key=f"m_hsn_{index}", label_visibility="collapsed" if index else "visible")
            if name.strip() and quantity > 0:
                taxable = round(quantity * rate - discount, 2)
                tax = round(taxable * gst_rate / 100, 2)
                lines.append(
                    {
                        "name": name, "quantity": quantity, "unit": unit, "rate": rate,
                        "discount": discount, "gst_rate": gst_rate, "hsn": hsn,
                        "cgst": round(tax / 2, 2), "sgst": round(tax / 2, 2),
                        "total": round(taxable + tax, 2),
                    }
                )
        submitted = st.form_submit_button("Create draft invoice", type="primary")

    if submitted:
        if not supplier_name.strip() or not invoice_number.strip():
            st.error("Supplier name and invoice number are required.")
            return
        if not lines:
            st.error("Add at least one line with a product name and quantity.")
            return
        invoice = create_manual_invoice(
            db, company, user,
            {
                "supplier_name": supplier_name,
                "gstin": gstin.strip().upper(),
                "invoice_number": invoice_number,
                "invoice_date": invoice_date.isoformat(),
                "grand_total": grand_total,
                "round_off": round_off,
            },
            lines,
        )
        st.success(f"Draft invoice #{invoice.id} created. Open Review Bills to verify it.")
        st.session_state.review_invoice_id = invoice.id
