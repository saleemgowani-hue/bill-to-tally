"""Product master: browse, edit, import and manage learned aliases."""
from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from database.models import Product, ProductAlias, StockMovement
from services import audit_service, demo_service
from utils.ui import brand_header, require_login, rupees, session_db


def render() -> None:
    user, company = require_login()
    db = session_db()
    brand_header(f"{company.name} · Products")

    tab_list, tab_add, tab_import, tab_alias, tab_moves = st.tabs(
        ["Product master", "Add product", "Import CSV", "Learned mappings", "Stock movements"]
    )

    with tab_list:
        _list_tab(db, company, user)
    with tab_add:
        _add_tab(db, company, user)
    with tab_import:
        _import_tab(db, company, user)
    with tab_alias:
        _alias_tab(db, company)
    with tab_moves:
        _movements_tab(db, company)


def _list_tab(db, company, user):
    search = st.text_input("Search by name, SKU, barcode or HSN")
    query = db.query(Product).filter(Product.company_id == company.id)
    if search.strip():
        pattern = f"%{search.strip()}%"
        query = query.filter(
            Product.name.ilike(pattern)
            | Product.sku.ilike(pattern)
            | Product.barcode.ilike(pattern)
            | Product.hsn.ilike(pattern)
        )
    products = query.order_by(Product.name).limit(500).all()

    if not products:
        st.info("No products yet. Add them here, import a CSV, or let the review "
                "screen create them from your first bill.")
        return

    # Seeded demo products carry opening stock nobody entered. Saying so here
    # answers "where did this quantity come from?" on the screen that raises it.
    samples = demo_service.sample_ids(db, company)["products"]

    frame = pd.DataFrame(
        [
            {
                "ID": p.id, "Name": p.name,
                "Source": "Sample" if p.id in samples else "Entered",
                "SKU": p.sku or "", "Barcode": p.barcode or "",
                "HSN": p.hsn or "", "Unit": p.unit or "", "GST %": p.gst_rate or 0,
                "Purchase rate": p.purchase_rate or 0, "Stock": p.current_stock or 0,
                "Tally item": p.tally_stock_item or "", "Active": bool(p.is_active),
            }
            for p in products
        ]
    )
    st.caption(f"{len(products)} product(s). Edit cells below and save.")
    if samples:
        st.caption(
            "**Sample** rows came with the demo, with opening stock, so purchases "
            "visibly add to something. Remove them from Settings → Demo."
        )
    edited = st.data_editor(
        frame,
        hide_index=True,
        width="stretch",
        disabled=["ID", "Source"],
        key="product_editor",
    )
    if st.button("Save changes", type="primary"):
        changes = 0
        lookup = {p.id: p for p in products}
        for _, row in edited.iterrows():
            product = lookup.get(int(row["ID"]))
            if product is None:
                continue
            updates = {
                "name": row["Name"], "sku": row["SKU"] or None,
                "barcode": str(row["Barcode"]) or None, "hsn": str(row["HSN"]) or None,
                "unit": row["Unit"], "gst_rate": float(row["GST %"] or 0),
                "purchase_rate": float(row["Purchase rate"] or 0),
                "tally_stock_item": row["Tally item"] or None,
                "is_active": bool(row["Active"]),
            }
            for field, value in updates.items():
                if getattr(product, field) != value:
                    setattr(product, field, value)
                    changes += 1
            new_stock = float(row["Stock"] or 0)
            if abs((product.current_stock or 0) - new_stock) > 1e-9:
                db.add(
                    StockMovement(
                        company_id=company.id, product_id=product.id,
                        quantity_before=product.current_stock or 0,
                        quantity_change=new_stock - (product.current_stock or 0),
                        quantity_after=new_stock, reason="Manual adjustment",
                    )
                )
                product.current_stock = new_stock
                changes += 1
        db.commit()
        audit_service.record(
            db, company.id, "PRODUCTS_EDITED", f"{changes} field(s) updated", user=user
        )
        st.success(f"Saved {changes} change(s).")
        st.rerun()

    st.download_button(
        "⬇️ Export product master (CSV)",
        data=frame.to_csv(index=False).encode(),
        file_name=f"products_{company.id}.csv",
        mime="text/csv",
    )


def _add_tab(db, company, user):
    with st.form("add_product"):
        col1, col2, col3 = st.columns(3)
        name = col1.text_input("Product name *")
        sku = col2.text_input("SKU")
        barcode = col3.text_input("Barcode / EAN")
        col4, col5, col6 = st.columns(3)
        hsn = col4.text_input("HSN/SAC")
        unit = col5.text_input("Unit", value="PCS")
        gst_rate = col6.number_input("GST %", 0.0, 40.0, 0.0, 0.5)
        col7, col8, col9 = st.columns(3)
        purchase_rate = col7.number_input("Purchase rate", 0.0, step=0.5)
        opening_stock = col8.number_input("Opening stock", 0.0, step=1.0)
        tally_item = col9.text_input("Tally stock item name", help="Leave blank to use the product name")
        if st.form_submit_button("Add product", type="primary"):
            if not name.strip():
                st.error("Product name is required.")
                return
            product = Product(
                company_id=company.id, name=name.strip()[:250], sku=sku or None,
                barcode=barcode or None, hsn=hsn or None, unit=unit or "PCS",
                gst_rate=gst_rate, purchase_rate=purchase_rate,
                current_stock=opening_stock, tally_stock_item=tally_item or None,
            )
            db.add(product)
            db.commit()
            audit_service.record(
                db, company.id, "PRODUCT_CREATED", f"'{product.name}' added manually", user=user
            )
            st.success(f"'{product.name}' added.")


