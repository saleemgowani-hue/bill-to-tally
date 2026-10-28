"""
Headless checks for the licence gate on the auth screen and the
expiry block on the main app.

    python tests/smoke_licence.py
"""
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

from database.database import delete_company, get_session  # noqa: E402
from database.models import Company, User  # noqa: E402
from services import license_service  # noqa: E402

DEMO_EMAIL = "demo@snsoftech.local"
APP = str(ROOT / "app.py")


def fresh(**session_state):
    at = AppTest.from_file(APP, default_timeout=90)
    for key, value in session_state.items():
        at.session_state[key] = value
    return at


def text_of(at) -> str:
    parts = []
    for collection in (at.markdown, at.error, at.success, at.warning, at.info, at.caption):
        parts += [element.value for element in collection]
    return " ".join(str(p) for p in parts)


def main() -> int:
    failures = 0
    db = get_session()
    user = db.query(User).filter(User.email == DEMO_EMAIL).one_or_none()
    if user is None:
        print("Run `python seed_demo.py` first.")
        return 1
    company = db.query(Company).filter(Company.id == user.company_id).one()

    def signup_fields(at):
        return {i.key: i for i in at.text_input if i.key and i.key.startswith("signup_")}

    def fill_signup(at, **values):
        fields = signup_fields(at)
        for name, value in values.items():
            fields[f"signup_{name}"].set_value(value)

    def click(at, label):
        for button in at.button:
            if button.label == label:
                button.click().run()
                return True
        return False

    # 1. The sign-up screen must ask for a licence key.
    at = fresh()
    at.run()
    if "signup_key" in signup_fields(at):
        print("ok    sign-up screen asks for a licence key")
    else:
        failures += 1
        print("FAIL  sign-up screen has no licence key field")

    # 2. The sign-up form must submit every field together, so a value can
    #    never be silently missing on the server.
    at = fresh()
    at.run()
    if any(getattr(b, "label", "") == "Create account" for b in at.button):
        print("ok    sign-up uses a submit button")
    else:
        failures += 1
        print("FAIL  no Create account button")

    # 3. An invalid key must be rejected on submit.
    at = fresh()
    at.run()
    fill_signup(
        at, key="SNST-ABCD-EFGH-JKMN-PQRS", company="Test Shop", name="Tester",
        email="reject@example.com", pw="Passw0rd123", pw2="Passw0rd123",
    )
    click(at, "Create account")
    if at.error:
        print("ok    invalid key is rejected on sign up")
    else:
        failures += 1
        print("FAIL  invalid key was not rejected")

    # 4. A missing field must name itself.
    at = fresh()
    at.run()
    fill_signup(at, key=license_service.generate_key("MONTHLY"), company="Test Shop",
                name="Tester")
    click(at, "Create account")
    if any("Work email" in str(e.value) for e in at.error):
        print("ok    a missing field is named in the error")
    else:
        failures += 1
        print(f"FAIL  missing field not named: {[e.value for e in at.error]}")

    # 5. A valid key creates the account and activates the licence.
    at = fresh()
    at.run()
    good = license_service.generate_key("YEARLY")
    unique = f"smoke_{dt.datetime.now():%H%M%S}@example.com"
    fill_signup(at, key=good, company="Smoke Test Traders", name="Smoke Tester",
                email=unique, pw="Passw0rd123", pw2="Passw0rd123")
    click(at, "Create account")
    created = db.query(User).filter(User.email == unique).one_or_none()
    if created is not None:
        company_created = db.query(Company).filter(
            Company.id == created.company_id
        ).one()
        if company_created.license_plan == "YEARLY":
            print("ok    valid key creates the account and activates the licence")
        else:
            failures += 1
            print("FAIL  account created but licence not activated")
        delete_company(db, company_created)   # tidy up for repeat runs
    else:
        failures += 1
        print(f"FAIL  valid key did not create an account: {[e.value for e in at.error]}")

    # 4. An expired licence must block the app.
    original = company.license_expires_at
    company.license_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    db.commit()

    at = fresh(user_id=user.id, company_id=company.id, nav="Dashboard")
    at.run()
    body = text_of(at)
    if "expired" in body.lower() or "renew" in body.lower():
        print("ok    expired licence blocks the app and offers renewal")
    else:
        failures += 1
        print("FAIL  expired licence did not block the app")

    if any("Bills processed" in str(m.value) for m in at.markdown):
        failures += 1
        print("FAIL  dashboard KPIs are visible despite an expired licence")
    else:
        print("ok    dashboard is not reachable with an expired licence")

    company.license_expires_at = original
    db.commit()

    # 5. With a valid licence the dashboard renders again.
    at = fresh(user_id=user.id, company_id=company.id, nav="Dashboard")
    at.run()
    if any("Bills processed" in str(m.value) for m in at.markdown):
        print("ok    dashboard renders with a valid licence")
    else:
        failures += 1
        print("FAIL  dashboard did not render with a valid licence")

    # 6. The demo banner should be visible.
    if "DEMO ACCOUNT" in text_of(at):
        print("ok    demo banner is shown")
    else:
        failures += 1
        print("FAIL  demo banner missing")

    db.close()
    print(f"\nfailures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
