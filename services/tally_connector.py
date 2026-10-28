"""
Tally integration layer.

HOW THIS ACTUALLY WORKS
-----------------------
Tally Prime / Tally.ERP 9 can act as an HTTP server on the machine where it is
running (Gateway of Tally -> F1: Help -> Settings -> Connectivity -> Client/Server
configuration -> "Act as Server" = Yes, default port 9000). Requests and
responses are XML envelopes. That is the documented, supported integration
surface and it is the only one this module uses. There is no cloud API and no
undocumented endpoint.

Consequences you must design around:
  * Tally has to be OPEN on that machine with the company LOADED.
  * The app must reach that machine over the network (same LAN, or a tunnel).
  * A pure-cloud SaaS install cannot reach a customer's desktop Tally directly;
    for those customers the app produces a Tally-compatible XML file that the
    user imports (Gateway of Tally -> Import -> Vouchers).

Three connectors implement one interface:
  * TallyXMLConnector  - real HTTP gateway
  * MockTallyConnector - deterministic fake for development and demos
  * FileExportConnector- always writes an importable XML file, never posts

TALLY_TEST_MODE=true shows the exact XML that WOULD be sent, and posts nothing.
"""
from __future__ import annotations

import abc
import datetime as dt
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from xml.sax.saxutils import escape

from utils.config import settings
from utils.logging import get_logger

log = get_logger("tally")

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class TallyError(RuntimeError):
    pass


@dataclass
class TallyResult:
    success: bool
    message: str
    mode: str = ""
    voucher_id: str = ""
    request_xml: str = ""
    response_xml: str = ""
    export_path: str = ""
    created: int = 0
    altered: int = 0
    errors: int = 0
    warnings: List[str] = field(default_factory=list)


@dataclass
class VoucherPayload:
    """Everything needed to build a voucher, already mapped.

    Field names are purchase-flavoured for historical reasons, but they are
    reused as-is for Sales: supplier_ledger doubles as the party ledger
    (customer, for a sale) and purchase_ledger doubles as the trading ledger
    (Sales Account, for a sale). transaction_kind picks which polarity
    build_voucher_xml applies — see build_sales_voucher_xml.
    """

    company_name: str
    voucher_type: str
    supplier_ledger: str
    purchase_ledger: str
    invoice_number: str
    invoice_date: str                 # ISO yyyy-mm-dd
    narration: str = ""
    godown: str = ""
    round_off_ledger: str = ""
    round_off_amount: float = 0.0
    tax_lines: List[dict] = field(default_factory=list)   # {ledger, amount}
    items: List[dict] = field(default_factory=list)
    grand_total: float = 0.0
    reference_id: str = ""
    transaction_kind: str = "purchase"   # "purchase" | "sales"


# --------------------------------------------------------------------------
# XML helpers
# --------------------------------------------------------------------------
def x(value) -> str:
    """Escape a value for XML content."""
    return escape(str(value if value is not None else ""))


def tally_date(iso_date: str) -> str:
    """'2026-09-04' -> '20260904' (Tally's date format)."""
    try:
        return dt.date.fromisoformat((iso_date or "").strip()).strftime("%Y%m%d")
    except (ValueError, TypeError):
        return dt.date.today().strftime("%Y%m%d")


def amount(value: float) -> str:
    return f"{float(value or 0.0):.2f}"


