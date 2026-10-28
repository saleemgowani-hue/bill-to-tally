"""Walk the customer's exact complaint, on screen, end to end.

  python seed_demo.py
  python tests/smoke_visibility.py

Checks, against a real rendered page rather than a function call:
  * the Excel option is offered on Tally Connection
  * Reports has an Excel export tab
  * a bill that is not posted yet is named on the report, with its next step
  * a posted bill then appears in the report
  * the seeded supplier and products are labelled "Sample"
  * the build's version is visible in the sidebar
"""
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

from database.database import get_session  # noqa: E402
from database.models import (  # noqa: E402
    Company, Invoice, InvoiceStatus, Supplier, User,
)
from utils.config import settings  # noqa: E402

DEMO_EMAIL = "demo@snsoftech.local"

failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    if condition:
        print(f"ok    {label}")
    else:
        failures += 1
        print(f"FAIL  {label}")
        if detail:
            print(f"      {detail[:600]}")


def page(uid, cid, name):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90)
    at.session_state["user_id"] = uid
    at.session_state["company_id"] = cid
    at.session_state["nav"] = name
    at.run()
    return at


def text_of(at) -> str:
    parts = []
    for collection in (at.markdown, at.caption, at.info, at.warning,
                       at.error, at.success):
        try:
            parts += [str(element.value) for element in collection]
        except Exception:
            pass
    for element in at.radio:
        parts += [str(option) for option in element.options]
    for element in at.tabs:
        parts.append(str(getattr(element, "label", "")))
    return "\n".join(parts)


def main() -> int:
    db = get_session()
    user = db.query(User).filter(User.email == DEMO_EMAIL).one_or_none()
    if user is None:
        print("Run `python seed_demo.py` first.")
        return 1
    uid, cid = user.id, user.company_id
    company = db.query(Company).filter(Company.id == cid).one()

    # --- the customer's bill, still waiting to be posted -------------------
    db.query(Invoice).filter(
        Invoice.company_id == cid, Invoice.invoice_number == "SMOKE-1"
    ).delete(synchronize_session=False)
    db.commit()
    bill = Invoice(
        company_id=cid, supplier_name="Balaji Traders", invoice_number="SMOKE-1",
        invoice_date=dt.date.today().isoformat(), grand_total=4820.0,
        status=InvoiceStatus.REVIEW_REQUIRED.value,
    )
    db.add(bill)
    db.commit()
    bill_id = bill.id
    db.close()

    # --- Tally Connection: Excel must be on offer -------------------------
    at = page(uid, cid, "Tally Connection")
    check("Tally Connection renders", not at.exception,
          str(at.exception[0].value) if at.exception else "")
    check("Excel is offered as a connection", "Excel" in text_of(at))

    # --- Reports: the waiting bill is named, with its next step -----------
    at = page(uid, cid, "Reports")
    body = text_of(at)
    check("Reports renders", not at.exception,
          str(at.exception[0].value) if at.exception else "")
    check("Reports has an Excel export tab", "Excel export" in body)
    check("the unposted bill is accounted for", "Review Required" in body, body)
    check("the next step is spelled out",
          "Confirm" in body or "Review bills" in body, body)
    check("it no longer dead-ends on 'nothing to report'",
          "nothing to report" not in body.lower(), body)

    # --- post it, and it should appear ------------------------------------
    db = get_session()
    db.query(Invoice).filter(Invoice.id == bill_id).update(
        {"status": InvoiceStatus.POSTED.value}
    )
    db.commit()
    db.close()

    at = page(uid, cid, "Reports")
    body = text_of(at)
    numbers = [str(m.value) for m in at.metric]
    check("the posted bill reaches the report",
          any("4,820" in n or "4820" in n for n in numbers) or "Balaji" in body,
          " | ".join(numbers))

    # --- an exported bill counts too --------------------------------------
    db = get_session()
    db.query(Invoice).filter(Invoice.id == bill_id).update(
        {"status": InvoiceStatus.EXPORTED.value}
    )
    db.commit()
    db.close()

    at = page(uid, cid, "Reports")
    numbers = [str(m.value) for m in at.metric]
    check("an exported bill counts as entered",
          any("4,820" in n or "4820" in n for n in numbers),
          " | ".join(numbers))

    # --- sample labels -----------------------------------------------------
    at = page(uid, cid, "Suppliers")
    body = text_of(at)
    frames = [str(element.value) for element in at.get("data_editor")]
    check("Suppliers renders", not at.exception,
          str(at.exception[0].value) if at.exception else "")
    check("the seeded supplier is labelled a Sample",
          "Sample" in body or any("Sample" in f for f in frames), body)

    at = page(uid, cid, "Products")
    body = text_of(at)
    frames = [str(element.value) for element in at.get("data_editor")]
    check("Products renders", not at.exception,
          str(at.exception[0].value) if at.exception else "")
    check("the seeded products are labelled Samples",
          "Sample" in body or any("Sample" in f for f in frames), body)

    # --- the build is identifiable ----------------------------------------
    at = page(uid, cid, "Dashboard")
    sidebar = "\n".join(str(element.value) for element in at.sidebar.caption)
    check("the sidebar shows the version and build date",
          settings.APP_VERSION in sidebar and settings.BUILD_DATE in sidebar,
          sidebar)

    # --- tidy up -----------------------------------------------------------
    db = get_session()
    db.query(Invoice).filter(Invoice.id == bill_id).delete(synchronize_session=False)
    db.commit()
    db.close()

    print(f"\nfailures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
