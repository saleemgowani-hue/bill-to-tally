"""
Mint licence keys for the AI Bill-to-Tally Stock Update Agent.

This is a VENDOR tool for SN Softech Solutions — it belongs on your PC,
not on a customer's. Keys are signed with LICENSE_SIGNING_SECRET from
your .env; anyone holding that secret can mint keys, so keep it private.

    python generate_keys.py                       # 25 yearly + 25 monthly -> Excel
    python generate_keys.py --yearly 50           # 50 yearly only
    python generate_keys.py --monthly 10 --csv    # also write a CSV
    python generate_keys.py --check SNST-...      # verify a single key

Every run appends to keys_issued_log.csv so you always have a record of
what you handed out, even if the Excel file is edited or lost.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
from pathlib import Path

from services.license_service import PLANS, generate_batch, inspect_key
from utils.config import settings

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "keys_issued_log.csv"


def build_rows(plan: str, count: int, batch: str) -> list[dict]:
    _, days, label = next(
        (value for char, value in PLANS.items() if value[0] == plan), (plan, 0, plan)
    )
    today = dt.date.today()
    return [
        {
            "Sr No": index,
            "Licence Key": key,
            "Plan": plan.title(),
            "Validity (days)": days,
            "Generated On": today.strftime("%d-%m-%Y"),
            "Batch": batch,
            "Status": "Unsold",
            "Customer Name": "",
            "Contact": "",
            "Sold On": "",
            "Activated On": "",
            "Expires On": "",
            "Notes": "",
        }
        for index, key in enumerate(generate_batch(plan, count), start=1)
    ]


def append_log(rows: list[dict]) -> None:
    exists = LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["Licence Key", "Plan", "Validity (days)", "Generated On", "Batch"]
        )
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in writer.fieldnames})


def write_excel(groups: dict[str, list[dict]], path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    HEAD_FILL = PatternFill("solid", fgColor="0B4A6F")
    HEAD_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    BODY = Font(name="Arial", size=10)
    KEYFONT = Font(name="Consolas", size=10, bold=True, color="1F4E79")
    FILLIN = PatternFill("solid", fgColor="FFFDE7")
    THIN = Side(style="thin", color="D0D5DD")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"

    # ---------------- key sheets ----------------
    headers = list(next(iter(groups.values()))[0].keys())
    fill_in_columns = {"Status", "Customer Name", "Contact", "Sold On",
                       "Activated On", "Expires On", "Notes"}

    for plan, rows in groups.items():
        sheet = workbook.create_sheet(f"{plan.title()} Keys")

        sheet["A1"] = f"{settings.COMPANY_BRAND} — {plan.title()} Licence Keys"
        sheet["A1"].font = Font(name="Arial", size=13, bold=True, color="0B4A6F")
        sheet["A2"] = (
            f"{settings.APP_NAME} · generated {dt.date.today():%d-%m-%Y} · "
            f"support {settings.SUPPORT_CONTACT}"
        )
        sheet["A2"].font = Font(name="Arial", size=9, italic=True, color="667085")
        sheet["A3"] = (
            "Yellow columns are for you to fill in as you sell each key. "
            "Each key activates ONE company; validity starts on activation, not on purchase."
        )
        sheet["A3"].font = Font(name="Arial", size=9, color="B54708")

        header_row = 5
        for column, name in enumerate(headers, start=1):
            cell = sheet.cell(row=header_row, column=column, value=name)
            cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        for offset, row in enumerate(rows, start=header_row + 1):
            for column, name in enumerate(headers, start=1):
                cell = sheet.cell(row=offset, column=column, value=row[name])
                cell.border = BORDER
                cell.font = KEYFONT if name == "Licence Key" else BODY
                if name in fill_in_columns:
                    cell.fill = FILLIN
                if name in ("Sr No", "Validity (days)", "Plan", "Generated On", "Batch"):
                    cell.alignment = Alignment(horizontal="center")

        last_row = header_row + len(rows)

        status_rule = DataValidation(
            type="list", formula1='"Unsold,Sold,Activated,Expired,Cancelled"', allow_blank=True
        )
        sheet.add_data_validation(status_rule)
        status_column = get_column_letter(headers.index("Status") + 1)
        status_rule.add(f"{status_column}{header_row + 1}:{status_column}{last_row}")

        widths = {
            "Sr No": 7, "Licence Key": 26, "Plan": 10, "Validity (days)": 14,
            "Generated On": 14, "Batch": 14, "Status": 12, "Customer Name": 24,
            "Contact": 15, "Sold On": 13, "Activated On": 14, "Expires On": 13,
            "Notes": 30,
        }
        for column, name in enumerate(headers, start=1):
            sheet.column_dimensions[get_column_letter(column)].width = widths.get(name, 14)
        sheet.freeze_panes = f"A{header_row + 1}"
        sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{last_row}"

    # ---------------- summary ----------------
    summary["A1"] = f"{settings.COMPANY_BRAND} — Licence Key Register"
    summary["A1"].font = Font(name="Arial", size=15, bold=True, color="0B4A6F")
    summary["A2"] = settings.BRAND_TAGLINE
    summary["A2"].font = Font(name="Arial", size=10, italic=True, color="667085")
    summary["A3"] = f"{settings.APP_NAME} · support {settings.SUPPORT_CONTACT}"
    summary["A3"].font = Font(name="Arial", size=10, color="667085")

    for column, name in enumerate(
        ["Plan", "Keys issued", "Validity (days)", "Sold", "Unsold"], start=1
    ):
        cell = summary.cell(row=5, column=column, value=name)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
        cell.alignment = Alignment(horizontal="center")

    row_number = 6
    for plan, rows in groups.items():
        tab = f"'{plan.title()} Keys'"
        first, last = 6, 5 + len(rows)
        summary.cell(row=row_number, column=1, value=plan.title()).font = BODY
        # Counted with formulas so the register stays live as you mark keys sold.
        summary.cell(
            row=row_number, column=2, value=f"=COUNTA({tab}!B{first}:B{last})"
        ).font = BODY
        summary.cell(row=row_number, column=3, value=rows[0]["Validity (days)"]).font = BODY
        summary.cell(
            row=row_number, column=4,
            value=f'=COUNTIF({tab}!G{first}:G{last},"Sold")'
                  f'+COUNTIF({tab}!G{first}:G{last},"Activated")',
        ).font = BODY
        summary.cell(
            row=row_number, column=5, value=f'=COUNTIF({tab}!G{first}:G{last},"Unsold")'
        ).font = BODY
        for column in range(1, 6):
            summary.cell(row=row_number, column=column).border = BORDER
            if column != 1:
                summary.cell(row=row_number, column=column).alignment = Alignment(
                    horizontal="center"
                )
        row_number += 1

    total_row = row_number
    summary.cell(row=total_row, column=1, value="TOTAL").font = Font(
        name="Arial", size=10, bold=True
    )
    for column in (2, 4, 5):
        letter = get_column_letter(column)
        cell = summary.cell(
            row=total_row, column=column,
            value=f"=SUM({letter}6:{letter}{total_row - 1})",
        )
        cell.font = Font(name="Arial", size=10, bold=True)
        cell.alignment = Alignment(horizontal="center")
    for column in range(1, 6):
        summary.cell(row=total_row, column=column).border = BORDER

    notes = [
        "",
        "HOW TO USE",
        "1. Give the customer one key from the relevant sheet.",
        "2. They enter it on the Sign up screen — a key is compulsory to create an account.",
        "3. Validity is counted from the day they ACTIVATE it, not the day you sold it.",
        "4. Each key works for exactly ONE company. It cannot be reused elsewhere.",
        "5. Mark Status as Sold, then Activated, so you know what is outstanding.",
        "6. To renew: give a new key. Settings > Licence > Activate. Renewing early",
        "   extends from the current expiry, so no paid days are lost.",
        "",
        "IMPORTANT",
        "Keys are signed with LICENSE_SIGNING_SECRET in your .env file.",
        "If you change that secret, every key in this sheet stops working.",
        "Keep this file and that secret confidential.",
    ]
    for offset, line in enumerate(notes, start=total_row + 2):
        cell = summary.cell(row=offset, column=1, value=line)
        if line in ("HOW TO USE", "IMPORTANT"):
            cell.font = Font(name="Arial", size=10, bold=True, color="0B4A6F")
        else:
            cell.font = Font(name="Arial", size=9, color="475467")

    for column, width in zip("ABCDE", (46, 14, 16, 12, 12)):
        summary.column_dimensions[column].width = width

    workbook.save(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate licence keys.")
    parser.add_argument("--yearly", type=int, default=25, help="how many yearly keys")
    parser.add_argument("--monthly", type=int, default=25, help="how many monthly keys")
    parser.add_argument("--trial", type=int, default=0, help="how many trial keys")
    parser.add_argument("--out", default="SN_Softech_Licence_Keys.xlsx")
    parser.add_argument("--csv", action="store_true", help="also write a CSV")
    parser.add_argument("--check", help="verify one key instead of generating")
    args = parser.parse_args()

    if args.check:
        try:
            info = inspect_key(args.check)
        except Exception as exc:
            print(f"INVALID: {exc}")
            return 1
        print(f"VALID  {info.key}  {info.plan}  {info.duration_days} days")
        return 0

    batch = f"B{dt.date.today():%Y%m%d}"
    groups: dict[str, list[dict]] = {}
    for plan, count in (("YEARLY", args.yearly), ("MONTHLY", args.monthly),
                        ("TRIAL", args.trial)):
        if count > 0:
            groups[plan] = build_rows(plan, count, batch)

    if not groups:
        print("Nothing to generate.")
        return 1

    path = ROOT / args.out
    write_excel(groups, path)
    for rows in groups.values():
        append_log(rows)

    if args.csv:
        csv_path = path.with_suffix(".csv")
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(next(iter(groups.values()))[0]))
            writer.writeheader()
            for rows in groups.values():
                writer.writerows(rows)
        print(f"CSV   : {csv_path}")

    for plan, rows in groups.items():
        print(f"{plan.title():8}: {len(rows)} keys")
    print(f"Excel : {path}")
    print(f"Log   : {LOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
