"""Every third-party import must be declared in requirements.txt.

This exists because openpyxl shipped undeclared: it was present in the
development environment, so the Excel export and the licence-key generator both
worked here and both failed on a customer's machine with
`ModuleNotFoundError: No module named 'openpyxl'`.

A missing declaration is invisible until someone installs from scratch, which
is exactly when it is most expensive to discover.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Modules that ship with Python, or are ours. Everything else needs declaring.
STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {
    "__future__", "typing_extensions",
}
OURS = {"app", "app_pages", "database", "services", "utils", "tests",
        "seed_demo", "generate_keys", "setup_secret", "make_customer_build",
        "demo_end_to_end", "conftest", "check_deps",
        # ocr_proxy/ has its own separate requirements.txt (see python_files()
        # below) - license_check is its module, imported via sys.path by
        # tests/test_proxy_license_check.py to cross-check it against
        # services/license_service.py.
        "license_check"}

# import name -> pip name, where they differ
PIP_NAME = {
    "PIL": "pillow",
    "cv2": "opencv-python-headless",
    "fitz": "pymupdf",
    "pymupdf": "pymupdf",
    "sqlalchemy": "sqlalchemy",
    "dateutil": "python-dateutil",
    "yaml": "pyyaml",
    "pyzbar": "pyzbar",
    "pytesseract": "pytesseract",
    "rapidocr": "rapidocr",
    "paddleocr": "paddleocr",
    "streamlit": "streamlit",
    "openpyxl": "openpyxl",
    "pandas": "pandas",
    "requests": "requests",
    "cryptography": "cryptography",
    "rapidfuzz": "rapidfuzz",
    "pytest": "pytest",
}

# Declared optional in requirements.txt (commented out) and imported lazily
# inside a try/except, so the app runs without them. rapidocr (default
# engine) and pytesseract (ready fallback) are NOT in this set — both are
# active, uncommented lines in requirements.txt like openpyxl/pandas.
OPTIONAL = {"paddleocr", "pyzbar"}


def python_files():
    for path in ROOT.rglob("*.py"):
        parts = set(path.parts)
        # ocr_proxy/ is a separate standalone service the vendor deploys on
        # their own server (never installed alongside this app, never
        # shipped to a customer - see ocr_proxy/README.md) with its OWN
        # requirements.txt; tests/test_proxy_license_check.py covers it
        # instead of this scan.
        if parts & {".venv", "venv", "__pycache__", "dist", "build", "data", "ocr_proxy"}:
            continue
        yield path


def top_level_imports(path: Path) -> set:
    """Every module imported by this file, at any nesting level."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:                      # not ours to police
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def declared_packages() -> set:
    """Package names from requirements.txt, including commented-out optionals."""
    declared = set()
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip().lstrip("#").strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if match:
            declared.add(match.group(1).lower().replace("_", "-"))
    return declared


class TestDeclaredDependencies:
    def test_requirements_file_exists(self):
        assert (ROOT / "requirements.txt").exists()

    def test_every_third_party_import_is_declared(self):
        declared = declared_packages()
        missing = {}
        for path in python_files():
            for name in top_level_imports(path):
                if name in STDLIB or name in OURS:
                    continue
                pip_name = PIP_NAME.get(name, name).lower().replace("_", "-")
                if pip_name not in declared:
                    missing.setdefault(pip_name, []).append(
                        str(path.relative_to(ROOT))
                    )
        assert not missing, (
            "these imports are not in requirements.txt, so a fresh install "
            f"will fail: { {k: v[:3] for k, v in missing.items()} }"
        )

    @pytest.mark.parametrize("package", ["openpyxl", "streamlit", "pandas",
                                         "sqlalchemy", "pillow"])
    def test_core_packages_are_declared(self, package):
        assert package in declared_packages()

    def test_optional_engines_stay_optional(self):
        """PaddleOCR/pyzbar are heavy and not everyone needs them; they must
        remain commented out so a default install stays small. (pytesseract
        is intentionally NOT in this set — see OPTIONAL above.)"""
        active = []
        for line in (ROOT / "requirements.txt").read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            name = re.match(r"^([A-Za-z0-9_.\-]+)", stripped)
            if name and name.group(1).lower() in OPTIONAL:
                active.append(name.group(1))
        assert not active, f"these should stay optional: {active}"


