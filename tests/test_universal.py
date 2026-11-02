"""Universal AI Bill-to-Tally upgrade: Gemini engine, universal schema,
document classifier, validation additions, accounting vouchers, API usage."""
from __future__ import annotations

import json

import pytest
import requests as real_requests
from PIL import Image

from services import gemini_engine
from services.ai_invoice_parser import InvoiceParser, ParseError
from services.document_classifier import classify
from services.gemini_engine import GeminiUniversalProvider, merge_chunk_results
from services.invoice_validator import (
    STATUS_ERROR,
    STATUS_NEEDS_REVIEW,
    STATUS_READY,
    STATUS_VERIFIED,
    InvoiceValidator,
    verification_status,
    words_to_number,
)
from services.ocr_service import OCRConfigError, OCRProvider, OCRResponse
from services.tally_connector import (
    AccountingVoucherPayload,
    LedgerVoucherPayload,
    MockTallyConnector,
    TallyError,
    VoucherPayload,
    build_accounting_voucher_xml,
    build_ledger_voucher_xml,
    build_sales_voucher_xml,
    check_balanced,
    required_masters,
)
from services.universal_schema import (
    build_universal_document,
    extract_extras,
    to_legacy_shape,
)


def page(color=255):
    return Image.new("RGB", (40, 40), (color, color, color))


def universal_answer(items, **extra):
    data = {
        "document_type": "purchase_invoice",
        "document_type_confidence": 0.95,
        "seller": {"name": "Shreeji Distributors", "gstin": "27ABCDE1234F1Z5"},
        "buyer": {"name": "Our Shop", "gstin": "27AAAAA0000A1Z5"},
        "invoice": {"invoice_number": "INV-9", "invoice_date": "2026-10-20",
                    "eway_bill_number": "1234 5678 9012"},
        "items": items,
        "overall_confidence": 0.95,
    }
    data.update(extra)
    return data


def item(name, qty=1, rate=100, gst=18, page_no=1):
    taxable = qty * rate
    tax = round(taxable * gst / 100 / 2, 2)
    return {"raw_name": name, "quantity": qty, "rate": rate, "taxable_value": taxable,
            "gst_rate": gst, "cgst": tax, "sgst": tax, "total": taxable + 2 * tax,
            "confidence": 0.95, "page": page_no}


# --------------------------------------------------------------------------
# Fake Google endpoint
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.headers = {}
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise real_requests.HTTPError(f"{self.status_code}")


