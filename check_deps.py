"""
Are the required packages installed and up to date?

    python check_deps.py            # exit 0 = fine, 1 = something to install
    python check_deps.py --list     # print what is missing and why

START.bat runs this on every launch. It replaced a `.venv\\.installed` marker
file, which only recorded that *an* install had happened once. When a later
version added a package, the marker still existed, the install step was
skipped, and the new feature failed on the customer's machine with
`ModuleNotFoundError` — which is exactly what happened with openpyxl.

Two things are checked:

  * every required package can be found, and
  * requirements.txt has not changed since the last successful install.

The second catches a version bump, which a presence check alone would miss.
Nothing is imported — `find_spec` only looks the package up, so this adds a
few milliseconds to startup rather than a few seconds.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = ROOT / ".venv" / ".requirements-hash"

# pip name -> the name you import it by, where they differ
IMPORT_NAME = {
    "pillow": "PIL",
    "opencv-python-headless": "cv2",
    "opencv-python": "cv2",
    "pymupdf": "pymupdf",
    "sqlalchemy": "sqlalchemy",
    "python-dateutil": "dateutil",
    "pyyaml": "yaml",
    "psycopg2-binary": "psycopg2",
}


def required_packages() -> list:
    """Uncommented entries in requirements.txt, names only."""
    if not REQUIREMENTS.exists():
        return []
    names = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if match:
            names.append(match.group(1))
    return names


def import_name(package: str) -> str:
    return IMPORT_NAME.get(package.lower(), package.lower().replace("-", "_"))


def missing_packages() -> list:
    missing = []
    for package in required_packages():
        try:
            found = importlib.util.find_spec(import_name(package))
        except (ImportError, ValueError):
            found = None
        if found is None:
            missing.append(package)
    return missing


def requirements_fingerprint() -> str:
    if not REQUIREMENTS.exists():
        return ""
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()[:16]


def stamp_matches() -> bool:
    if not STAMP.exists():
        return False
    return STAMP.read_text(encoding="utf-8").strip() == requirements_fingerprint()


def write_stamp() -> None:
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(requirements_fingerprint(), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check installed packages.")
    parser.add_argument("--list", action="store_true", help="explain what is missing")
    parser.add_argument(
        "--stamp", action="store_true",
        help="record that requirements.txt is now installed",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="check packages only, ignoring the stamp (used right after installing)",
    )
    args = parser.parse_args()

    if args.stamp:
        write_stamp()
        return 0

    missing = missing_packages()
    # After a fresh install the packages are present but the stamp has not been
    # written yet, so a stamp check would report failure on a perfectly good
    # install. --verify asks the only question that matters at that moment:
    # is anything still missing?
    changed = False if args.verify else not stamp_matches()

    if args.list:
        if missing:
            print("Missing: " + ", ".join(missing))
        if changed:
            print("requirements.txt has changed since the last install.")
        if not missing and not changed:
            print("All required packages are installed.")

    return 1 if (missing or changed) else 0


if __name__ == "__main__":
    sys.exit(main())
