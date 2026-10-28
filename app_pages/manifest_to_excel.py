"""Manifest to Excel: turn photos or PDFs of a transporter's handwritten
manifests/bilties into one reviewable, downloadable Excel workbook.

Upload one manifest or many. They are read one after another, each shows up
in its own editable table, and a single Build Excel produces ONE workbook:

  * one bill  -> a single "Manifest" sheet (exactly as before);
  * many bills -> "All Manifests" (every consignment in one filterable table),
    "Summary" (one line per bill) and one sheet per bill.

This is independent of the Upload Bill / stock pipeline: a manifest is a
transporter's own consignment list (who is carrying what, to where, for how
much freight), not a purchase/sales bill, so nothing here touches stock,
Tally, or GST. The output is the Excel sheet itself - see
services.excel_export.build_multi_manifest_workbook.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import streamlit as st

from database.database import get_setting
from services.excel_export import build_multi_manifest_workbook
from services.manifest_parser import (
    LOW_CONFIDENCE,
    MANIFEST_PROVIDER_REQUIRED,
    ManifestParser,
    ParsedManifest,
    ParseError,
    rows_from_records,
)
from services.ocr_service import PROVIDER_LABELS, get_provider
from utils.config import settings
from utils.image_tools import to_images
from utils.security import decrypt_secret, validate_upload
from utils.ui import brand_header, confidence_light, require_login, session_db

PROVIDER_KEY_SETTING = {
    "gemini": "gemini_api_key",
    "openai": "openai_api_key",
    "claude": "anthropic_api_key",
}

# Session-state slots. Results are keyed by the uploaded file's own id, so two
# files with the same name (or the same file twice) never overwrite each other.
RESULTS = "manifest_results"      # key -> ParsedManifest
ERRORS = "manifest_errors"        # key -> message (file that could not be read)
THUMBS = "manifest_thumbs"        # key -> small PIL preview of page 1
BUILT = "manifest_built"          # the last workbook built, ready to download


def _build_provider(db, company):
    """Same assembly as Upload Bill's build_parser (app_pages/upload_bill.py),
    kept as its own small copy here rather than a shared import: the two
    pages are free to diverge (eg. a dedicated manifest engine later) and
    neither should have to think about the other when changing its own
    provider wiring."""
    provider_name = get_setting(db, company.id, "ocr_provider", settings.OCR_PROVIDER)
    config = {}
    key_setting = PROVIDER_KEY_SETTING.get(provider_name)
    if key_setting:
        config["api_key"] = decrypt_secret(get_setting(db, company.id, key_setting, ""))
    model = get_setting(db, company.id, f"{provider_name}_model", "")
    if model:
        config["model"] = model
    if provider_name == "auto":
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
            config[f"{fallback}_api_key"] = decrypt_secret(
                get_setting(db, company.id, fb_key_setting, "")
            )
    return get_provider(provider_name, config), provider_name


def _rows_to_dataframe(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "S.No": r.s_no, "C.No.": r.c_no, "Station": r.station,
                "Consignee": r.consignee, "Weight": r.weight,
                "No. of Pkg": r.no_of_pkg, "To Pay": r.to_pay,
                "Paid": r.paid, "Amount": r.amount,
                "Confidence": round((r.confidence or 0) * 100),
            }
            for r in rows
        ]
    )


def _file_key(uploaded) -> str:
    return str(getattr(uploaded, "file_id", None) or f"{uploaded.name}_{uploaded.size}")


def _extract(entry: dict, provider) -> None:
    """Read one uploaded file. Never raises: a bad file is recorded in ERRORS
    so the rest of the batch still gets processed."""
    key = entry["key"]
    st.session_state[ERRORS].pop(key, None)
    try:
        pages = to_images(entry["name"], entry["data"])
        if not pages:
            raise ParseError(
                "No pages could be rendered. For PDFs, install PyMuPDF "
                "(`pip install pymupdf`)."
            )
        parsed = ManifestParser(provider).parse_images(pages)
    except ParseError as exc:
        st.session_state[ERRORS][key] = str(exc)
        return
    except Exception as exc:  # unreadable image, corrupt PDF, ...
        st.session_state[ERRORS][key] = f"Could not read this file: {exc}"
        return
    thumb = pages[0].copy()
    thumb.thumbnail((700, 700))
    st.session_state[THUMBS][key] = thumb
    st.session_state[RESULTS][key] = parsed


def render() -> None:
    user, company = require_login()
    db = session_db()

    for slot in (RESULTS, ERRORS, THUMBS):
        st.session_state.setdefault(slot, {})

    brand_header(f"{company.name} · Manifest to Excel")
    st.caption(
        "Upload one manifest or several — photos or PDFs. They are read one "
        "after another and you get ONE Excel file: a single bill gives one "
        "sheet; many bills give a combined sheet, a summary and a sheet per "
        "bill. This does not touch stock, Tally, or GST."
    )

    provider, provider_name = _build_provider(db, company)
    if not provider.supports_structured:
        st.warning(MANIFEST_PROVIDER_REQUIRED, icon="⚠️")
        st.caption(f"Current engine: **{PROVIDER_LABELS.get(provider_name, provider_name)}**")
        return

    uploaded_files = st.file_uploader(
        "Manifests / bilties (JPG, PNG, WEBP or PDF) — add as many as you need",
        type=["jpg", "jpeg", "png", "webp", "pdf"],
        accept_multiple_files=True,
        key="manifest_uploader",
    )
    if not uploaded_files:
        st.info(
            "Drop one or more manifests here. A multi-page PDF is treated as "
            "ONE manifest; to combine several bills, add each as its own file."
        )
        return

    # ---- validate what was uploaded ---------------------------------------
    entries = []
    for uploaded in uploaded_files:
        data = uploaded.getvalue()
        ok, message = validate_upload(uploaded.name, data)
        if not ok:
            st.error(f"**{uploaded.name}** — {message}")
            continue
        entries.append({"key": _file_key(uploaded), "name": uploaded.name, "data": data})
    if not entries:
        return

    pending = [e for e in entries if e["key"] not in st.session_state[RESULTS]]
    engine_label = PROVIDER_LABELS.get(provider_name, provider_name)

    # ---- extract, one after another ---------------------------------------
    if pending:
        label = (
            "Extract manifest" if len(entries) == 1
            else f"Extract {len(pending)} manifest{'s' if len(pending) != 1 else ''}"
        )
        if st.button(label, type="primary", key="manifest_extract"):
            progress = st.progress(0.0)
            status = st.empty()
            for index, entry in enumerate(pending, start=1):
                status.info(
                    f"Reading {index} of {len(pending)} with {engine_label}: "
                    f"**{entry['name']}**"
                )
                _extract(entry, provider)
                progress.progress(index / len(pending))
            status.empty()
            progress.empty()
            st.session_state.pop(BUILT, None)
            pending = [e for e in entries if e["key"] not in st.session_state[RESULTS]]

    failed = [e for e in entries if e["key"] in st.session_state[ERRORS]
              and e["key"] not in st.session_state[RESULTS]]
    for entry in failed:
        st.error(
            f"**{entry['name']}** could not be read.\n\n"
            f"{st.session_state[ERRORS][entry['key']]}\n\n"
            "Press Extract again to retry it, or remove it from the list above."
        )

    done = [e for e in entries if e["key"] in st.session_state[RESULTS]]
    if not done:
        return
    if pending and not failed:
        st.caption(f"{len(pending)} file(s) not read yet.")

    # ---- review each bill --------------------------------------------------
    total_rows = sum(len(st.session_state[RESULTS][e["key"]].rows) for e in done)
    total_check = sum(
        1 for e in done for r in st.session_state[RESULTS][e["key"]].rows
        if r.confidence and r.confidence < LOW_CONFIDENCE
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Bills read", len(done))
    m2.metric("Consignments", total_rows)
    m3.metric("Rows to check", total_check)
    m4.metric("Engine", engine_label)

    st.caption(
        "Rows below 80% confidence are the ones most likely to need a fix — "
        "edit any cell before building. Edits are not saved anywhere except "
        "the Excel file you download."
    )

    to_build = []   # (key, ParsedManifest with edits applied)
    for number, entry in enumerate(done, start=1):
        key = entry["key"]
        parsed: ParsedManifest = st.session_state[RESULTS][key]
        low = sum(1 for r in parsed.rows if r.confidence and r.confidence < LOW_CONFIDENCE)
        shown_no = (st.session_state.get(f"mn_{key}", parsed.manifest_no) or "").strip()
        title = (
            f"Bill {number} · Manifest {shown_no or '—'} · "
            f"{len(parsed.rows)} rows"
            + (f" · {low} to check" if low else "")
            + f" · {entry['name']}"
        )
        with st.expander(title, expanded=len(done) == 1):
            include = st.checkbox("Include this bill in the Excel", value=True, key=f"mi_{key}")

            for warning in parsed.warnings:
                st.caption(f"⚠️ {warning}")

            left, right = st.columns([1, 3])
            with left:
                thumb = st.session_state[THUMBS].get(key)
                if thumb is not None:
                    st.image(thumb, caption=f"{entry['name']} — page 1", width=240)
                st.caption(
                    f"Overall confidence: {confidence_light(parsed.overall_confidence)} "
                    f"{parsed.overall_confidence * 100:.0f}%"
                )
            with right:
                h1, h2, h3, h4 = st.columns(4)
                manifest_no = h1.text_input("Manifest No.", value=parsed.manifest_no, key=f"mn_{key}")
                manifest_date = h2.text_input("Date", value=parsed.manifest_date, key=f"md_{key}")
                from_station = h3.text_input("From", value=parsed.from_station, key=f"mf_{key}")
                to_station = h4.text_input("To", value=parsed.to_station, key=f"mt_{key}")

            if not parsed.rows:
                st.warning("No consignment rows were recognised in this image.")
                continue

            edited = st.data_editor(
                _rows_to_dataframe(parsed.rows),
                use_container_width=True,
                hide_index=True,
                num_rows="dynamic",
                key=f"manifest_editor_{key}",
                column_config={
                    "Confidence": st.column_config.NumberColumn("Confidence %", disabled=True),
                },
            )
            if include:
                to_build.append((
                    key,
                    ParsedManifest(
                        manifest_no=manifest_no.strip(), manifest_date=manifest_date.strip(),
                        from_station=from_station.strip(), to_station=to_station.strip(),
                        rows=rows_from_records(edited.to_dict("records")),
                        overall_confidence=parsed.overall_confidence,
                        provider=parsed.provider, model=parsed.model,
                    ),
                ))

    # ---- build ONE workbook ------------------------------------------------
    st.divider()
    if not to_build:
        st.info("Nothing selected — tick \"Include this bill\" on at least one bill.")
        return

    count = len(to_build)
    build_label = (
        "Build Excel" if count == 1
        else f"Build Excel — {count} bills in 1 file"
    )
    if st.button(build_label, type="primary", key="manifest_build"):
        path = build_multi_manifest_workbook([m for _, m in to_build])
        st.session_state[BUILT] = {
            "name": Path(path).name,
            "data": Path(path).read_bytes(),
            "keys": tuple(k for k, _ in to_build),
            "bills": count,
            "rows": sum(len(m.rows) for _, m in to_build),
            "at": dt.datetime.now().strftime("%H:%M:%S"),
        }

    built = st.session_state.get(BUILT)
    if built and built["keys"] == tuple(k for k, _ in to_build):
        st.success(
            f"Built at {built['at']} — {built['bills']} bill(s), {built['rows']} "
            f"consignments in one file. Changed something afterwards? "
            f"Press Build again."
        )
        st.download_button(
            "⬇️ Download Excel",
            data=built["data"],
            file_name=built["name"],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="manifest_download",
        )
    elif built:
        st.caption("The selection changed since the last build — press Build again.")
