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
  * leaves out seed_demo.py and swaps it for init_customer_db.py in the
    launchers, so first run creates an empty database and the customer's
    first screen is Sign Up with a licence key — no demo account, no
    printed password, no free trial key
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
    "demo_bootstrap.py",   # public-demo only; app.py imports it lazily
    "DEMO_MODE.flag",
    # This is what START.bat/INSTALL.bat call on a fresh install to seed a demo
    # company with a printed password and an auto-activated trial licence,
    # bypassing Sign Up entirely. A customer build swaps it out for
    # init_customer_db.py (see _use_customer_db_init below) so the licence-key
    # Sign Up screen is unavoidable, and this file is left out so nobody can
    # run it by hand and recreate that bypass.
    "seed_demo.py",
    # Imports seed_demo at module level, so it can't even be collected once
    # seed_demo.py is excluded above. INSTALL.bat's post-install `pytest tests`
    # check would otherwise report a false failure to the customer.
    "test_seed.py",
    # Developer-facing; assumes the reader can run seed_demo.py, RESET_DEMO.bat
    # etc. and documents the demo password. READ_ME_FIRST.txt is what a
    # customer gets instead.
    "README.md",
}
EXCLUDE_DIRS = {
    "data", "backups", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", ".git",
    # The OCR proxy is a separate service the VENDOR deploys on their own
    # server (see ocr_proxy/README.md) — it holds the real paid Gemini key
    # and must never reach a customer's machine or installer zip.
    "ocr_proxy",
}
# Vendor/demo-flavoured docs. TROUBLESHOOTING.md is excluded too even though
# parts of it are generic — the rest walks through seed_demo.py flags, the
# demo password and direct-DB fixes, and untangling that from the genuinely
# useful parts isn't worth the risk of leaving something in by accident.
# The customer has READ_ME_FIRST.txt plus phone support instead.
EXCLUDE_DOCS = {"VENDOR_SETUP.md", "LICENSING.md", "DEMO_GUIDE.md", "TROUBLESHOOTING.md"}

# Text substitutions applied to the customer copy of each launcher, so first
# run creates an empty database (real Sign Up + licence key required) instead
# of seed_demo.py's demo company with a printed password and a free trial key.
LAUNCHER_PATCHES = {
    "START.bat": [
        (
            "echo  [5/5] Preparing database and demo company...\r\n"
            "    \"%VPY%\" seed_demo.py",
            "echo  [5/5] Preparing database...\r\n"
            "    \"%VPY%\" init_customer_db.py",
        ),
        (
            "echo    Sign in : demo@snsoftech.local\r\n"
            "echo    Password: Demo@1234\r\n",
            "echo    Sign up with the licence key supplied with this software.\r\n",
        ),
    ],
    "INSTALL.bat": [
        (
            "echo  Preparing database...\r\n"
            "    \"%VPY%\" seed_demo.py",
            "echo  Preparing database...\r\n"
            "    \"%VPY%\" init_customer_db.py",
        ),
    ],
    "RUN.bat": [
        (
            "echo  Sign in: demo@snsoftech.local  /  Demo@1234\r\n",
            "echo  Sign up with the licence key supplied with this software.\r\n",
        ),
    ],
}

