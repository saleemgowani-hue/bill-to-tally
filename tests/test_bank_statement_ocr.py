"""Tests for services.bank_statement_parser (photo/PDF bank statement OCR)."""
import pytest
from PIL import Image

from services.bank_statement_import import make_row_hash
from services.bank_statement_parser import (
    BankStatementParseError,
    BankStatementParser,
    _to_rows,
)
from services.ocr_service import MockOCRProvider, TesseractOCRProvider


def _img():
    return Image.new("RGB", (10, 10))


def test_mock_provider_returns_bank_rows_not_invoice_shape():
    parser = BankStatementParser(MockOCRProvider())
    result = parser.parse_images([_img()])
    assert len(result.rows) == 3
    assert result.rows[0].debit == 12600.0
    assert result.rows[1].credit == 3200.5
    assert result.warnings


def test_text_only_provider_raises_clear_error():
    parser = BankStatementParser(TesseractOCRProvider())
    with pytest.raises(BankStatementParseError):
        parser.parse_images([_img()])


def test_no_images_raises():
    parser = BankStatementParser(MockOCRProvider())
    with pytest.raises(BankStatementParseError):
        parser.parse_images([])


def test_to_rows_skips_zero_amount_and_blank_rows():
    data = {
        "transactions": [
            {"date": "2026-09-01", "narration": "Real txn", "debit": 100, "credit": 0},
            {"date": "", "narration": "", "debit": 0, "credit": 0},
            {"date": "2026-09-02", "narration": "Zero amount row", "debit": 0, "credit": 0},
        ]
    }
    rows = _to_rows(data)
    assert len(rows) == 1
    assert rows[0].narration == "Real txn"


def test_to_rows_hash_matches_excel_path_hash_for_same_values():
    data = {"transactions": [
        {"date": "2026-09-01", "narration": "Same txn", "reference_no": "R1", "debit": 50, "credit": 0}
    ]}
    rows = _to_rows(data)
    expected = make_row_hash("2026-09-01", "Same txn", "R1", 50.0, 0.0)
    assert rows[0].row_hash == expected


def test_to_rows_converts_indian_date_format():
    data = {"transactions": [
        {"date": "05/09/2026", "narration": "x", "debit": 10, "credit": 0}
    ]}
    rows = _to_rows(data)
    assert rows[0].txn_date == "2026-09-05"
