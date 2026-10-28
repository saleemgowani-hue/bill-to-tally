"""
Headless check that the demo-clearing controls exist only on a demo company.

    python tests/smoke_demo_button.py

The service already refuses a real company, but a button that merely *looks*
pressable on a customer's screen is its own problem. This checks the UI.
"""
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

from database.database import create_company_with_admin, delete_company, get_session  # noqa: E402
from database.models import Company, User  # noqa: E402
from services import demo_service  # noqa: E402

DEMO_EMAIL = "demo@snsoftech.local"


def render(user, page):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.session_state["user_id"] = user.id
    at.session_state["company_id"] = user.company_id
    at.session_state["nav"] = page
    at.run()
    labels = [b.label for b in at.button]
    text = " ".join(str(m.value) for m in at.markdown)
    return at, labels, text


def main() -> int:
    failures = 0
    db = get_session()

    demo_user = db.query(User).filter(User.email == DEMO_EMAIL).one_or_none()
    if demo_user is None:
        print("Run `python seed_demo.py` first.")
        return 1
    demo = db.query(Company).filter(Company.id == demo_user.company_id).one()

    # A throwaway real company, removed at the end.
    real_user = create_company_with_admin(
        db, f"Real Shop {uuid.uuid4().hex[:6]}", "Owner",
        f"{uuid.uuid4().hex[:8]}@real.local", "Passw0rd123",
    )
    real = db.query(Company).filter(Company.id == real_user.company_id).one()

    def check(label, condition):
        nonlocal failures
        if condition:
            print(f"ok    {label}")
        else:
            failures += 1
            print(f"FAIL  {label}")

    # ---- demo company ----
    at, labels, text = render(demo_user, "Dashboard")
    check("demo dashboard renders", not at.exception)
    check("demo shows the DEMO banner", "DEMO ACCOUNT" in text)

    at, labels, text = render(demo_user, "Settings")
    check("demo settings renders", not at.exception)
    check("demo settings has the Demo tab", "🧪 Demo" in str(at))

    # ---- real company ----
    at, labels, text = render(real_user, "Dashboard")
    check("real dashboard renders", not at.exception)
    check("real company shows NO demo banner", "DEMO ACCOUNT" not in text)
    check("real dashboard has NO clear button",
          not any("lear demo" in label for label in labels))

    at, labels, text = render(real_user, "Settings")
    check("real settings renders", not at.exception)
    check("real settings has NO Demo tab", "🧪 Demo" not in str(at))
    check("real settings has NO clear button",
          not any("lear demo" in label for label in labels))

    # ---- the service refuses regardless of the UI ----
    try:
        demo_service.clear_demo_data(db, real, real_user)
        check("service refuses a real company", False)
    except demo_service.NotADemoCompany:
        check("service refuses a real company", True)

    check("demo company is still flagged as a demo", demo_service.is_demo(demo))

    delete_company(db, real)
    db.close()
    print(f"\nfailures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
