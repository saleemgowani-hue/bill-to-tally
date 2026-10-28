"""Import Excel: bring purchase data in from a spreadsheet instead of a bill photo.

Runs through the exact same pipeline as Upload Bill (validation, product
matching, duplicate check) — see services.purchase_service.ingest_excel and
services.excel_import for the extraction step that is unique to this page.
"""
from __future__ import annotations

import datetime as dt

import streamlit as st

from services import audit_service
from services.excel_import import ExcelImportError, sample_template_bytes
from services.invoice_validator import InvoiceValidator
from services.purchase_service import ingest_excel
from utils.security import safe_filename
from utils.ui import (
    brand_header,
    confidence_light,
    friendly_error,
    require_login,
    rupees,
    session_db,
    status_badge,
)


def render() -> None:
    user, company = require_login()
    db = session_db()

    brand_header(f"{company.name} · Import Excel")
    st.caption(
        "Bring purchase data in from a spreadsheet instead of a bill photo. "
        "Every bill still lands in **Review Bills** for a check before you "
        "Confirm and post it to Tally — nothing is sent to Tally from here."
    )

    with st.expander("Expected columns", expanded=False):
        st.markdown(
            "- **Required:** Item Name\n"
            "- **Recommended:** Supplier Name, Invoice Number, Invoice Date, "
            "Qty, Rate\n"
            "- **Optional:** Supplier GSTIN, HSN, Barcode, Unit, Discount, "
            "Taxable Value, GST Rate, CGST, SGST, IGST, Cess, Total\n\n"
            "Column names don't have to match exactly — common variants "
            "(e.g. \"Vendor\", \"Product\", \"Bill No.\") are recognized "
            "automatically. Rows are grouped into separate bills by "
            "Supplier + Invoice Number, so one sheet can carry several "
            "bills at once. Anything left blank is computed with plain "
            "arithmetic where possible (qty × rate − discount, etc.) and "
            "otherwise flagged for you to fill in during review — nothing "
            "is guessed or silently corrected."
        )
        st.download_button(
            "⬇️ Download sample template",
            data=sample_template_bytes(),
            file_name="purchase_import_template.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    file = st.file_uploader(
        "Purchase sheet (XLSX, XLS or CSV)",
        type=["xlsx", "xls", "csv"],
        key="excel_uploader",
    )
    if not file:
        st.info("Upload a spreadsheet to import one or more purchase bills at once.")
        return

    raw = file.getvalue()
    filename = safe_filename(file.name)
    st.markdown(f"**{filename}** · {len(raw)/1024:.0f} KB")

    if st.button("Import", type="primary", key="excel_import_btn"):
        try:
            results = ingest_excel(db, company, user, filename, raw, InvoiceValidator())
        except ExcelImportError as exc:
            st.error(f"**Could not import this sheet.**\n\n{exc}")
            return
        except Exception as exc:
            audit_service.record_error(db, company.id, "excel_import", str(exc), exc)
            friendly_error("Excel import", exc)
            return

        st.success(f"Imported {len(results)} bill(s) from '{filename}'.")

        for result in results:
            invoice = result.invoice
            st.markdown("---")
            st.markdown(
                f"**{invoice.supplier_name or 'Unknown supplier'} · "
                f"{invoice.invoice_number or 'no invoice number'}** → "
                f"{status_badge(invoice.status)}",
                unsafe_allow_html=True,
            )

            for warning in result.warnings:
                st.info(warning)

            if result.duplicate_reason:
                st.error(f"⚠️ Possible duplicate invoice detected. {result.duplicate_reason}")
                st.caption(
                    "This bill was saved with status *Duplicate*. Open it in "
                    "Review Bills to compare and decide."
                )

            cols = st.columns(4)
            cols[0].metric("Supplier", (invoice.supplier_name or "—")[:22])
            cols[1].metric("Invoice no.", invoice.invoice_number or "—")
            cols[2].metric("Items", len(invoice.items))
            cols[3].metric(
                "Total",
                rupees(invoice.grand_total),
                f"{confidence_light(invoice.overall_confidence)} "
                f"{(invoice.overall_confidence or 0):.0%}",
            )

            errors = len(result.report.errors)
            warnings_count = len(result.report.warnings)
            if errors:
                st.error(f"{errors} blocking issue(s) and {warnings_count} warning(s) need review.")
            elif warnings_count:
                st.warning(f"{warnings_count} field(s) need a quick check.")
            else:
                st.success("Arithmetic and GST checks passed. Verify and confirm.")

            if st.button(
                "Open verification screen", key=f"open_excel_{invoice.id}", type="secondary"
            ):
                st.session_state.review_invoice_id = invoice.id
                st.session_state.nav = "Review Bills"
                st.rerun()

        if st.button("Go to Review Bills", type="primary", key="excel_go_review"):
            st.session_state.nav = "Review Bills"
            st.rerun()
