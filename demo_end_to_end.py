"""
Runs the entire pipeline headlessly against samples/sample_purchase_bill.png:

    upload -> AI extract -> validate -> match -> confirm -> Tally -> stock

Useful as a self-check after installing, and as a demo you can show a customer.

    python seed_demo.py
    python demo_end_to_end.py
"""
from pathlib import Path

from PIL import Image

from database.database import get_session, init_db
from database.models import (
    Company, InvoiceItem, MatchStatus, Product, StockMovement, User,
)
from services.ai_invoice_parser import InvoiceParser
from services.ocr_service import get_provider
from services.product_matcher import ProductMatcher
from services.purchase_service import confirm_invoice, ingest_bill, post_to_tally
from services.tally_connector import MockTallyConnector

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "samples" / "sample_purchase_bill.png"
DEMO_EMAIL = "demo@snsoftech.local"


def rule(title):
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def main():
    init_db()
    db = get_session()
    user = db.query(User).filter(User.email == DEMO_EMAIL).one_or_none()
    if user is None:
        print("Run `python seed_demo.py` first.")
        return 1
    company = db.query(Company).filter(Company.id == user.company_id).one()

    rule("1. UPLOAD  (OCR provider: mock — swap to gemini/openai/claude in Settings)")
    data = SAMPLE.read_bytes()
    images = [Image.open(SAMPLE).convert("RGB")]
    parser = InvoiceParser(get_provider("mock"))
    result = ingest_bill(
        db, company, user, SAMPLE.name, data, images, parser, store_image=False
    )
    invoice = result.invoice
    print(f"   file            : {SAMPLE.name} ({len(data):,} bytes)")
    print(f"   supplier        : {invoice.supplier_name}  [{invoice.supplier_gstin}]")
    print(f"   invoice no/date : {invoice.invoice_number}  {invoice.invoice_date}")
    print(f"   grand total     : Rs {invoice.grand_total:,.2f}")
    print(f"   confidence      : {invoice.overall_confidence:.0%}")
    print(f"   status          : {invoice.status}")

    rule("2. VALIDATION  (nothing is silently corrected)")
    report = result.report
    print(f"   calculated total: Rs {report.calculated_total:,.2f}")
    print(f"   tax scope       : {report.tax_scope}")
    print(f"   blocking        : {report.is_blocking}")
    for issue in report.errors:
        print(f"   ERROR   {issue.message}")
    for issue in report.warnings:
        print(f"   warning {issue.message}")
    if not report.errors and not report.warnings:
        print("   no issues found")

    rule("3. PRODUCT MATCHING  (against the 5 seeded products)")
    items = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == invoice.id).all()
    matcher = ProductMatcher(db, company.id)
    for item in items:
        match = matcher.match({
            "raw_name": item.raw_name, "barcode": item.barcode,
            "hsn": item.hsn, "unit": item.unit,
        })
        print(f"   {item.raw_name:<28} -> {match.status:<18} "
              f"{match.score:.2f}  {match.product_name or '(none)'}")
        print(f"   {'':<28}    {match.reason}")
        if match.product_id and match.status == MatchStatus.MATCHED.value:
            item.product_id = match.product_id

    rule("4. HUMAN VERIFICATION  (operator links anything still unmatched)")
    for item in items:
        if not item.product_id:
            product = matcher.create_product_from_item({
                "normalized_name": item.raw_name, "hsn": item.hsn,
                "unit": item.unit, "gst_rate": item.gst_rate, "rate": item.rate,
                "barcode": item.barcode,
            }, user.id)
            item.product_id = product.id
            print(f"   created new product: {product.name}")
        item.confidence = max(item.confidence or 0, 0.95)
    invoice.overall_confidence = 0.95
    db.commit()

    ok, message = confirm_invoice(
        db, company, user, invoice, override_reason="demo: verified against paper bill"
    )
    print(f"   confirm -> {ok}  |  {message}")
    print(f"   status  -> {invoice.status}")

    rule("5. POST TO TALLY  (mock connector — no accounting entry is created)")
    before = {
        i.product_id: db.query(Product).filter(Product.id == i.product_id).one().current_stock
        for i in items
    }
    tally_result, transaction = post_to_tally(
        db, company, user, invoice,
        MockTallyConnector({"company": company.tally_company_name}),
        run_preflight=False,
    )
    print(f"   success   : {tally_result.success}")
    print(f"   mode      : {tally_result.mode}")
    print(f"   message   : {tally_result.message}")
    print(f"   voucher id: {tally_result.voucher_id}")
    print(f"   status    : {invoice.status}")

    rule("6. STOCK UPDATE  (only after Tally accepted the voucher)")
    for item in items:
        product = db.query(Product).filter(Product.id == item.product_id).one()
        print(f"   {product.name:<30} {before[item.product_id]:>8.1f} "
              f"-> {product.current_stock:>8.1f}   (+{item.quantity})")
    moves = db.query(StockMovement).filter(StockMovement.invoice_id == invoice.id).count()
    print(f"   stock movement rows written: {moves}")

    rule("7. IDEMPOTENCY  (re-posting must not double the stock)")
    from database.models import InvoiceStatus
    invoice.status = InvoiceStatus.CONFIRMED.value
    db.commit()
    again, _ = post_to_tally(
        db, company, user, invoice, MockTallyConnector(), run_preflight=False
    )
    product = db.query(Product).filter(Product.id == items[0].product_id).one()
    print(f"   second post success : {again.success}")
    print(f"   message             : {again.message}")
    print(f"   stock unchanged at  : {product.current_stock}")

    rule("GENERATED TALLY XML (first 900 characters)")
    print((transaction.request_xml or "")[:900])

    print("\nPipeline completed end to end.\n")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
