"""
One-time setup of the licence signing secret.

    python setup_secret.py            # generate one if .env has none
    python setup_secret.py --show     # print what is configured (masked)
    python setup_secret.py --set VALUE

Every licence key is signed with this value. Two rules:

  * Set it ONCE per business, then keep it safe. Changing it invalidates
    every key you have ever issued and there is no way back.
  * A vendor and their customers must share the SAME value, or the keys the
    vendor issues will not validate on the customer's machine.

This script never overwrites an existing secret unless you pass --set.
"""
import argparse
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
KEY = "LICENSE_SIGNING_SECRET"


def read_env() -> str:
    if not ENV.exists():
        return ""
    return ENV.read_text(encoding="utf-8")


def current_value(text: str) -> str:
    match = re.search(rf"^{KEY}=(.*)$", text, re.M)
    return match.group(1).strip() if match else ""


def write_value(text: str, value: str) -> str:
    if re.search(rf"^{KEY}=", text, re.M):
        return re.sub(rf"^{KEY}=.*$", f"{KEY}={value}", text, flags=re.M)
    return text.rstrip("\n") + f"\n{KEY}={value}\n"


def mask(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure the licence signing secret.")
    parser.add_argument("--show", action="store_true", help="show what is set, masked")
    parser.add_argument("--set", dest="value", help="set a specific value (overwrites)")
    args = parser.parse_args()

    if not ENV.exists():
        example = ROOT / ".env.example"
        if not example.exists():
            print("No .env or .env.example found. Run this from the app folder.")
            return 1
        ENV.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print("Created .env from .env.example")

    text = read_env()
    existing = current_value(text)

    if args.show:
        print(f"{KEY} = {mask(existing)}")
        return 0

    if args.value:
        if existing and existing != args.value:
            print("WARNING: a secret is already set.")
            print("Replacing it invalidates every licence key issued with the old one.")
            if input("Type REPLACE to continue: ").strip() != "REPLACE":
                print("Cancelled. Nothing changed.")
                return 1
        ENV.write_text(write_value(text, args.value), encoding="utf-8")
        print(f"{KEY} set to {mask(args.value)}")
        return 0

    if existing:
        print(f"Already configured: {mask(existing)}")
        print("Nothing to do. Use --set to change it (this invalidates old keys).")
        return 0

    value = secrets.token_urlsafe(32)
    ENV.write_text(write_value(text, value), encoding="utf-8")
    print("A new licence signing secret was generated and saved to .env:")
    print()
    print(f"    {value}")
    print()
    print("WRITE THIS DOWN somewhere safe, outside this computer.")
    print("Every licence key you issue is signed with it. If you lose it, or")
    print("change it, every key you have issued stops working permanently.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