def _import_tab(db, company, user):
    st.caption(
        "CSV columns: name (required), sku, barcode, hsn, unit, gst_rate, "
        "purchase_rate, current_stock, tally_stock_item"
    )
    template = pd.DataFrame(
        [
            {
                "name": "Parle-G Gold Biscuit 800 gm", "sku": "PG800", "barcode": "8901719101234",
                "hsn": "19053100", "unit": "PCS", "gst_rate": 18, "purchase_rate": 95,
                "current_stock": 0, "tally_stock_item": "",
            }
        ]
    )
    st.download_button(
        "⬇️ Download template",
        data=template.to_csv(index=False).encode(),
        file_name="product_template.csv",
        mime="text/csv",
    )
    uploaded = st.file_uploader("Product CSV", type=["csv"], key="prod_csv")
    if uploaded is None:
        return
    try:
        frame = pd.read_csv(io.BytesIO(uploaded.getvalue()))
    except Exception as exc:
        st.error(f"Could not read that CSV: {exc}")
        return
    if "name" not in frame.columns:
        st.error("The CSV must have a 'name' column.")
        return
    st.dataframe(frame.head(20), hide_index=True, width="stretch")
    if st.button(f"Import {len(frame)} row(s)", type="primary"):
        created = skipped = 0
        for _, row in frame.iterrows():
            name = str(row.get("name") or "").strip()
            if not name:
                skipped += 1
                continue
            exists = (
                db.query(Product)
                .filter(Product.company_id == company.id, Product.name.ilike(name))
                .first()
            )
            if exists:
                skipped += 1
                continue
            db.add(
                Product(
                    company_id=company.id, name=name[:250],
                    sku=str(row.get("sku") or "") or None,
                    barcode=str(row.get("barcode") or "") or None,
                    hsn=str(row.get("hsn") or "") or None,
                    unit=str(row.get("unit") or "PCS"),
                    gst_rate=float(row.get("gst_rate") or 0),
                    purchase_rate=float(row.get("purchase_rate") or 0),
                    current_stock=float(row.get("current_stock") or 0),
                    tally_stock_item=str(row.get("tally_stock_item") or "") or None,
                )
            )
            created += 1
        db.commit()
        audit_service.record(
            db, company.id, "PRODUCTS_IMPORTED",
            f"{created} created, {skipped} skipped from '{uploaded.name}'", user=user,
        )
        st.success(f"Imported {created} product(s); skipped {skipped} duplicate/blank row(s).")


def _alias_tab(db, company):
    st.caption(
        "Every mapping you confirm on the review screen is remembered here, so the "
        "same supplier wording matches instantly next time."
    )
    aliases = (
        db.query(ProductAlias)
        .filter(ProductAlias.company_id == company.id)
        .order_by(ProductAlias.times_used.desc())
        .limit(300)
        .all()
    )
    if not aliases:
        st.info("No learned mappings yet.")
        return
    products = {
        p.id: p.name
        for p in db.query(Product).filter(Product.company_id == company.id).all()
    }
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Bill wording": a.alias_raw,
                    "Mapped product": products.get(a.product_id, f"#{a.product_id}"),
                    "Barcode": a.barcode or "",
                    "Times used": a.times_used or 1,
                }
                for a in aliases
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    to_delete = st.selectbox(
        "Remove a mapping",
        [None] + [a.id for a in aliases],
        format_func=lambda i: "— select —" if i is None else
        next(a.alias_raw for a in aliases if a.id == i),
    )
    if to_delete and st.button("Delete mapping"):
        db.delete(db.query(ProductAlias).filter(ProductAlias.id == to_delete).one())
        db.commit()
        st.success("Mapping removed.")
        st.rerun()


def _movements_tab(db, company):
    movements = (
        db.query(StockMovement)
        .filter(StockMovement.company_id == company.id)
        .order_by(StockMovement.created_at.desc())
        .limit(300)
        .all()
    )
    if not movements:
        st.info("No stock movements recorded yet.")
        return
    products = {
        p.id: p.name
        for p in db.query(Product).filter(Product.company_id == company.id).all()
    }
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "When": m.created_at.strftime("%d-%m-%Y %H:%M") if m.created_at else "",
                    "Product": products.get(m.product_id, f"#{m.product_id}"),
                    "Before": m.quantity_before,
                    "Change": f"+{m.quantity_change:g}" if (m.quantity_change or 0) >= 0
                    else f"{m.quantity_change:g}",
                    "After": m.quantity_after,
                    "Reason": m.reason or "",
                    # str(): mixing an int with "" gives an object column
                    # that Arrow cannot render.
                    "Invoice": str(m.invoice_id) if m.invoice_id else "",
                }
                for m in movements
            ]
        ),
        hide_index=True,
        width="stretch",
    )