READ_ME = """AI BILL-TO-TALLY STOCK UPDATE AGENT
{brand} - support {support}

Prepared for: {customer}

BEFORE YOU START - one thing to install (one-time, free):

  Python 3.14
     Download: https://www.python.org/downloads/windows/
     Run the installer and TICK "Add Python to PATH" on the very
     first screen - this is the most common mistake, so don't miss it.

This app reads your bill photos itself, fully offline, with RapidOCR
(fast, the default) - INSTALL.bat below sets it up automatically, no
extra download needed. It also sets up Tesseract OCR as a backup engine
- that does download something (~80 MB, from the official Tesseract
site). If a firewall/antivirus blocks that one step, the app still works
fine on RapidOCR alone; do Tesseract by hand only if you specifically
want that backup:
     Download: https://github.com/UB-Mannheim/tesseract/wiki
     Run the 64-bit installer - the default options are fine.

TO INSTALL (one-time)
  1. Extract this zip anywhere (Desktop is fine)
  2. Double-click  INSTALL.bat
  3. Wait - it sets up the app and OCR engines (2 to 5 minutes)
  4. When it says "INSTALLATION COMPLETE", you're done

DESKTOP SHORTCUT (optional, one-time)
  Double-click  CREATE_DESKTOP_SHORTCUT.bat  - an "AI Bill-to-Tally Agent"
  icon appears on your Desktop. Use that icon from now on.
  Do NOT copy RUN.bat itself to the Desktop - it will not work there.
  If you move this folder later, run CREATE_DESKTOP_SHORTCUT.bat again.

TO RUN (every day)
  1. Double-click  RUN.bat (or the Desktop icon)
  2. The app opens in your web browser at http://localhost:8501
  3. First time: sign up with the licence key supplied with this software
  4. Keep the black window open while you use the app - closing it stops the app

EVERY DAY
  RUN.bat          start the software
  CREATE_DESKTOP_SHORTCUT.bat   one-time: puts an icon on your Desktop
  BACKUP.bat       copy your data to the backups folder - run this daily

MANIFEST TO EXCEL (transport manifests / bilties, not purchase bills)
  Upload a photo or PDF of a handwritten manifest and get back an editable
  Excel sheet, one row per consignment. You can add SEVERAL manifests at
  once (for example five bills of one company): they are read one after
  another and you still get ONE Excel file - a combined sheet with every
  consignment, a summary line per bill, and a sheet for each bill. One
  manifest alone gives a single sheet, as before. This is separate from your stock
  pipeline - nothing here touches Tally or GST. Handwriting needs an AI
  vision engine (Gemini / OpenAI / Claude / Auto) - RapidOCR/Tesseract
  cannot read handwriting, so set that up in Settings -> AI / OCR first if
  this page asks for it.

Your licence key activates one company. Keep it somewhere safe.

Questions: {brand}, {support}
"""


def _vendor_env_value(name: str) -> str:
    env = ROOT / ".env"
    if not env.exists():
        return ""
    match = re.search(rf"^{name}=(.*)$", env.read_text(encoding="utf-8"), re.M)
    return match.group(1).strip() if match else ""


def vendor_secret() -> str:
    return _vendor_env_value("LICENSE_SIGNING_SECRET")


def build_env(secret: str) -> str:
    """The customer's .env: the example with the secret already filled in."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    text = re.sub(
        r"^LICENSE_SIGNING_SECRET=.*$",
        f"LICENSE_SIGNING_SECRET={secret}",
        text,
        flags=re.M,
    )
    # Point every customer build at the vendor's own deployed OCR proxy (see
    # ocr_proxy/) so "SN Softech Cloud AI" works out of the box in Settings -
    # the customer only ever enters their own licence key, never a URL.
    # Blank in the vendor's dev .env just means that engine shows a clear
    # "not configured" message instead of breaking the build.
    proxy_url = _vendor_env_value("OCR_PROXY_URL")
    if proxy_url:
        text = re.sub(
            r"^OCR_PROXY_URL=.*$",
            f"OCR_PROXY_URL={proxy_url}",
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


def apply_launcher_patches(destination: Path) -> None:
    """Swap seed_demo.py out for init_customer_db.py in the customer copy of
    each launcher, and drop the printed demo credentials — see
    LAUNCHER_PATCHES for what changes and why. Raises if a patch does not
    apply: shipping a launcher that still seeds a demo account, silently,
    is worse than refusing to build."""
    for filename, patches in LAUNCHER_PATCHES.items():
        target = destination / filename
        if not target.exists():
            raise RuntimeError(f"{filename} was not found in the copied build.")
        with open(target, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        for old, new in patches:
            if old not in text:
                raise RuntimeError(
                    f"{filename} has changed and no longer matches an expected "
                    f"block — update LAUNCHER_PATCHES in this script.\n"
                    f"  (looked for: {old[:70]!r}...)"
                )
            text = text.replace(old, new)
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


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
    try:
        apply_launcher_patches(staging)
    except RuntimeError as exc:
        print(f"REFUSING: {exc}")
        shutil.rmtree(staging)
        return 1
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
        all_names = bundle.namelist()
        names = {Path(n).name for n in all_names}
    leaked = names & EXCLUDE_FILES - {".env"}
    if leaked:
        print(f"REFUSING: these should not be in a customer build: {sorted(leaked)}")
        archive.unlink()
        return 1
    proxy_leaked = [n for n in all_names if "ocr_proxy" in Path(n).parts]
    if proxy_leaked:
        print(f"REFUSING: ocr_proxy (vendor-only, holds the real Gemini key) "
              f"leaked into the build: {proxy_leaked[:3]}")
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
