"""
Prepare an empty database for a real customer install — VENDOR TOOL OUTPUT.

    python init_customer_db.py

Unlike seed_demo.py, this creates no company, no user, and no trial licence.
It only creates the (empty) tables, so the very first screen the customer
sees is the app's own Sign Up page, where a licence key is required before
any company can be created. This is what ships inside a customer build —
see make_customer_build.py, which swaps seed_demo.py out for this in
START.bat / INSTALL.bat.
"""
from database.database import init_db


def main() -> int:
    init_db()
    print("Database ready. Sign up on the page that opens using your licence key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