class TestImportsResolve:
    """Everything declared as required must actually import here."""

    @pytest.mark.parametrize("module", ["openpyxl", "streamlit", "pandas",
                                        "sqlalchemy", "PIL", "requests",
                                        "cryptography", "rapidfuzz"])
    def test_module_imports(self, module):
        __import__(module)


class TestMissingPackageMessage:
    """A missing package must produce an instruction, not a stack trace."""

    def test_router_handles_module_not_found_separately(self):
        source = (ROOT / "app.py").read_text()
        assert "except ModuleNotFoundError" in source
        index_specific = source.index("except ModuleNotFoundError")
        index_general = source.index("except Exception as exc:  # a page must never")
        assert index_specific < index_general, (
            "ModuleNotFoundError must be caught before the general handler, "
            "or it never fires"
        )

    def test_message_names_the_missing_package_and_the_fix(self):
        source = (ROOT / "app.py").read_text()
        assert "pip install -r requirements.txt" in source
        assert "exc.name" in source, "the message should name the package"


class TestSelfHealingLaunchers:
    """START.bat must install a package that a later version added.

    The old logic wrote a `.venv\\.installed` marker on first run and skipped
    the install step ever after. When openpyxl was added, every existing
    install kept that marker, never installed it, and the Excel export failed.
    """

    def test_check_deps_script_exists(self):
        assert (ROOT / "check_deps.py").exists()

    def test_no_launcher_relies_on_a_one_time_marker(self):
        offenders = []
        for bat in sorted(ROOT.glob("*.bat")):
            text = bat.read_text(encoding="utf-8", errors="ignore")
            if ".installed" in text:
                offenders.append(bat.name)
        assert not offenders, (
            "a one-time marker skips installing packages added later: "
            + ", ".join(offenders)
        )

    def test_start_bat_checks_dependencies_every_launch(self):
        text = (ROOT / "START.bat").read_text(encoding="utf-8", errors="ignore")
        assert "check_deps.py" in text
        assert "pip install -r requirements.txt" in text

    def test_run_launchers_repair_too(self):
        """RUN.bat skips setup, so it needs its own repair step."""
        for name in ("RUN.bat", "RUN_ON_NETWORK.bat"):
            text = (ROOT / name).read_text(encoding="utf-8", errors="ignore")
            assert "check_deps.py" in text, f"{name} cannot repair a missing package"

    def test_post_install_check_ignores_the_stamp(self):
        """Right after installing, the stamp is not written yet — checking it
        there would report failure on a perfectly good install."""
        for name in ("START.bat", "INSTALL.bat"):
            text = (ROOT / name).read_text(encoding="utf-8", errors="ignore")
            assert "check_deps.py --verify" in text, (
                f"{name} should verify with --verify after installing"
            )


class TestCheckDeps:
    def test_reads_only_uncommented_requirements(self):
        import check_deps

        names = [n.lower() for n in check_deps.required_packages()]
        assert "openpyxl" in names
        assert "rapidocr" in names                 # default OCR engine, always installed
        # rapidocr does NOT declare onnxruntime as its own dependency (confirmed
        # against PyPI metadata) — omitting it here is exactly the bug a real
        # customer install hit: "onnxruntime is not installed" on Test this engine.
        assert "onnxruntime" in names
        assert "pytesseract" in names              # ready fallback, always installed
        assert "paddleocr" not in names            # still commented out, optional

    def test_maps_odd_import_names(self):
        import check_deps

        assert check_deps.import_name("Pillow") == "PIL"
        assert check_deps.import_name("opencv-python-headless") == "cv2"
        assert check_deps.import_name("openpyxl") == "openpyxl"

    def test_reports_nothing_missing_here(self):
        import check_deps

        assert check_deps.missing_packages() == []

    def test_detects_a_missing_package(self, monkeypatch):
        import importlib.util

        import check_deps

        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name, *a, **k: None if name == "openpyxl" else real(name, *a, **k),
        )
        assert "openpyxl" in check_deps.missing_packages()

    def test_fingerprint_changes_with_requirements(self, monkeypatch, tmp_path):
        import check_deps

        original = check_deps.requirements_fingerprint()
        fake = tmp_path / "requirements.txt"
        fake.write_text("streamlit>=1.50\nopenpyxl>=3.1\n")
        monkeypatch.setattr(check_deps, "REQUIREMENTS", fake)
        assert check_deps.requirements_fingerprint() != original
