"""Tests for arithmetic reconciliation and GST rules."""
import pytest

from services.invoice_validator import (
    InvoiceValidator,
    detect_tax_scope,
    state_code_from_gstin,
)


def line(**overrides):
    base = {
        "raw_name": "Test Item", "quantity": 10, "unit": "PCS", "rate": 100.0,
        "discount": 0.0, "taxable_value": 1000.0, "gst_rate": 18.0,
        "cgst": 90.0, "sgst": 90.0, "igst": 0.0, "cess": 0.0,
        "total": 1180.0, "confidence": 0.95,
    }
    base.update(overrides)
    return base


def invoice(items=None, **overrides):
    base = {
        "supplier": {"name": "Test Supplier", "gstin": "23ABCDE1234F1Z5", "address": ""},
        "invoice": {"invoice_number": "INV-1", "invoice_date": "2026-09-04", "due_date": ""},
        "items": items if items is not None else [line()],
        "subtotal": 1000.0, "discount_total": 0.0, "tax_total": 180.0,
        "cess_total": 0.0, "round_off": 0.0, "grand_total": 1180.0,
        "overall_confidence": 0.95,
    }
    base.update(overrides)
    return base


class TestCleanInvoice:
    def test_no_issues(self):
        report = InvoiceValidator().validate(invoice(), buyer_gstin="23ZZZZZ9999Z1Z9")
        assert not report.is_blocking
        assert report.calculated_total == 1180.0
        assert report.tax_scope == "INTRA-STATE"


class TestArithmetic:
    def test_taxable_mismatch_is_blocking(self):
        report = InvoiceValidator().validate(invoice([line(taxable_value=900.0)]))
        assert report.is_blocking
        assert any("does not reconcile" in i.message for i in report.errors)

    def test_grand_total_mismatch_is_reported_not_corrected(self):
        data = invoice(grand_total=12450.0)
        report = InvoiceValidator().validate(data)
        assert report.is_blocking
        assert data["grand_total"] == 12450.0  # untouched
        assert any("Invoice total mismatch" in i.message for i in report.errors)

    def test_small_rounding_is_tolerated(self):
        report = InvoiceValidator(tolerance=1.0).validate(invoice(grand_total=1180.40))
        assert not report.is_blocking

    def test_discount_applied_to_taxable(self):
        report = InvoiceValidator().validate(
            invoice(
                [line(discount=100.0, taxable_value=900.0, cgst=81.0, sgst=81.0, total=1062.0)],
                subtotal=900.0, tax_total=162.0, grand_total=1062.0,
            )
        )
        assert not report.is_blocking

    def test_discount_larger_than_line_is_blocking(self):
        report = InvoiceValidator().validate(invoice([line(discount=5000.0)]))
        assert report.is_blocking

    def test_zero_quantity_is_blocking(self):
        report = InvoiceValidator().validate(invoice([line(quantity=0)]))
        assert report.is_blocking


class TestGST:
    def test_cgst_and_igst_together_is_blocking(self):
        report = InvoiceValidator().validate(invoice([line(igst=180.0)]))
        assert report.is_blocking

    def test_tax_not_matching_rate_warns(self):
        report = InvoiceValidator().validate(
            invoice([line(cgst=50.0, sgst=50.0, total=1100.0)], tax_total=100.0, grand_total=1100.0)
        )
        assert any("does not match" in i.message for i in report.warnings)

    def test_unequal_cgst_sgst_warns(self):
        report = InvoiceValidator().validate(
            invoice([line(cgst=120.0, sgst=60.0)])
        )
        assert any("should normally be equal" in i.message for i in report.warnings)


class TestTaxScope:
    def test_state_code(self):
        assert state_code_from_gstin("23ABCDE1234F1Z5") == "23"
        assert state_code_from_gstin("") == ""

    def test_same_state_is_intra(self):
        assert detect_tax_scope("23ABCDE1234F1Z5", "23XYZAB1234C1Z2", []) == "INTRA-STATE"

    def test_different_state_is_inter(self):
        assert detect_tax_scope("23ABCDE1234F1Z5", "27XYZAB1234C1Z2", []) == "INTER-STATE"

    def test_falls_back_to_printed_columns(self):
        assert detect_tax_scope("", "", [{"cgst": 90, "sgst": 90}]) == "INTRA-STATE"
        assert detect_tax_scope("", "", [{"igst": 180}]) == "INTER-STATE"

    def test_never_guesses_when_ambiguous(self):
        assert detect_tax_scope("", "", [{"cgst": 0, "sgst": 0, "igst": 0}]) == "UNKNOWN"


class TestConfidence:
    def test_low_overall_confidence_is_blocking(self):
        report = InvoiceValidator().validate(invoice(overall_confidence=0.55))
        assert report.is_blocking

    def test_medium_confidence_warns_only(self):
        report = InvoiceValidator().validate(invoice(overall_confidence=0.80))
        assert not report.is_blocking
        assert report.warnings

    def test_low_line_confidence_flags_that_line(self):
        report = InvoiceValidator().validate(invoice([line(confidence=0.40)]))
        assert 0 in report.line_flags


