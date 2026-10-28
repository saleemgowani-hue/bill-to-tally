"""Render every page headlessly as a signed-in admin and report exceptions.

Run with:  python tests/smoke_pages.py
This is a manual smoke check, not part of the pytest suite.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

from database.database import get_session  # noqa: E402
from database.models import User  # noqa: E402

DEMO_EMAIL = "demo@snsoftech.local"

PAGES = [
    "Dashboard", "Upload Bill", "Manifest to Excel", "Review Bills",
    "Products", "Suppliers", "Tally Connection", "Purchase History",
    "Reports", "Settings",
]


def main() -> int:
    db = get_session()
    user = db.query(User).filter(User.email == DEMO_EMAIL).one_or_none()
    if user is None:
        print("Run `python seed_demo.py` first.")
        return 1
    uid, cid = user.id, user.company_id
    db.close()

    failures = 0
    for page in PAGES:
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90)
        at.session_state["user_id"] = uid
        at.session_state["company_id"] = cid
        at.session_state["nav"] = page
        at.run()
        if at.exception:
            failures += 1
            print(f"FAIL  {page}")
            for exc in at.exception:
                print("      ", str(exc.value)[:500])
        else:
            print(f"ok    {page}")

    print(f"\nfailures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
