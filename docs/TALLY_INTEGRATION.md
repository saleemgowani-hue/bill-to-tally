# Tally Integration Guide

Everything you need to connect this agent to Tally Prime or Tally.ERP 9 — and
an honest account of what Tally can and cannot do.

---

## 1. How Tally integration actually works

Tally exposes an **XML gateway over HTTP**. When Tally is running with
connectivity enabled, it listens on a port (9000 by default) and accepts XML
requests: `Export Data` to read masters and reports, `Import Data` to create
vouchers.

Things people expect that are **not true**:

| Expectation | Reality |
|---|---|
| "Tally has a cloud API" | No. The gateway is local/LAN only. |
| "I can post to Tally from my server" | Only if your server can reach that PC on the LAN or through a VPN. |
| "Tally can be closed" | No. Tally must be running with the company open. |
| "The gateway is secured" | It is not. No authentication, no TLS. Never expose port 9000 to the internet. |

This is why the app ships three connector modes, and why **file export** is a
first-class option rather than a fallback.

---

## 2. Enabling the gateway

### Tally Prime

1. Press **F1** → **Settings** → **Connectivity**.
2. Open **Client/Server configuration**.
3. Set **Tally acts as** = `Both` (or `Server`).
4. Set **Port** = `9000`.
5. Accept, and keep Tally running with your company **open**.

### Tally.ERP 9

1. Press **F12** → **Advanced Configuration**.
2. Set **Tally acts as** = `Both`.
3. Set **Port** = `9000`.
4. Accept and restart Tally.

### Verify it before touching the app

Open a browser on the same PC and go to `http://localhost:9000`. You should get
a response from Tally (often an XML error page — that is fine, it proves the
listener is alive). If the browser cannot connect, Tally is not listening and
nothing else in this guide will work.

---

## 3. Choosing a connector mode

Set this in **Tally Connection → Connection**.

### Mock
No Tally involved. Every response is labelled `MOCK` and no accounting entry is
ever created. Use for demos, training and automated tests.

### Tally XML (HTTP) — the real integration
Posts vouchers directly. Requires network reach to the Tally machine.

- Same PC → host `localhost`, port `9000`
- Shop LAN → host = the Tally PC's IP (e.g. `192.168.1.7`), port `9000`
- Different site → **do not** port-forward. Use a VPN, or use file export.

### No Tally — Excel

For a customer who does not use Tally at all. Choose **No Tally — keep records
in Excel** in *Tally Connection*.

Confirming and posting a bill writes it to a workbook, and **stock updates
immediately**. There is no external accounting system to wait for: this app is
the book of record, so there is nothing to import and nothing to confirm
afterwards. Test mode does not apply, because nothing is being sent anywhere.

The customer's real deliverable is **Reports → 📗 Excel export**. Choose what
to export:

- **Date range** — everything between two dates
- **By supplier** — one or more suppliers, over all dates or a period
- **Pick specific bills** — choose them from a list

Then choose the shape:

- **One combined workbook** — five sheets:

  | Sheet | What it holds |
  |---|---|
  | Purchase Register | one row per bill, with tax split out |
  | Item Detail | one row per line item across every bill |
  | GST Summary | taxable value and tax grouped by rate |
  | Supplier Summary | one row per supplier, biggest first |
  | Stock Position | current stock and its value (optional) |

- **A separate file per bill (ZIP)** — one workbook per invoice, each named
  `Supplier_InvoiceNo_Date.xlsx`, for a customer who files bills individually.

Totals are live formulas, so an accountant who edits a figure sees the sheet
re-add itself. The GST sheet is a working summary, not a statutory return.

### File export
Generates a `.xml` file. Import it in Tally via
**Import → Vouchers → (select file)**. Use this when the app is hosted and the
customer's Tally is somewhere you cannot reach. It is slower but it always
works, and the customer keeps full control of what enters their books.

**Stock waits for you here.** After the file is generated the bill sits in
**Exported** status and no stock moves. Import the file, check the voucher in
Day Book, then press **I have imported this in Tally** on the review screen —
that is when stock updates. If the import failed, press **Import failed — send
it back** and the bill returns to Confirmed.

This matters: a generated file may never be imported, or the import may fail
halfway. If the app moved stock on export, its figures and Tally's would drift
apart silently and you would only notice at stock-taking.

**One bill per file.** Tally's import is not all-or-nothing — if one voucher in
a batch fails, the earlier ones are still created and Tally reports only a
count. Finding which of twenty failed costs more than batching saves. One file
per bill means you always know exactly what went in.

---

## 4. TEST MODE — read this before going live

`TALLY_TEST_MODE=true` (the default) means:

- the voucher XML is built exactly as it would be for a real post,
- it is displayed and downloadable,
- **nothing is sent to Tally** and no accounting entry is created.

Keep it on until you have:

1. generated XML for at least 5 real bills,
2. imported them manually into a **copy** of the customer's company,
3. checked in Tally that the party ledger, tax ledgers, stock items, quantities
   and the grand total all match the paper bill,