def build_purchase_voucher_xml(payload: VoucherPayload) -> str:
    """
    Build a Purchase voucher in Tally's Import Data format.

    Accounting convention: supplier ledger is CREDITED with the invoice total
    (negative amount in Tally XML), purchase/tax ledgers are DEBITED (positive).
    """
    total = float(payload.grand_total or 0.0)

    inventory_blocks = []
    for item in payload.items:
        quantity = float(item.get("quantity") or 0)
        rate = float(item.get("rate") or 0)
        taxable = float(item.get("taxable_value") or 0)
        unit = item.get("unit") or "PCS"
        godown = item.get("godown") or payload.godown
        godown_block = (
            f"""
              <BATCHALLOCATIONS.LIST>
                <GODOWNNAME>{x(godown)}</GODOWNNAME>
                <BATCHNAME>Primary Batch</BATCHNAME>
                <AMOUNT>{amount(taxable)}</AMOUNT>
                <ACTUALQTY>{quantity:g} {x(unit)}</ACTUALQTY>
                <BILLEDQTY>{quantity:g} {x(unit)}</BILLEDQTY>
              </BATCHALLOCATIONS.LIST>"""
            if godown
            else ""
        )
        inventory_blocks.append(
            f"""
            <ALLINVENTORYENTRIES.LIST>
              <STOCKITEMNAME>{x(item.get('stock_item'))}</STOCKITEMNAME>
              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
              <RATE>{amount(rate)}/{x(unit)}</RATE>
              <AMOUNT>-{amount(taxable)}</AMOUNT>
              <ACTUALQTY>{quantity:g} {x(unit)}</ACTUALQTY>
              <BILLEDQTY>{quantity:g} {x(unit)}</BILLEDQTY>
              <HSNCODE>{x(item.get('hsn') or '')}</HSNCODE>{godown_block}
              <ACCOUNTINGALLOCATIONS.LIST>
                <LEDGERNAME>{x(payload.purchase_ledger)}</LEDGERNAME>
                <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                <AMOUNT>-{amount(taxable)}</AMOUNT>
              </ACCOUNTINGALLOCATIONS.LIST>
            </ALLINVENTORYENTRIES.LIST>"""
        )

    tax_blocks = []
    for tax in payload.tax_lines:
        tax_amount = float(tax.get("amount") or 0)
        if abs(tax_amount) < 0.005:
            continue
        tax_blocks.append(
            f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{x(tax.get('ledger'))}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amount(tax_amount)}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""
        )

    if payload.round_off_ledger and abs(payload.round_off_amount) >= 0.005:
        sign = "" if payload.round_off_amount > 0 else "-"
        tax_blocks.append(
            f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{x(payload.round_off_ledger)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>{'Yes' if payload.round_off_amount > 0 else 'No'}</ISDEEMEDPOSITIVE>
              <AMOUNT>{sign}{amount(abs(payload.round_off_amount))}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""
        )

    party_block = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{x(payload.supplier_ledger)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
              <AMOUNT>{amount(total)}</AMOUNT>
              <BILLALLOCATIONS.LIST>
                <NAME>{x(payload.invoice_number)}</NAME>
                <BILLTYPE>New Ref</BILLTYPE>
                <AMOUNT>{amount(total)}</AMOUNT>
              </BILLALLOCATIONS.LIST>
            </ALLLEDGERENTRIES.LIST>"""

    remote_id = payload.reference_id or str(uuid.uuid4())
    date_value = tally_date(payload.invoice_date)

    return f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{x(payload.company_name)}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER VCHTYPE="{x(payload.voucher_type)}" ACTION="Create" OBJVIEW="Invoice Voucher View" REMOTEID="{x(remote_id)}">
            <DATE>{date_value}</DATE>
            <EFFECTIVEDATE>{date_value}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>{x(payload.voucher_type)}</VOUCHERTYPENAME>
            <REFERENCE>{x(payload.invoice_number)}</REFERENCE>
            <REFERENCEDATE>{date_value}</REFERENCEDATE>
            <PARTYLEDGERNAME>{x(payload.supplier_ledger)}</PARTYLEDGERNAME>
            <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
            <NARRATION>{x(payload.narration)}</NARRATION>
            <ISINVOICE>Yes</ISINVOICE>
            <HASCASHFLOW>No</HASCASHFLOW>{party_block}{''.join(tax_blocks)}{''.join(inventory_blocks)}
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


def build_sales_voucher_xml(payload: VoucherPayload) -> str:
    """
    Build a Sales voucher in Tally's Import Data format — the exact mirror
    of build_purchase_voucher_xml, with every debit/credit flipped:

    Accounting convention: customer ledger (payload.supplier_ledger) is
    DEBITED with the invoice total (negative amount, ISDEEMEDPOSITIVE=Yes —
    receivable goes up); the sales/trading ledger (payload.purchase_ledger)
    and output-tax ledgers are CREDITED (positive amount, ISDEEMEDPOSITIVE=No
    — income and tax liability go up). This is standard double-entry for a
    sale: Dr Customer, Cr Sales + Output GST.
    """
    total = float(payload.grand_total or 0.0)

    inventory_blocks = []
    for item in payload.items:
        quantity = float(item.get("quantity") or 0)
        rate = float(item.get("rate") or 0)
        taxable = float(item.get("taxable_value") or 0)
        unit = item.get("unit") or "PCS"
        godown = item.get("godown") or payload.godown
        godown_block = (
            f"""
              <BATCHALLOCATIONS.LIST>
                <GODOWNNAME>{x(godown)}</GODOWNNAME>
                <BATCHNAME>Primary Batch</BATCHNAME>
                <AMOUNT>-{amount(taxable)}</AMOUNT>
                <ACTUALQTY>-{quantity:g} {x(unit)}</ACTUALQTY>
                <BILLEDQTY>-{quantity:g} {x(unit)}</BILLEDQTY>
              </BATCHALLOCATIONS.LIST>"""
            if godown
            else ""
        )
        inventory_blocks.append(
            f"""
            <ALLINVENTORYENTRIES.LIST>
              <STOCKITEMNAME>{x(item.get('stock_item'))}</STOCKITEMNAME>
              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
              <RATE>{amount(rate)}/{x(unit)}</RATE>
              <AMOUNT>{amount(taxable)}</AMOUNT>
              <ACTUALQTY>-{quantity:g} {x(unit)}</ACTUALQTY>
              <BILLEDQTY>-{quantity:g} {x(unit)}</BILLEDQTY>
              <HSNCODE>{x(item.get('hsn') or '')}</HSNCODE>{godown_block}
              <ACCOUNTINGALLOCATIONS.LIST>
                <LEDGERNAME>{x(payload.purchase_ledger)}</LEDGERNAME>
                <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                <AMOUNT>{amount(taxable)}</AMOUNT>
              </ACCOUNTINGALLOCATIONS.LIST>
            </ALLINVENTORYENTRIES.LIST>"""
        )

    tax_blocks = []
    for tax in payload.tax_lines:
        tax_amount = float(tax.get("amount") or 0)
        if abs(tax_amount) < 0.005:
            continue
        tax_blocks.append(
            f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{x(tax.get('ledger'))}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
              <AMOUNT>{amount(tax_amount)}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""
        )

    if payload.round_off_ledger and abs(payload.round_off_amount) >= 0.005:
        # Positive round_off (total rounded UP) increases what the customer
        # owes — a credit. Negative round_off (rounded down) is a debit.
        sign = "-" if payload.round_off_amount > 0 else ""
        tax_blocks.append(
            f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{x(payload.round_off_ledger)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>{'No' if payload.round_off_amount > 0 else 'Yes'}</ISDEEMEDPOSITIVE>
              <AMOUNT>{sign}{amount(abs(payload.round_off_amount))}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""
        )

    party_block = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{x(payload.supplier_ledger)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amount(total)}</AMOUNT>
              <BILLALLOCATIONS.LIST>
                <n>{x(payload.invoice_number)}</n>
                <BILLTYPE>New Ref</BILLTYPE>
                <AMOUNT>-{amount(total)}</AMOUNT>
              </BILLALLOCATIONS.LIST>
            </ALLLEDGERENTRIES.LIST>"""

    remote_id = payload.reference_id or str(uuid.uuid4())
    date_value = tally_date(payload.invoice_date)

    return f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{x(payload.company_name)}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER VCHTYPE="{x(payload.voucher_type)}" ACTION="Create" OBJVIEW="Invoice Voucher View" REMOTEID="{x(remote_id)}">
            <DATE>{date_value}</DATE>
            <EFFECTIVEDATE>{date_value}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>{x(payload.voucher_type)}</VOUCHERTYPENAME>
            <REFERENCE>{x(payload.invoice_number)}</REFERENCE>
            <REFERENCEDATE>{date_value}</REFERENCEDATE>
            <PARTYLEDGERNAME>{x(payload.supplier_ledger)}</PARTYLEDGERNAME>
            <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
            <NARRATION>{x(payload.narration)}</NARRATION>
            <ISINVOICE>Yes</ISINVOICE>
            <HASCASHFLOW>No</HASCASHFLOW>{party_block}{''.join(tax_blocks)}{''.join(inventory_blocks)}
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


def build_voucher_xml(payload: VoucherPayload) -> str:
    """Dispatch to the right builder by payload.transaction_kind."""
    if payload.transaction_kind == "sales":
        return build_sales_voucher_xml(payload)
    return build_purchase_voucher_xml(payload)



@dataclass
class LedgerVoucherPayload:
    """A pure ledger-to-ledger voucher — Payment, Receipt, or Contra — with
    no stock items. Used for bank reconciliation: money leaving the bank
    (Payment) or arriving in it (Receipt), optionally allocated against one
    or more open Purchase/Sales invoices via bill_allocations.

    Accounting convention (standard double-entry, same debit=negative+Yes /
    credit=positive+No pattern as VoucherPayload):
      Payment: Dr party_ledger (supplier/expense), Cr bank_ledger
      Receipt: Dr bank_ledger, Cr party_ledger (customer/income)
    See build_ledger_voucher_xml.
    """

    company_name: str
    voucher_type: str          # "Payment" | "Receipt" | "Contra"
    direction: str             # "payment" | "receipt" — picks the polarity
    bank_ledger: str
    party_ledger: str
    amount: float
    voucher_date: str          # ISO yyyy-mm-dd
    narration: str = ""
    reference_id: str = ""
    voucher_number: str = ""
    bill_allocations: List[dict] = field(default_factory=list)  # [{invoice_number, amount}]
    transaction_kind: str = "bank"


def build_ledger_voucher_xml(payload: LedgerVoucherPayload) -> str:
    amount_value = float(payload.amount or 0.0)
    is_payment = payload.direction == "payment"

    bill_blocks = "".join(
        f"""
              <BILLALLOCATIONS.LIST>
                <n>{x(alloc.get('invoice_number'))}</n>
                <BILLTYPE>Agst Ref</BILLTYPE>
                <AMOUNT>{'-' if is_payment else ''}{amount(float(alloc.get('amount') or 0))}</AMOUNT>
              </BILLALLOCATIONS.LIST>"""
        for alloc in payload.bill_allocations
        if abs(float(alloc.get("amount") or 0)) >= 0.005
    )

    # Payment: Dr party (debit=negative+Yes), Cr bank (credit=positive+No).
    # Receipt: Dr bank (debit=negative+Yes), Cr party (credit=positive+No).
    if is_payment:
        party_entry = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{x(payload.party_ledger)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amount(amount_value)}</AMOUNT>{bill_blocks}
            </ALLLEDGERENTRIES.LIST>"""
        bank_entry = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{x(payload.bank_ledger)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
              <AMOUNT>{amount(amount_value)}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""
    else:
        bank_entry = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{x(payload.bank_ledger)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amount(amount_value)}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""
        party_entry = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{x(payload.party_ledger)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
              <AMOUNT>{amount(amount_value)}</AMOUNT>{bill_blocks}
            </ALLLEDGERENTRIES.LIST>"""

    remote_id = payload.reference_id or str(uuid.uuid4())
    date_value = tally_date(payload.voucher_date)

    return f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{x(payload.company_name)}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER VCHTYPE="{x(payload.voucher_type)}" ACTION="Create" REMOTEID="{x(remote_id)}">
            <DATE>{date_value}</DATE>
            <EFFECTIVEDATE>{date_value}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>{x(payload.voucher_type)}</VOUCHERTYPENAME>
            <REFERENCE>{x(payload.voucher_number)}</REFERENCE>
            <PARTYLEDGERNAME>{x(payload.party_ledger)}</PARTYLEDGERNAME>
            <NARRATION>{x(payload.narration)}</NARRATION>
            <ISINVOICE>No</ISINVOICE>{bank_entry}{party_entry}
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


def build_xml_for_payload(payload) -> str:
    """Single entry point every connector uses: picks the right builder for
    whichever payload type it was handed."""
    if isinstance(payload, LedgerVoucherPayload):
        return build_ledger_voucher_xml(payload)
    return build_voucher_xml(payload)



def build_stock_item_xml(
    company_name: str, name: str, unit: str, hsn: str = "", gst_rate: float = 0.0,
    stock_group: str = "Primary",
) -> str:
    return f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{x(company_name)}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <STOCKITEM NAME="{x(name)}" ACTION="Create">
            <NAME>{x(name)}</NAME>
            <PARENT>{x(stock_group)}</PARENT>
            <BASEUNITS>{x(unit)}</BASEUNITS>
            <GSTAPPLICABLE>&#4; Applicable</GSTAPPLICABLE>
            <HSNCODE>{x(hsn)}</HSNCODE>
            <GSTTYPEOFSUPPLY>Goods</GSTTYPEOFSUPPLY>
          </STOCKITEM>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


def build_ledger_xml(company_name: str, name: str, group: str, gstin: str = "") -> str:
    gstin_block = f"\n            <PARTYGSTIN>{x(gstin)}</PARTYGSTIN>" if gstin else ""
    return f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{x(company_name)}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <LEDGER NAME="{x(name)}" ACTION="Create">
            <NAME>{x(name)}</NAME>
            <PARENT>{x(group)}</PARENT>{gstin_block}
          </LEDGER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


def build_collection_request(
    company_name: str, collection_id: str, tally_type: str, fetch: List[str]
) -> str:
    native = "".join(f"<NATIVEMETHOD>{x(f)}</NATIVEMETHOD>" for f in fetch)
    company_block = (
        f"<SVCURRENTCOMPANY>{x(company_name)}</SVCURRENTCOMPANY>" if company_name else ""
    )
    return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>{x(collection_id)}</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        {company_block}
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="{x(collection_id)}" ISMODIFY="No">
            <TYPE>{x(tally_type)}</TYPE>
            {native}
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""


COMPANY_LIST_REQUEST = """<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>List of Companies</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="List of Companies" ISMODIFY="No">
            <TYPE>Company</TYPE>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""


def parse_names(xml_text: str, tag: str) -> List[str]:
    """Pull NAME attributes / <NAME> values out of a Tally collection response."""
    names = re.findall(rf'<{tag}\b[^>]*NAME="([^"]+)"', xml_text or "", re.I)
    if not names:
        names = re.findall(
            rf"<{tag}\b[^>]*>.*?<NAME[^>]*>(.*?)</NAME>", xml_text or "", re.I | re.S
        )
    cleaned = []
    for name in names:
        value = re.sub(r"&#\d+;", "", name).strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def parse_import_response(xml_text: str) -> Dict[str, int]:
    def grab(tag: str) -> int:
        match = re.search(rf"<{tag}>(-?\d+)</{tag}>", xml_text or "", re.I)
        return int(match.group(1)) if match else 0

    return {
        "created": grab("CREATED"),
        "altered": grab("ALTERED"),
        "errors": grab("ERRORS"),
        "exceptions": grab("EXCEPTIONS"),
        "lasterror": grab("LASTVCHID"),
    }


def extract_error_text(xml_text: str) -> str:
    matches = re.findall(r"<LINEERROR>(.*?)</LINEERROR>", xml_text or "", re.I | re.S)
    if matches:
        return "; ".join(m.strip() for m in matches)[:600]
    match = re.search(r"<DESC>(.*?)</DESC>", xml_text or "", re.I | re.S)
    return match.group(1).strip()[:600] if match else ""


# --------------------------------------------------------------------------
# Connector interface
# --------------------------------------------------------------------------
class TallyConnector(abc.ABC):
    name = "base"

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.company_name = self.config.get("company") or settings.TALLY_COMPANY
        self.test_mode = bool(
            self.config.get("test_mode", settings.TALLY_TEST_MODE)
        )

    @abc.abstractmethod
    def test_connection(self) -> TallyResult: ...

    @abc.abstractmethod
    def list_companies(self) -> List[str]: ...

    @abc.abstractmethod
    def list_ledgers(self) -> List[str]: ...

    @abc.abstractmethod
    def list_stock_items(self) -> List[str]: ...

    @abc.abstractmethod
    def list_units(self) -> List[str]: ...

    @abc.abstractmethod
    def list_voucher_types(self) -> List[str]: ...

    @abc.abstractmethod
    def post_voucher(self, payload: VoucherPayload) -> TallyResult: ...

    def create_stock_item(self, name: str, unit: str, hsn: str = "", gst_rate: float = 0.0,
                          stock_group: str = "Primary") -> TallyResult:
        raise NotImplementedError

    def create_ledger(self, name: str, group: str, gstin: str = "") -> TallyResult:
        raise NotImplementedError

    # -- shared -----------------------------------------------------------
    def export_xml(self, payload: VoucherPayload, directory: Optional[Path] = None) -> str:
        """Always-available fallback: write an importable XML file."""
        directory = Path(directory or settings.EXPORT_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        number = getattr(payload, "invoice_number", "") or getattr(payload, "voucher_number", "") or "voucher"
        safe_number = re.sub(r"[^A-Za-z0-9_-]", "_", number)
        filename = (
            f"tally_{payload.transaction_kind}_{safe_number}_"
            f"{dt.datetime.now():%Y%m%d%H%M%S}.xml"
        )
        path = directory / filename
        path.write_text(build_xml_for_payload(payload), encoding="utf-8")
        log.info("Tally XML written to %s", path)
        return str(path)

    def preflight(self, payload) -> List[str]:
        """
        Verify every master the voucher references exists in Tally.
        Returns a list of human-readable problems (empty == good to post).
        """
        problems: List[str] = []
        try:
            companies = self.list_companies()
            if companies and self.company_name and self.company_name not in companies:
                problems.append(
                    f"Company '{self.company_name}' is not open in Tally. "
                    f"Open companies: {', '.join(companies) or 'none'}."
                )
            ledgers = {l.lower() for l in self.list_ledgers()}

            if isinstance(payload, LedgerVoucherPayload):
                if ledgers:
                    party_label = "Customer/expense ledger" if payload.direction == "payment" else "Party ledger"
                    for label, ledger in (
                        ("Bank ledger", payload.bank_ledger),
                        (party_label, payload.party_ledger),
                    ):
                        if ledger and ledger.lower() not in ledgers:
                            problems.append(f"{label} '{ledger}' does not exist in Tally.")
                voucher_types = {v.lower() for v in self.list_voucher_types()}
                if voucher_types and payload.voucher_type.lower() not in voucher_types:
                    problems.append(f"Voucher type '{payload.voucher_type}' does not exist in Tally.")
                return list(dict.fromkeys(problems))

            if ledgers:
                party_label = "Customer ledger" if payload.transaction_kind == "sales" else "Supplier ledger"
                trading_label = "Sales ledger" if payload.transaction_kind == "sales" else "Purchase ledger"
                for label, ledger in (
                    (party_label, payload.supplier_ledger),
                    (trading_label, payload.purchase_ledger),
                ):
                    if ledger and ledger.lower() not in ledgers:
                        problems.append(f"{label} '{ledger}' does not exist in Tally.")
                for tax in payload.tax_lines:
                    ledger = tax.get("ledger") or ""
                    if (
                        ledger
                        and abs(float(tax.get("amount") or 0)) >= 0.005
                        and ledger.lower() not in ledgers
                    ):
                        problems.append(f"Tax ledger '{ledger}' does not exist in Tally.")
                if (
                    payload.round_off_ledger
                    and abs(payload.round_off_amount) >= 0.005
                    and payload.round_off_ledger.lower() not in ledgers
                ):
                    problems.append(
                        f"Round-off ledger '{payload.round_off_ledger}' does not exist in Tally."
                    )
            stock_items = {s.lower() for s in self.list_stock_items()}
            if stock_items:
                for item in payload.items:
                    name = (item.get("stock_item") or "").lower()
                    if name and name not in stock_items:
                        problems.append(
                            f"Stock item '{item.get('stock_item')}' does not exist in Tally."
                        )
            units = {u.lower() for u in self.list_units()}
            if units:
                for item in payload.items:
                    unit = (item.get("unit") or "").lower()
                    if unit and unit not in units:
                        problems.append(
                            f"Unit '{item.get('unit')}' is not defined in Tally."
                        )
            voucher_types = {v.lower() for v in self.list_voucher_types()}
            if voucher_types and payload.voucher_type.lower() not in voucher_types:
                problems.append(
                    f"Voucher type '{payload.voucher_type}' does not exist in Tally."
                )
        except TallyError as exc:
            problems.append(str(exc))
        # de-duplicate, keep order
        return list(dict.fromkeys(problems))


# --------------------------------------------------------------------------
class TallyXMLConnector(TallyConnector):
    """Real connector over Tally's HTTP XML gateway."""

    name = "tally-xml"

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.host = self.config.get("host") or settings.TALLY_HOST
        self.port = int(self.config.get("port") or settings.TALLY_PORT)
        self.timeout = int(
            self.config.get("timeout") or settings.TALLY_TIMEOUT_SECONDS
        )

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _send(self, xml_body: str) -> str:
        if requests is None:
            raise TallyError("The 'requests' package is required for Tally connectivity.")
        try:
            response = requests.post(
                self.url,
                data=xml_body.encode("utf-8"),
                headers={"Content-Type": "text/xml;charset=utf-8"},
                timeout=self.timeout,
            )
        except Exception as exc:
            raise TallyError(
                f"Could not reach Tally at {self.url}. Is Tally open with "
                f"'Act as Server' enabled? ({exc})"
            ) from exc
        if response.status_code != 200:
            raise TallyError(f"Tally returned HTTP {response.status_code}.")
        return response.text

    def test_connection(self) -> TallyResult:
        try:
            body = self._send(COMPANY_LIST_REQUEST)
        except TallyError as exc:
            return TallyResult(False, str(exc), mode="LIVE")
        companies = parse_names(body, "COMPANY")
        message = (
            f"Connected to Tally at {self.url}. "
            f"Open companies: {', '.join(companies) if companies else 'none detected'}."
        )
        return TallyResult(True, message, mode="LIVE", response_xml=body[:4000])

    def list_companies(self) -> List[str]:
        return parse_names(self._send(COMPANY_LIST_REQUEST), "COMPANY")

    def list_ledgers(self) -> List[str]:
        body = self._send(
            build_collection_request(
                self.company_name, "List of Ledgers", "Ledger", ["Name", "Parent"]
            )
        )
        return parse_names(body, "LEDGER")

    def list_stock_items(self) -> List[str]:
        body = self._send(
            build_collection_request(
                self.company_name,
                "List of StockItems",
                "StockItem",
                ["Name", "BaseUnits", "ClosingBalance"],
            )
        )
        return parse_names(body, "STOCKITEM")

    def list_units(self) -> List[str]:
        body = self._send(
            build_collection_request(
                self.company_name, "List of Units", "Unit", ["Name", "OriginalName"]
            )
        )
        return parse_names(body, "UNIT")

    def list_voucher_types(self) -> List[str]:
        body = self._send(
            build_collection_request(
                self.company_name, "List of VoucherTypes", "VoucherType", ["Name", "Parent"]
            )
        )
        return parse_names(body, "VOUCHERTYPE")

    def post_voucher(self, payload: VoucherPayload) -> TallyResult:
        xml_body = build_xml_for_payload(payload)
        if self.test_mode:
            path = self.export_xml(payload)
            return TallyResult(
                True,
                "TEST MODE — nothing was posted to Tally. The exact XML that would "
                "have been sent is shown below and saved to disk.",
                mode="TEST_MODE",
                request_xml=xml_body,
                export_path=path,
                warnings=["TALLY_TEST_MODE is ON. No accounting entry was created."],
            )
        try:
            response = self._send(xml_body)
        except TallyError as exc:
            return TallyResult(False, str(exc), mode="LIVE", request_xml=xml_body)

        stats = parse_import_response(response)
        if stats["errors"] or stats["exceptions"] or stats["created"] < 1:
            detail = extract_error_text(response) or "Tally reported no voucher created."
            return TallyResult(
                False,
                f"Tally rejected the voucher: {detail}",
                mode="LIVE",
                request_xml=xml_body,
                response_xml=response[:8000],
                errors=stats["errors"] or 1,
            )
        if isinstance(payload, LedgerVoucherPayload):
            kind_label = "Payment" if payload.direction == "payment" else "Receipt"
        else:
            kind_label = "Sales" if payload.transaction_kind == "sales" else "Purchase"
        return TallyResult(
            True,
            f"{kind_label} voucher created in Tally ({stats['created']} voucher).",
            mode="POSTED",
            voucher_id=payload.reference_id,
            request_xml=xml_body,
            response_xml=response[:8000],
            created=stats["created"],
            altered=stats["altered"],
        )

    def create_stock_item(self, name, unit, hsn="", gst_rate=0.0, stock_group="Primary"):
        xml_body = build_stock_item_xml(
            self.company_name, name, unit, hsn, gst_rate, stock_group
        )
        if self.test_mode:
            return TallyResult(
                True, f"TEST MODE — would create stock item '{name}'.",
                mode="TEST_MODE", request_xml=xml_body,
            )
        response = self._send(xml_body)
        stats = parse_import_response(response)
        ok = stats["created"] > 0 or stats["altered"] > 0
        return TallyResult(
            ok,
            f"Stock item '{name}' " + ("created." if ok else "was not created."),
            mode="LIVE",
            request_xml=xml_body,
            response_xml=response[:4000],
        )

    def create_ledger(self, name, group, gstin=""):
        xml_body = build_ledger_xml(self.company_name, name, group, gstin)
        if self.test_mode:
            return TallyResult(
                True, f"TEST MODE — would create ledger '{name}' under '{group}'.",
                mode="TEST_MODE", request_xml=xml_body,
            )
        response = self._send(xml_body)
        stats = parse_import_response(response)
        ok = stats["created"] > 0 or stats["altered"] > 0
        return TallyResult(
            ok,
            f"Ledger '{name}' " + ("created." if ok else "was not created."),
            mode="LIVE",
            request_xml=xml_body,
            response_xml=response[:4000],
        )


# --------------------------------------------------------------------------
class MockTallyConnector(TallyConnector):
    """
    Deterministic stand-in for a real Tally installation.

    Used for development, automated tests and product demos. It never claims a
    voucher reached Tally: every result is labelled MOCK.
    """

    name = "mock"

    COMPANIES = ["SN Demo Traders (2026-27)"]
    LEDGERS = [
        "Shreeji Distributors", "Purchase Account", "CGST Input", "SGST Input",
        "IGST Input", "Cess Input", "Round Off", "Discount Received",
        "Sundry Creditors",
        "Sales Account", "CGST Output", "SGST Output", "IGST Output",
        "Cess Output", "Sundry Debtors",
        "HDFC Bank", "Bank Charges", "Salary Account", "Rent Account",
    ]
    STOCK_ITEMS = ["Parle-G Gold Biscuit 800 gm", "Tata Salt 1 kg", "Amul Butter 500 gm"]
    UNITS = ["PCS", "KG", "BOX", "LTR", "GM", "CTN"]
    VOUCHER_TYPES = ["Purchase", "Sales", "Payment", "Receipt", "Journal"]

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.company_name = self.company_name or self.COMPANIES[0]
        self._posted: List[str] = []

    def test_connection(self) -> TallyResult:
        return TallyResult(
            True,
            "MOCK connector active. No real Tally instance is involved — this "
            "mode exists so the rest of the workflow can be built and demonstrated.",
            mode="MOCK",
        )

    def list_companies(self):
        return list(self.COMPANIES)

    def list_ledgers(self):
        return list(self.LEDGERS)

    def list_stock_items(self):
        return list(self.STOCK_ITEMS)

    def list_units(self):
        return list(self.UNITS)

    def list_voucher_types(self):
        return list(self.VOUCHER_TYPES)

    def post_voucher(self, payload: VoucherPayload) -> TallyResult:
        xml_body = build_xml_for_payload(payload)
        path = self.export_xml(payload)
        voucher_id = f"MOCK-{uuid.uuid4().hex[:10].upper()}"
        self._posted.append(voucher_id)
        return TallyResult(
            True,
            "MOCK POST accepted. Nothing was written to a real Tally company; "
            "the generated XML was saved so you can import it manually.",
            mode="TEST_MODE" if self.test_mode else "MOCK",
            voucher_id=voucher_id,
            request_xml=xml_body,
            export_path=path,
            created=1,
            warnings=["Mock connector — no real accounting entry exists."],
        )

    def create_stock_item(self, name, unit, hsn="", gst_rate=0.0, stock_group="Primary"):
        self.STOCK_ITEMS.append(name)
        return TallyResult(True, f"MOCK: stock item '{name}' registered.", mode="MOCK")

    def create_ledger(self, name, group, gstin=""):
        self.LEDGERS.append(name)
        return TallyResult(True, f"MOCK: ledger '{name}' registered.", mode="MOCK")


# --------------------------------------------------------------------------
class FileExportConnector(TallyConnector):
    """
    For cloud installs and customers who prefer manual control.
    Produces an importable XML file and nothing else.
    """

    name = "file-export"

    def test_connection(self) -> TallyResult:
        return TallyResult(
            True,
            "File-export mode. The app will generate Tally-compatible XML for you "
            "to import via Gateway of Tally → Import → Vouchers.",
            mode="XML_EXPORT",
        )

    def list_companies(self):
        return [self.company_name] if self.company_name else []

    def list_ledgers(self):
        return []

    def list_stock_items(self):
        return []

    def list_units(self):
        return []

    def list_voucher_types(self):
        return []

    def preflight(self, payload: VoucherPayload) -> List[str]:
        return []  # nothing to verify; the user imports manually

    def post_voucher(self, payload: VoucherPayload) -> TallyResult:
        path = self.export_xml(payload)
        return TallyResult(
            True,
            "Tally-compatible XML generated. Import it in Tally: "
            "Gateway of Tally → Import → Vouchers → select this file.",
            mode="XML_EXPORT",
            request_xml=build_xml_for_payload(payload),
            export_path=path,
            voucher_id=payload.reference_id,
        )



class ExcelConnector(TallyConnector):
    """For customers who do not use Tally at all.

    There is no external accounting system here, so there is nothing to post
    to and nothing to wait for: this app is the book of record. Posting writes
    the bill to a workbook and the caller applies stock straight away, exactly
    as it would after Tally accepted a voucher.

    That is the important difference from FileExportConnector. An XML file has
    to be imported into Tally before the stock is real, so the bill waits in
    EXPORTED. A workbook is a report of something that already happened, and
    making the customer confirm an import that never happens would strand every
    bill they enter.
    """

    name = "excel"

    def test_connection(self) -> TallyResult:
        return TallyResult(
            True,
            "Excel mode. No Tally connection is needed — each posted bill is "
            "written to a workbook, and stock is kept by this app.",
            mode="EXCEL",
        )

    # No Tally to ask, so nothing to list and nothing to verify.
    def list_companies(self):
        return [self.company_name] if self.company_name else []

    def list_ledgers(self):
        return []

    def list_stock_items(self):
        return []

    def list_units(self):
        return []

    def list_voucher_types(self):
        return []

    def preflight(self, payload) -> List[str]:
        return []

    def post_voucher(self, payload) -> TallyResult:
        if isinstance(payload, LedgerVoucherPayload):
            from services.excel_export import append_bank_ledger_row

            path = append_bank_ledger_row(payload)
            return TallyResult(
                True,
                "Bank entry saved to an Excel ledger — no stock is affected.",
                mode="EXCEL",
                export_path=path,
                voucher_id=payload.reference_id,
            )

        from services.excel_export import build_bill_workbook

        path = build_bill_workbook(payload)
        return TallyResult(
            True,
            "Bill saved to an Excel workbook. Stock has been updated in this app.",
            mode="EXCEL",
            export_path=path,
            voucher_id=payload.reference_id,
        )


CONNECTOR_REGISTRY = {
    "mock": MockTallyConnector,
    "xml": TallyXMLConnector,
    "http": TallyXMLConnector,
    "file": FileExportConnector,
    "excel": ExcelConnector,
}

CONNECTOR_LABELS = {
    "mock": "Mock (development / demo — no real Tally)",
    "xml": "Tally HTTP XML Gateway (Tally open on this network)",
    "file": "Generate XML file for manual import",
    "excel": "No Tally — keep records in Excel",
}


def get_connector(mode: str = "mock", config: Optional[Dict] = None) -> TallyConnector:
    cls = CONNECTOR_REGISTRY.get((mode or "mock").lower(), MockTallyConnector)
    return cls(config or {})
