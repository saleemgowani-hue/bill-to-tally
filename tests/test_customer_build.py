"""
Guards make_customer_build.py's core promise: a customer build has no demo
account, no vendor key-minting tools, and still requires a licence key at
Sign Up. Runs the real build tool against a temp secret and inspects the zip
it produces — if a future edit to a launcher's wording breaks a text patch,
or a new vendor-only file isn't excluded, this fails loudly instead of
shipping a customer a demo password.
"""
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Anything that would leak the demo login, seed a bypass account, or hand the
# holder a way to mint their own licence keys.
FORBIDDEN_FILENAMES = {
    "seed_demo.py", "test_seed.py", "generate_keys.py", "GENERATE_KEYS.bat",
    "SN_Softech_Licence_Keys.xlsx", "keys_issued_log.csv", "RESET_DEMO.bat",
    "demo_end_to_end.py", "README.md",
}
FORBIDDEN_STRINGS = (b"demo@snsoftech.local", b"Demo@1234")
# Only checked in files a customer would actually run/read.
LAUNCHER_AND_DOC_SUFFIXES = (".bat", ".txt", ".md")


@pytest.fixture(scope="module")
def customer_zip(tmp_path_factory):
    build_dir = tmp_path_factory.mktemp("customer_build")
    secret = "x" * 32
    result = subprocess.run(
        [sys.executable, "make_customer_build.py", "Pytest Customer",
         "--secret", secret],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archive = ROOT / "dist" / "BillToTallyAgent_Pytest_Customer.zip"
    assert archive.exists()
    dest = build_dir / archive.name
    dest.write_bytes(archive.read_bytes())
    archive.unlink()
    shutil_staging = ROOT / "dist" / "BillToTallyAgent_Pytest_Customer"
    if shutil_staging.exists():
        import shutil
        shutil.rmtree(shutil_staging)
    return dest


def test_forbidden_files_absent(customer_zip):
    with zipfile.ZipFile(customer_zip) as bundle:
        names = {Path(n).name for n in bundle.namelist()}
    leaked = names & FORBIDDEN_FILENAMES
    assert not leaked, f"customer build must not contain: {sorted(leaked)}"


def test_no_demo_credentials_in_launchers_or_docs(customer_zip):
    with zipfile.ZipFile(customer_zip) as bundle:
        for name in bundle.namelist():
            if not name.endswith(LAUNCHER_AND_DOC_SUFFIXES):
                continue
            data = bundle.read(name)
            for needle in FORBIDDEN_STRINGS:
                assert needle not in data, f"{needle!r} found in {name}"


def test_first_run_scripts_reference_init_customer_db_not_seed_demo(customer_zip):
    with zipfile.ZipFile(customer_zip) as bundle:
        for launcher in ("START.bat", "INSTALL.bat"):
            text = bundle.read(f"BillToTallyAgent_Pytest_Customer/{launcher}").decode("utf-8")
            assert "seed_demo.py" not in text, f"{launcher} still calls seed_demo.py"
            assert "init_customer_db.py" in text, f"{launcher} should call init_customer_db.py"


def test_init_customer_db_is_present_and_creates_no_company(customer_zip):
    with zipfile.ZipFile(customer_zip) as bundle:
        names = bundle.namelist()
        assert any(n.endswith("init_customer_db.py") for n in names)
        text = bundle.read(next(n for n in names if n.endswith("init_customer_db.py"))).decode("utf-8")
    assert "create_company_with_admin" not in text
    assert "license_service.generate_key" not in text


def test_signup_still_requires_a_licence_key(customer_zip):
    """app.py itself is unmodified by the build — signup must still gate on
    a licence key so removing the demo account doesn't leave a back door."""
    with zipfile.ZipFile(customer_zip) as bundle:
        text = bundle.read("BillToTallyAgent_Pytest_Customer/app.py").decode("utf-8")
    assert "license_key" in text
    assert "license_service.check_available" in text


def test_desktop_shortcut_script_ships_and_is_sound(customer_zip):
    with zipfile.ZipFile(customer_zip) as bundle:
        raw = bundle.read("BillToTallyAgent_Pytest_Customer/CREATE_DESKTOP_SHORTCUT.bat")
        readme = bundle.read("BillToTallyAgent_Pytest_Customer/READ_ME_FIRST.txt").decode("utf-8")
    text = raw.decode("ascii")  # must stay plain ASCII for cmd.exe
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert "WScript.Shell" in text and "WorkingDirectory" in text
    assert "RUN.bat" in text and "START.bat" in text
    assert "CREATE_DESKTOP_SHORTCUT.bat" in readme
