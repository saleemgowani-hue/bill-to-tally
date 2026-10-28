"""
Seed a demo company so you can click through the app immediately.

    python seed_demo.py                 # 5 products with opening stock
    python seed_demo.py --empty         # same products, stock 0
    python seed_demo.py --no-products   # no products at all
    python seed_demo.py --no-suppliers  # no sample supplier either
    python seed_demo.py --reset         # delete the demo company and rebuild it
    python seed_demo.py --no-wipe       # keep entered data (no 60-minute sweep)

Creates: demo@snsoftech.local / Demo@1234  (Admin)

Why products are seeded at all: product matching cannot be demonstrated
against an empty master. The opening stock is only there so the stock
figures move visibly when you post a bill. Use --empty if you would rather
start every product at zero.
"""
import argparse

from database.database import (
    create_company_with_admin, delete_company, get_session, init_db, set_setting,
)
from database.models import Company, Product, Supplier, User
from services import demo_service, license_service
from utils.config import settings

DEMO_EMAIL = "demo@snsoftech.local"
DEMO_PASSWORD = "Demo@1234"
DEMO_COMPANY = "SN Demo Traders"

# name, sku, barcode, hsn, unit, gst%, purchase rate, opening stock
PRODUCTS = [
    ("Parle-G Gold Biscuit 800 gm", "PG800", "8901719101234", "19053100", "PCS", 18.0, 95.0, 120),
    ("Tata Salt 1 kg",              "TS1KG", "",              "25010020", "PCS",  5.0, 22.0, 300),
    ("Amul Butter 500 gm",          "AB500", "",              "04051000", "PCS", 12.0, 265.0, 40),
    ("Britannia Marie Gold 250 gm", "BMG250","",              "19053100", "PCS", 18.0, 30.0, 80),
    ("Fortune Sunflower Oil 1 L",   "FSO1L", "",              "15121110", "LTR",  5.0, 145.0, 60),
    ("Colgate Strong Teeth 100 gm", "CST100","",              "33061020", "PCS", 18.0, 52.0, 45),
    ("Maggi Noodles 70 gm",         "MAG70", "",              "19023010", "PCS", 18.0, 14.0, 200),
]

def delete_demo_company(db) -> bool:
    """Remove the demo company and everything belonging to it."""
    company = db.query(Company).filter(Company.name == DEMO_COMPANY).one_or_none()
    if company is None:
        return False
    delete_company(db, company)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the demo company.")
    parser.add_argument(
        "--empty", action="store_true",
        help="create the products with zero opening stock",
    )
    parser.add_argument(
        "--no-products", action="store_true",
        help="create no products at all (product matching will find nothing)",
    )
    parser.add_argument(
        "--no-suppliers", action="store_true",
        help="create no sample supplier, so the supplier list starts empty",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="delete the existing demo company first, then rebuild it",
    )
    parser.add_argument(
        "--no-wipe", action="store_true",
        help="do not mark it as a demo, so entered data is kept permanently",
    )
    args = parser.parse_args()

    init_db()
    db = get_session()

    if args.reset and delete_demo_company(db):
        print("Existing demo company deleted.")

    if db.query(User).filter(User.email == DEMO_EMAIL).one_or_none():
        print(f"Demo account already exists: {DEMO_EMAIL} / {DEMO_PASSWORD}")
        print("Use --reset to delete it and start again.")
        return 0

    user = create_company_with_admin(
        db, DEMO_COMPANY, "Demo Admin", DEMO_EMAIL, DEMO_PASSWORD,
        gstin="22ZZZZZ9999Z1Z9", state="Chhattisgarh",
    )
    company = db.query(Company).filter(Company.id == user.company_id).one()
    company.tally_company_name = f"{DEMO_COMPANY} (2026-27)"

    seeded_suppliers, seeded_products = [], []

    if not args.no_suppliers:
        supplier = Supplier(
            company_id=company.id, name="Shreeji Distributors",
            gstin="23ABCDE1234F1Z5", state_code="23",
            address="12 MG Road, Indore, Madhya Pradesh - 452001",
        )
        db.add(supplier)
        seeded_suppliers.append(supplier)

    if not args.no_products:
        for name, sku, barcode, hsn, unit, gst, rate, stock in PRODUCTS:
            product = Product(
                company_id=company.id, name=name, sku=sku, barcode=barcode or None,
                hsn=hsn, unit=unit, gst_rate=gst, purchase_rate=rate,
                current_stock=0.0 if args.empty else float(stock),
            )
            db.add(product)
            seeded_products.append(product)
    db.commit()
    product_count = len(seeded_products)

    set_setting(db, company.id, "ocr_provider", "mock")
    set_setting(db, company.id, "tally_mode", "mock")
    set_setting(db, company.id, "tally_test_mode", "True")

    trial_key = license_service.generate_key("TRIAL")
    license_service.activate(db, company, trial_key, activated_by=user.id)

    # The key is needed at every sign in, so write it somewhere findable
    # rather than leaving it only in this terminal output.
    key_file = settings.DATA_DIR / "demo_licence_key.txt"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(
        f"Demo sign-in details for {DEMO_COMPANY}\n"
        f"  Email    : {DEMO_EMAIL}\n"
        f"  Password : {DEMO_PASSWORD}\n"
        f"  Licence  : {trial_key}\n",
        encoding="utf-8",
    )

    if not args.no_wipe:
        demo_service.mark_as_demo(db, company)
        # Label them as samples so the app can say "this came with the demo"
        # and offer to remove them — nobody uploaded these.
        demo_service.record_sample_data(
            db, company,
            product_ids=[p.id for p in seeded_products],
            supplier_ids=[s.id for s in seeded_suppliers],
        )

    supplier_line = ("Sample supplier: none" if args.no_suppliers
                     else "Sample supplier: Shreeji Distributors")
    if args.no_products:
        stock_line = "Products: none"
    elif args.empty:
        stock_line = f"Products: {product_count}, opening stock 0"
    else:
        stock_line = f"Products: {product_count}, with opening stock"

    print("Demo company seeded.")
    print(f"  Sign in:  {DEMO_EMAIL}")
    print(f"  Password: {DEMO_PASSWORD}")
    print(f"  {stock_line}  |  {supplier_line}")
    print("  OCR: mock  |  Tally: mock + test mode")
    print(f"  Licence:  TRIAL, 14 days  ({trial_key})")
    print(f"  (also saved to {key_file})")
    if args.no_wipe:
        print("  Demo flag OFF - this behaves like a normal company")
    elif demo_service.auto_wipe_enabled():
        print(f"  DEMO account - clears automatically after "
              f"{demo_service.settings.DEMO_RETENTION_MINUTES} minutes, "
              f"or instantly from the button in the app")
    else:
        print("  DEMO account - clear it between customers with the")
        print("  'Clear demo data' button in the app (no automatic timer)")
    print("Now run:  streamlit run app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