class TestMissingHeader:
    def test_missing_supplier_and_number_are_blocking(self):
        data = invoice()
        data["supplier"]["name"] = ""
        data["invoice"]["invoice_number"] = ""
        report = InvoiceValidator().validate(data)
        assert len(report.errors) >= 2

    def test_malformed_gstin_warns(self):
        data = invoice()
        data["supplier"]["gstin"] = "NOTAGSTIN"
        report = InvoiceValidator().validate(data)
        assert any("GSTIN" in i.message for i in report.warnings)

    def test_empty_invoice_is_blocking(self):
        report = InvoiceValidator().validate(invoice(items=[]))
        assert report.is_blocking


class TestHumanVerification:
    """A confidence score is the AI's uncertainty. The review screen exists to
    settle it, so a line a person has checked must stop blocking — while real
    arithmetic problems must keep blocking regardless."""

    def test_low_confidence_blocks_before_verification(self):
        report = InvoiceValidator().validate(invoice([line(confidence=0.64)]))
        assert report.is_blocking
        assert any("Save line" in i.message for i in report.errors)

    def test_verified_line_no_longer_blocks(self):
        report = InvoiceValidator().validate(
            invoice([line(confidence=0.64, verified=True)])
        )
        assert not report.is_blocking

    def test_verification_is_recorded_for_the_reader(self):
        report = InvoiceValidator().validate(
            invoice([line(confidence=0.64, verified=True)])
        )
        notes = [i.message for i in report.issues if i.severity == "info"]
        assert any("checked and saved" in m for m in notes)

    def test_verification_does_not_excuse_bad_arithmetic(self):
        report = InvoiceValidator().validate(
            invoice([line(confidence=0.64, verified=True, taxable_value=900.0)])
        )
        assert report.is_blocking
        assert any("does not reconcile" in i.message for i in report.errors)

    def test_verification_does_not_excuse_a_wrong_grand_total(self):
        report = InvoiceValidator().validate(
            invoice([line(confidence=0.64, verified=True)], grand_total=99999.0)
        )
        assert report.is_blocking

    def test_verification_does_not_excuse_conflicting_gst(self):
        report = InvoiceValidator().validate(
            invoice([line(confidence=0.64, verified=True, igst=180.0)])
        )
        assert report.is_blocking

    def test_unverified_high_confidence_line_is_clean(self):
        report = InvoiceValidator().validate(invoice([line(confidence=0.97)]))
        assert not report.is_blocking


class TestInvoiceLevelConfidence:
    """Correcting the one doubtful line must clear the invoice-level block.

    Requiring every line to be re-saved is busywork on lines that were read
    correctly, and it teaches people to press Save without reading.
    """

    def test_invoice_blocks_while_a_line_is_doubtful(self):
        report = InvoiceValidator().validate(
            invoice([line(confidence=0.58), line(confidence=0.96)], overall_confidence=0.58)
        )
        assert report.is_blocking

    def test_verifying_only_the_bad_line_clears_it(self, session, company, user):
        from services.purchase_service import recalculate_totals
        from database.models import Invoice, InvoiceItem

        stored = Invoice(company_id=company.id, supplier_name="S", status="Review Required",
                         overall_confidence=0.58)
        session.add(stored)
        session.commit()
        session.add_all([
            InvoiceItem(company_id=company.id, invoice_id=stored.id, raw_name="Bad line", quantity=1, rate=10,
                        taxable_value=10, total=10, confidence=0.58),
            InvoiceItem(company_id=company.id, invoice_id=stored.id, raw_name="Good line", quantity=1, rate=10,
                        taxable_value=10, total=10, confidence=0.96),
            InvoiceItem(company_id=company.id, invoice_id=stored.id, raw_name="Also good", quantity=1, rate=10,
                        taxable_value=10, total=10, confidence=0.94),
        ])
        session.commit()

        items = session.query(InvoiceItem).filter(
            InvoiceItem.invoice_id == stored.id
        ).all()
        bad = next(i for i in items if i.raw_name == "Bad line")
        bad.edited_by_user = True
        bad.confidence = 1.0
        session.commit()

        recalculate_totals(session, stored)
        # the weakest remaining line is 0.94, comfortably above the threshold
        assert stored.overall_confidence == pytest.approx(0.94)

    def test_a_second_doubtful_line_keeps_it_blocked(self, session, company, user):
        from services.purchase_service import recalculate_totals
        from database.models import Invoice, InvoiceItem

        stored = Invoice(company_id=company.id, supplier_name="S", status="Review Required",
                         overall_confidence=0.58)
        session.add(stored)
        session.commit()
        session.add_all([
            InvoiceItem(company_id=company.id, invoice_id=stored.id, raw_name="Bad one", quantity=1, rate=10,
                        taxable_value=10, total=10, confidence=0.58),
            InvoiceItem(company_id=company.id, invoice_id=stored.id, raw_name="Bad two", quantity=1, rate=10,
                        taxable_value=10, total=10, confidence=0.61),
        ])
        session.commit()

        items = session.query(InvoiceItem).filter(
            InvoiceItem.invoice_id == stored.id
        ).all()
        items[0].edited_by_user = True
        items[0].confidence = 1.0
        session.commit()

        recalculate_totals(session, stored)
        assert stored.overall_confidence == pytest.approx(0.61)
        assert stored.overall_confidence < 0.70     # still blocked, correctly