class FakeGoogle:
    exceptions = real_requests.exceptions

    def __init__(self, answer_for_pages, status=200, truncate_over=None):
        self.answer_for_pages = answer_for_pages
        self.status = status
        self.truncate_over = truncate_over
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        n_images = sum(1 for p in json["contents"][0]["parts"] if "inline_data" in p)
        self.calls.append({"url": url, "headers": headers, "pages": n_images,
                           "prompt": json["contents"][0]["parts"][0]["text"]})
        if self.status != 200:
            return FakeResponse(self.status, {"error": {"message": "nope"}})
        finish = "STOP"
        if self.truncate_over and n_images > self.truncate_over:
            finish = "MAX_TOKENS"
        answer = self.answer_for_pages(n_images, len(self.calls))
        return FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": json_dumps(answer)}]},
                            "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 1000 * n_images,
                              "candidatesTokenCount": 200, "totalTokenCount": 1000 * n_images + 200},
        })

    def get(self, url, headers=None, params=None, timeout=None):
        if headers.get("x-goog-api-key") != "good-key":
            return FakeResponse(400, {"error": {"message": "API key not valid"}})
        return FakeResponse(200, {"models": [
            {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-embedding-2", "supportedGenerationMethods": ["embedContent"]},
        ]})


def json_dumps(data):
    return json.dumps(data)


@pytest.fixture
def fake_google(monkeypatch):
    holder = {}

    def install(fake):
        holder["fake"] = fake
        monkeypatch.setattr(gemini_engine, "_requests", lambda: fake)
        return fake
    return install


# --------------------------------------------------------------------------
class TestGeminiEngine:
    def test_key_in_header_never_in_url(self, fake_google):
        fake = fake_google(FakeGoogle(lambda n, c: universal_answer([item("A")])))
        GeminiUniversalProvider({"api_key": "secret-123"}).extract_structured(
            [page()], "prompt {page_note}", "schema")
        assert fake.calls[0]["headers"]["x-goog-api-key"] == "secret-123"
        assert "secret-123" not in fake.calls[0]["url"]

    def test_long_pdf_is_split_into_page_groups_and_merged_in_order(self, fake_google):
        def answer(n, call_no):
            rows = [item(f"Row {call_no}-{i}", page_no=i + 1) for i in range(n)]
            extra = {"grand_total": 999} if call_no == 3 else {}
            return universal_answer(rows, **extra)
        fake = fake_google(FakeGoogle(answer))
        usage = []
        provider = GeminiUniversalProvider(
            {"api_key": "k", "pages_per_call": 4, "on_usage": usage.append})
        response = provider.extract_structured([page()] * 10, "p {page_note}", "s")
        assert [c["pages"] for c in fake.calls] == [4, 4, 2]
        names = [i["raw_name"] for i in response.structured["items"]]
        assert names[0] == "Row 1-0" and names[-1] == "Row 3-1" and len(names) == 10
        assert [i["page"] for i in response.structured["items"]] == list(range(1, 11))
        assert response.structured["grand_total"] == 999
        assert len(usage) == 3 and usage[0]["prompt_tokens"] == 4000
        assert "pages 5-8 of a 10-page document" in fake.calls[1]["prompt"]

    def test_truncated_answer_is_split_not_lost(self, fake_google):
        fake = fake_google(FakeGoogle(
            lambda n, c: universal_answer([item(f"R{c}-{i}") for i in range(n)]),
            truncate_over=1,
        ))
        response = GeminiUniversalProvider({"api_key": "k", "pages_per_call": 2}) \
            .extract_structured([page(), page()], "p {page_note}", "s")
        assert len(response.structured["items"]) == 2
        assert [c["pages"] for c in fake.calls] == [2, 1, 1]

    def test_missing_key_and_quota_are_config_errors(self, fake_google, monkeypatch):
        import services.ocr_service as ocr
        monkeypatch.setattr(ocr.time, "sleep", lambda s: None)
        with pytest.raises(OCRConfigError):
            GeminiUniversalProvider({}).extract_structured([page()], "p", "s")
        fake_google(FakeGoogle(lambda n, c: {}, status=429))
        with pytest.raises(OCRConfigError):
            GeminiUniversalProvider({"api_key": "k"}).extract_structured([page()], "p", "s")

    def test_blocked_by_usage_limit(self):
        with pytest.raises(OCRConfigError, match="limit"):
            GeminiUniversalProvider({"api_key": "k", "blocked_reason": "monthly limit used"}) \
                .extract_structured([page()], "p", "s")

    def test_validate_key_lists_document_models_only(self, fake_google):
        fake_google(FakeGoogle(lambda n, c: {}))
        ok, _, models = gemini_engine.validate_key("good-key")
        assert ok and models == ["gemini-flash-latest", "gemini-3.8-flash"]
        ok, message, _ = gemini_engine.validate_key("bad-key")
        assert not ok and "rejected" in message

    def test_mask_key(self):
        assert gemini_engine.mask_key("AIzaSyABCDEFGHIJ1234") == "AIza••••••••1234"


class TestMerge:
    def test_header_from_first_filled_totals_from_last(self):
        a = universal_answer([item("A")], grand_total=0)
        a["invoice"]["invoice_number"] = ""
        b = universal_answer([item("B")], grand_total=500, freight=50)
        merged = merge_chunk_results([a, b], [0, 1])
        assert merged["invoice"]["invoice_number"] == "INV-9"
        assert merged["grand_total"] == 500 and merged["freight"] == 50
        assert [i["raw_name"] for i in merged["items"]] == ["A", "B"]


# --------------------------------------------------------------------------
class UniversalStub(OCRProvider):
    name = "universal"
    supports_structured = True
    uses_universal_schema = True

    def __init__(self, answer=None, error=None):
        super().__init__({})
        self.answer, self.error, self.prompts = answer, error, []

    def extract_text(self, images):
        return OCRResponse(provider=self.name, text="")

    def extract_structured(self, images, prompt, schema_hint):
        self.prompts.append((prompt, schema_hint))
        if self.error:
            raise self.error
        return OCRResponse(provider=self.name, model="g", structured=self.answer)


class TextStub(OCRProvider):
    name = "rapidocr"

    def extract_text(self, images):
        return OCRResponse(provider=self.name, text="Invoice No: T-1\nTata Salt 1kg 10 25 250")


class TestParser:
    def test_universal_answer_maps_to_existing_contract(self):
        answer = universal_answer([item("Tata Salt")], freight=40,
                                  amount_in_words="Rupees One Hundred Fifty Eight Only")
        parsed = InvoiceParser(UniversalStub(answer)).parse_images([page()])
        assert parsed.data["supplier"]["name"] == "Shreeji Distributors"
        assert parsed.data["universal"]["charges"]["freight"] == 40
        assert parsed.data["universal"]["invoice_details"]["eway_bill_number"] == "1234 5678 9012"
        assert parsed.data["universal"]["document_type"] == "purchase_invoice"

    def test_sales_flow_takes_buyer_as_the_other_party(self):
        answer = universal_answer([item("X")], document_type="sales_invoice")
        parsed = InvoiceParser(UniversalStub(answer)).parse_images([page()], "sales")
        assert parsed.data["supplier"]["name"] == "Our Shop"

    def test_offline_fallback_when_gemini_unreachable(self):
        parser = InvoiceParser(UniversalStub(error=OCRConfigError("no key")), fallback=TextStub())
        parsed = parser.parse_images([page()])
        assert parsed.fallback_used and parsed.provider == "rapidocr"
        assert "offline" in parsed.warnings[0]

    def test_no_fallback_raises(self):
        with pytest.raises(ParseError):
            InvoiceParser(UniversalStub(error=OCRConfigError("no key"))).parse_images([page()])

    def test_config_error_is_not_retried(self):
        stub = UniversalStub(error=OCRConfigError("bad key"))
        with pytest.raises(ParseError):
            InvoiceParser(stub, max_attempts=3).parse_images([page()])
        assert len(stub.prompts) == 1


# --------------------------------------------------------------------------
class TestClassifier:
    def test_bilty_is_not_forced_into_purchase(self):
        c = classify("transport_bilty", 0.9, flow="purchase")
        assert not c.is_accounting and not c.matches_flow
        assert c.suggested_action == "manifest"

    def test_text_signal_overrides_unsure_ai(self):
        c = classify("purchase_invoice", 0.5, text="CREDIT NOTE No. CN-12")
        assert c.document_type == "credit_note" and c.voucher_type == "Credit Note"

    def test_own_gstin_as_seller_means_sales(self):
        c = classify("purchase_invoice", 0.9, seller_gstin="27AAAAA0000A1Z5",
                     our_gstin="27AAAAA0000A1Z5")
        assert c.document_type == "sales_invoice" and not c.matches_flow

    def test_expense_bill_is_fine_on_purchase_screen(self):
        assert classify("expense_receipt", 0.9).matches_flow


# --------------------------------------------------------------------------
def invoice_data(freight=0.0, words="", grand_total=None):
    items = [item("A", 2, 100), item("B", 1, 50)]
    sub = 250.0
    tax = round(sum(i["cgst"] + i["sgst"] for i in items), 2)
    raw = universal_answer(items, freight=freight, amount_in_words=words)
    data = {
        "supplier": {"name": "S", "gstin": "27ABCDE1234F1Z5"},
        "invoice": {"invoice_number": "1", "invoice_date": "2026-10-20"},
        "items": items, "subtotal": sub, "tax_total": tax, "round_off": 0,
        "grand_total": grand_total if grand_total is not None else sub + tax + freight,
        "overall_confidence": 0.95,
        "universal": extract_extras(raw),
    }
    return data


class TestValidation:
    def test_words(self):
        assert words_to_number("Rupees Three Hundred Forty Five and Fifty Paise Only") == 345.5
        assert words_to_number("One Crore Two Lakh Only") == 10_200_000
        assert words_to_number("blah blah") is None

    def test_freight_is_part_of_the_total(self):
        report = InvoiceValidator().validate(invoice_data(freight=40), "27AAAAA0000A1Z5")
        assert report.calculated_charges == 40
        assert not [i for i in report.errors if i.field == "grand_total"]

    def test_freight_on_bill_but_missing_from_total_is_flagged(self):
        report = InvoiceValidator().validate(
            invoice_data(freight=40, grand_total=295.0), "27AAAAA0000A1Z5")
        assert [i for i in report.errors if i.field == "grand_total"]

    def test_amount_in_words_mismatch_warns(self):
        report = InvoiceValidator().validate(
            invoice_data(words="Rupees Nine Hundred Only"), "27AAAAA0000A1Z5")
        assert any(i.field == "amount_in_words" for i in report.warnings)

    def test_future_date_warns(self):
        data = invoice_data()
        data["invoice"]["invoice_date"] = "2099-01-01"
        report = InvoiceValidator().validate(data, "27AAAAA0000A1Z5")
        assert any(i.field == "invoice.invoice_date" for i in report.warnings)

    def test_status_ladder(self):
        clean = InvoiceValidator().validate(invoice_data(), "27AAAAA0000A1Z5")
        assert verification_status(clean) in (STATUS_VERIFIED, STATUS_NEEDS_REVIEW)
        assert verification_status(clean, confirmed=True) == STATUS_READY
        bad = InvoiceValidator().validate({"items": []}, "")
        assert verification_status(bad) == STATUS_ERROR
        assert clean.to_dict()["verification_status"]

    def test_validator_never_edits_the_data(self):
        data = invoice_data(freight=40, grand_total=1.0)
        before = json.dumps(data, sort_keys=True)
        InvoiceValidator().validate(data, "")
        assert json.dumps(data, sort_keys=True) == before


class TestUniversalDocument:
    def test_common_shape(self):
        data = invoice_data(freight=40)
        doc = build_universal_document(data, {"issues": [
            {"severity": "warning", "field": "x", "message": "m"}]}, {"file": "a.jpg"})
        for key in ("document_type", "supplier", "customer", "invoice_details", "items",
                    "tax_summary", "charges", "totals", "extraction_confidence",
                    "validation_errors", "source_document"):
            assert key in doc
        assert doc["charges"]["freight"] == 40
        assert doc["invoice_details"]["eway_bill_number"] == "1234 5678 9012"
        assert doc["validation_errors"][0]["field"] == "x"
        assert doc["items"][0]["page"] == 1

    def test_not_printed_stays_none(self):
        extras = extract_extras(universal_answer([]))
        assert extras["charges"]["freight"] is None

    def test_legacy_shape_untouched_for_old_answers(self):
        old = {"supplier": {"name": "A"}}
        assert to_legacy_shape(old) is old


# --------------------------------------------------------------------------
class TestTallyVouchers:
    def _journal(self, credit=100.0, vtype="Debit Note"):
        return AccountingVoucherPayload(
            company_name="Co", voucher_type=vtype, voucher_date="2026-10-20",
            voucher_number="DN-1", party_ledger="Shreeji",
            entries=[
                {"ledger": "Shreeji", "debit": 118.0, "bill_ref": "INV-9"},
                {"ledger": "Purchase Account", "credit": credit},
                {"ledger": "IGST Input", "credit": 18.0, "group": "Duties & Taxes"},
            ],
        )

    def test_debit_note_xml(self):
        xml = build_accounting_voucher_xml(self._journal())
        assert 'VCHTYPE="Debit Note"' in xml
        assert "<LEDGERNAME>Shreeji</LEDGERNAME>" in xml
        assert "<AMOUNT>-118.00</AMOUNT>" in xml and "<AMOUNT>100.00</AMOUNT>" in xml
        assert "<NAME>INV-9</NAME>" in xml

    def test_unbalanced_voucher_is_refused(self):
        with pytest.raises(TallyError, match="does not balance"):
            build_accounting_voucher_xml(self._journal(credit=90.0))
        assert check_balanced([{"ledger": "A", "debit": 1, "credit": 1}, {"ledger": "B", "credit": 1}])

    @pytest.mark.parametrize("vtype", ["Journal", "Contra", "Credit Note"])
    def test_all_types_post_on_mock(self, vtype):
        payload = self._journal(vtype=vtype)
        connector = MockTallyConnector()
        connector.ensure_masters(payload)
        assert connector.preflight(payload) == []
        assert connector.post_voucher(payload).success

    def test_bill_reference_uses_NAME_tag(self):
        ledger = LedgerVoucherPayload(
            company_name="Co", voucher_type="Payment", direction="payment",
            bank_ledger="HDFC Bank", party_ledger="Shreeji", amount=100,
            voucher_date="2026-10-20", bill_allocations=[{"invoice_number": "INV-9", "amount": 100}])
        assert "<NAME>INV-9</NAME>" in build_ledger_voucher_xml(ledger)
        sales = VoucherPayload(
            company_name="Co", voucher_type="Sales", supplier_ledger="Cust",
            purchase_ledger="Sales Account", invoice_number="S-1", invoice_date="2026-10-20",
            grand_total=100, transaction_kind="sales")
        xml = build_sales_voucher_xml(sales)
        assert "<NAME>S-1</NAME>" in xml and "<n>" not in xml

    def test_required_masters_groups(self):
        payload = VoucherPayload(
            company_name="Co", voucher_type="Purchase", supplier_ledger="New Supplier",
            purchase_ledger="Purchase Account", invoice_number="1", invoice_date="2026-10-20",
            tax_lines=[{"ledger": "IGST Input", "amount": 18},
                       {"ledger": "Freight Inward", "amount": 40, "group": "Direct Expenses"}],
            items=[{"stock_item": "Widget", "unit": "NOS"}])
        needs = required_masters(payload)
        groups = {l["name"]: l["group"] for l in needs["ledgers"]}
        assert groups["New Supplier"] == "Sundry Creditors"
        assert groups["Freight Inward"] == "Direct Expenses"
        assert groups["IGST Input"] == "Duties & Taxes"
        assert needs["units"] == ["NOS"]
        connector = MockTallyConnector()
        result = connector.ensure_masters(payload)
        assert "Unit 'NOS'" in result["created"] and connector.preflight(payload) == []


# --------------------------------------------------------------------------
class TestApiManager:
    def test_usage_recorded_and_limit_blocks_central(self, session, company):
        from database.database import set_setting
        from services import api_manager

        set_setting(session, company.id, "gemini_key_mode", "central")
        set_setting(session, company.id, "api_monthly_limit", "2")
        for _ in range(2):
            api_manager.record_usage(session, company.id, {
                "provider": "universal", "model": "m", "key_mode": "central",
                "pages": 1, "prompt_tokens": 1000, "output_tokens": 100})
        summary = api_manager.usage_summary(session, company.id, api_manager.month_start())
        assert summary.calls == 2 and summary.remaining == 0 and summary.cost_inr > 0
        config = api_manager.universal_config(session, company)
        assert "limit" in config["blocked_reason"]

    def test_own_key_mode_is_never_limited(self, session, company):
        from database.database import set_setting
        from services import api_manager

        api_manager.save_own_key(session, company.id, "AIza-own-key")
        set_setting(session, company.id, "gemini_key_mode", "customer")
        set_setting(session, company.id, "api_monthly_limit", "1")
        api_manager.record_usage(session, company.id, {"key_mode": "customer", "pages": 1})
        config = api_manager.universal_config(session, company)
        assert config["api_key"] == "AIza-own-key" and not config["blocked_reason"]
        assert config["transport"] == "direct"

    def test_other_companies_never_see_a_key(self, session, company, other_company):
        from services import api_manager

        api_manager.save_own_key(session, company.id, "AIza-company-one")
        assert api_manager.own_key(session, other_company.id) == ""