4. checked the stock summary moved by the right quantity.

Only then turn test mode off, and even then post one bill and verify it before
doing a batch. Vouchers written into a live company are the customer's
accounting records — treat the switch as a release, not a setting.

---

## 5. Ledger mapping

Nothing about your chart of accounts is hard-coded. **Tally Connection → Ledger
mapping** controls every name the voucher uses:

| Mapping key | Typical value | Used for |
|---|---|---|
| `voucher_type` | `Purchase` | The voucher type name in Tally |
| `purchase_ledger` | `Purchase Account` | The debit side of the purchase |
| `cgst_ledger` | `CGST Input` | Intra-state CGST |
| `sgst_ledger` | `SGST Input` | Intra-state SGST |
| `igst_ledger` | `IGST Input` | Inter-state IGST |
| `cess_ledger` | `Cess Input` | Cess, where charged |
| `round_off_ledger` | `Round Off` | Rounding difference |
| `discount_ledger` | `Discount Received` | Where discount is posted separately |
| `default_godown` | `Main Location` | Inventory allocation |
| `default_unit` | `PCS` | Fallback unit for new stock items |
| `supplier_ledger_group` | `Sundry Creditors` | Group for new supplier ledgers |
| `stock_group` | `Primary` | Group for new stock items |

**These names must match Tally exactly**, including spacing and case. Click
**Fetch masters** to pull the live list from Tally and pick from a dropdown
instead of typing — that removes the single most common cause of failed imports.

Per-supplier and per-product overrides are also supported: a supplier row
carries its own Tally ledger name, and a product row carries its own Tally stock
item name. Use these when the name in your app differs from the name in Tally.

---

## 6. The pre-check

Before any live post the app runs a **preflight** against Tally and refuses to
send if anything is missing:

- the company is open in Tally,
- the voucher type exists,
- the supplier ledger exists,
- the purchase and tax ledgers exist,
- every stock item on the bill exists,
- the units exist.

This turns a silent bad import into a clear message listing exactly what to
create. Run it from **Review Bills → Run pre-check** any time.

---

## 7. What the voucher looks like

For an intra-state purchase of ₹7,354.50:

```
Purchase Voucher   Date: 04-09-2026   Ref: SD/2026/1187
  Party (credit)      Shreeji Distributors        7,354.50
  Purchase (debit)    Purchase Account            6,510.00
    ├─ Parle-G Gold Biscuit 800 gm   24 PCS @ 95.00
    ├─ Tata Salt 1 kg                50 PCS @ 22.00  (disc 50.00)
    └─ Amul Butter 500 gm            12 PCS @ 265.00
  CGST Input (debit)                                422.25
  SGST Input (debit)                                422.25
```

Key XML details:

- `REMOTEID` carries our idempotency key, so re-importing the same file does not
  create a second voucher.
- `REFERENCE` is the supplier's invoice number.
- Inventory entries include `HSNCODE` and godown allocation.
- Zero-value tax ledgers are omitted rather than posted as zero.
- Inter-state bills use IGST only; intra-state use CGST + SGST. The scope is
  decided from the two GSTINs, falling back to the tax columns printed on the
  bill, and is reported as `UNKNOWN` rather than guessed.

---

## 8. Duplicate protection

Two independent layers:

1. **Before extraction** — the file hash, then supplier + invoice number, then
   supplier + date + amount. A suspected duplicate is held in `DUPLICATE` status
   and shown alongside the original for a human decision.
2. **At posting** — a SHA-256 idempotency key over company, supplier, invoice
   number, total and date, with a unique database constraint. Posting the same
   bill twice returns a clear refusal and does **not** touch stock.

---

## 9. Common errors

| Message | Cause | Fix |
|---|---|---|
| `Could not reach Tally at host:9000` | Tally closed, gateway off, firewall | Start Tally, enable connectivity, allow the port |
| `Ledger 'X' does not exist` | Mapping name ≠ Tally name | Fetch masters and pick from the dropdown |
| `Stock item 'X' does not exist` | Product not created in Tally | Create it, or map the product to the existing Tally name |
| `Company 'X' is not open` | Wrong company name, or not loaded | Open it in Tally; copy the exact name including the year suffix |
| `Unit 'X' does not exist` | Unit missing in Tally | Create the unit of measure in Tally |
| Voucher imports but stock does not move | Item not maintained in inventory | Enable inventory for that stock item |
| Totals differ by paise | Rounding | Set a round-off ledger and adjust `ROUNDING_TOLERANCE` |

---

## 10. Safety summary

- Stock is applied **only after** Tally accepts the voucher, never before. In
  file-export mode that means only after *you* confirm the import.
- Stock is applied **once**, guarded by `stock_applied` on the transaction.
- A failed pre-check posts nothing and moves nothing.
- Extracted figures are never rewritten to make a total reconcile.
- Overriding a blocking error requires a typed reason, which is stored in the
  audit log against the user who typed it.
