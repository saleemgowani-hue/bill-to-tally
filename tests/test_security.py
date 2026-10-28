"""Tests for password hashing, secret storage and upload validation."""
import pytest

from utils.security import (
    detect_file_type,
    encrypt_secret,
    decrypt_secret,
    hash_password,
    mask_secret,
    password_problems,
    safe_filename,
    validate_upload,
    verify_password,
)


class TestPasswords:
    def test_hash_is_not_plaintext(self):
        stored = hash_password("Passw0rd123")
        assert "Passw0rd123" not in stored
        assert stored.startswith("pbkdf2_sha256$")

    def test_verify_round_trip(self):
        stored = hash_password("Passw0rd123")
        assert verify_password("Passw0rd123", stored)
        assert not verify_password("wrong", stored)

    def test_salt_is_random(self):
        assert hash_password("same") != hash_password("same")

    def test_empty_password_rejected(self):
        with pytest.raises(ValueError):
            hash_password("")

    def test_policy(self):
        assert password_problems("Passw0rd123") == []
        assert password_problems("short") != []
        assert "at least one number" in password_problems("abcdefghij")


class TestSecrets:
    def test_round_trip(self):
        token = encrypt_secret("sk-secret-key-value")
        assert "sk-secret-key-value" not in token
        assert decrypt_secret(token) == "sk-secret-key-value"

    def test_empty(self):
        assert decrypt_secret("") == ""

    def test_mask(self):
        assert mask_secret("abcdefghij").endswith("ghij")
        assert "abcdef" not in mask_secret("abcdefghij")


class TestUploads:
    JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
    PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    PDF = b"%PDF-1.7" + b"0" * 100

    def test_valid_files(self):
        assert validate_upload("bill.jpg", self.JPEG)[0]
        assert validate_upload("bill.png", self.PNG)[0]
        assert validate_upload("bill.pdf", self.PDF)[0]

    def test_bad_extension(self):
        ok, message = validate_upload("script.exe", self.JPEG)
        assert not ok and "not allowed" in message

    def test_extension_content_mismatch(self):
        ok, message = validate_upload("fake.png", self.JPEG)
        assert not ok

    def test_oversized(self):
        big = self.JPEG + b"0" * (20 * 1024 * 1024)
        ok, message = validate_upload("big.jpg", big)
        assert not ok and "limit" in message

    def test_empty_file(self):
        assert not validate_upload("bill.jpg", b"")[0]

    def test_magic_detection(self):
        assert detect_file_type(self.PDF) == "pdf"
        assert detect_file_type(b"random bytes") is None

    def test_path_traversal_is_stripped(self):
        assert safe_filename("../../etc/passwd") == "passwd"
        assert "/" not in safe_filename("a/b/c.jpg")
