"""
Build a customer package — VENDOR TOOL.

    python make_customer_build.py "Novelty Store"

Produces  dist/BillToTallyAgent_<Customer>.zip  which the customer extracts and
runs. They never open a text file, never type a secret, never see a setting.
Extract, double-click START.bat, sign up with the key you gave them. That is it.

What it does:
  * copies the application
  * leaves out everything that could mint keys (generate_keys.py,
    GENERATE_KEYS.bat, your key register and issue log, vendor docs)
  * writes a ready .env carrying your licence signing secret, so the app
    validates your keys straight away
  * drops in a short READ_ME_FIRST.txt for the customer

Use --secret to give this customer their own secret instead of yours. Keys for
them must then be minted with that same value — see docs/VENDOR_SETUP.md.
"""
import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

# Anything here would let the holder create their own licence keys, or exposes
# your commercial records. It must never reach a customer.
EXCLUDE_FILES = {
    "generate_keys.py",
    "GENERATE_KEYS.bat",
    "make_customer_build.py",
    "SN_Softech_Licence_Keys.xlsx",
    "keys_issued_log.csv",
    ".env",
    # A customer has no demo to reset, and this is a file they could click out
    # of curiosity. Doing so would create a second company called "SN Demo
    # Traders" in their install, with a password that is printed in the docs —
    # confusing at best, and somewhere real bills could be typed by mistake.
    "RESET_DEMO.bat",
    "demo_end_to_end.py",
}
EXCLUDE_DIRS = {"data", "backups", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", ".git"}
EXCLUDE_DOCS = {"VENDOR_SETUP.md", "LICENSING.md"}

READ_ME = """AI BILL-TO-TALLY STOCK UPDATE AGENT
{brand} - support {support}

Prepared for: {customer}

TO START
  1. Double-click  START.bat
  2. Wait - the first run installs what it needs (2 to 5 minutes)
  3. The app opens in your web browser
  4. Sign up with the licence key supplied with this software

If Windows says Python is not found, install Python 3.10 or newer from
python.org and TICK "Add Python to PATH" on the first installer screen.

EVERY DAY
  START.bat        start the software
  BACKUP.bat       copy your data to the backups folder - run this daily

Keep the black window open while you use the app. Closing it stops the app.

Your licence key activates one company. Keep it somewhere safe.

Questions: {brand}, {support}
"""


def vendor_secret() -> str:
    env = ROOT / ".env"
    if not env.exists():
        return ""
    match = re.search(r"^LICENSE_SIGNING_SECRET=(.*)$", env.read_text(encoding="utf-8"), re.M)
    return match.group(1).strip() if match else ""


def build_env(secret: str) -> str:
    """The customer's .env: the example with the secret already filled in."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    text = re.sub(
        r"^LICENSE_SIGNING_SECRET=.*$",
        f"LICENSE_SIGNING_SECRET={secret}",
        text,
        flags=re.M,
    )
    header = (
        "# Configured by SN Softech Solutions. Do not edit unless asked to.\n"
        "# Changing LICENSE_SIGNING_SECRET will stop your licence key working.\n\n"
    )
    return header + text


def copy_tree(destination: Path) -> None:
    for source in ROOT.rglob("*"):
        relative = source.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in relative.parts):
            continue
        if source.is_dir():
            continue
        if relative.name in EXCLUDE_FILES:
            continue
        if relative.parts[:1] == ("docs",) and relative.name in EXCLUDE_DOCS:
            continue
        if source.suffix in (".pyc", ".pyo"):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a customer package.")
    parser.add_argument("customer", help="customer name, used in the file name")
    parser.add_argument(
        "--secret",
        help="secret for this customer (default: yours, from .env)",
    )
    args = parser.parse_args()

    secret = args.secret or vendor_secret()
    if not secret:
        print("No licence signing secret found.")
        print("Run `python setup_secret.py` first, or pass --secret.")
        return 1
    if len(secret) < 16:
        print("That secret is too short to be safe (16 characters minimum).")
        return 1

    slug = re.sub(r"[^A-Za-z0-9]+", "_", args.customer).strip("_") or "Customer"
    staging = DIST / f"BillToTallyAgent_{slug}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    copy_tree(staging)
    (staging / ".env").write_text(build_env(secret), encoding="utf-8")

    from utils.config import settings

    (staging / "READ_ME_FIRST.txt").write_text(
        READ_ME.format(
            brand=settings.COMPANY_BRAND,
            support=settings.SUPPORT_CONTACT,
            customer=args.customer,
        ).replace("\n", "\r\n"),
        encoding="utf-8",
    )

    archive = DIST / f"BillToTallyAgent_{slug}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(staging.parent))

    # Safety net: prove nothing that mints keys slipped through.
    with zipfile.ZipFile(archive) as bundle:
        names = {Path(n).name for n in bundle.namelist()}
    leaked = names & EXCLUDE_FILES - {".env"}
    if leaked:
        print(f"REFUSING: these should not be in a customer build: {sorted(leaked)}")
        archive.unlink()
        return 1

    size = archive.stat().st_size / 1024
    print(f"Built  {archive}  ({size:,.0f} KB)")
    print(f"Secret embedded: {secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}")
    print()
    print("The customer extracts it and double-clicks START.bat. Nothing to edit.")
    print("Give them one key from your register when you hand it over.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
