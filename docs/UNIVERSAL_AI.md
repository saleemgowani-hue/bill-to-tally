# Universal AI Bill-to-Tally — what changed and how it works

This upgrade turns the Bill-to-Tally Agent into the **Universal AI Bill-to-Tally
Agent**: one engine for every industry and every kind of business document,
with no per-supplier templates.

## 1. Universal AI engine (Google Gemini)

Select it in **API Settings → Use Universal AI engine** (setting `ocr_provider = universal`).

| Feature | How |
|---|---|
| Any layout, any industry | One universal prompt + schema (`services/universal_schema.py`). No industry or supplier is hard-coded. |
| Long / multi-page PDFs | Read in groups of *Pages per AI request* (default 4) and merged in page order. Every item row keeps its page number. |
| Never lose rows | If Gemini's answer is cut off at its output limit, that group is split in half and asked again. A single page that is still too long is reported as an error, never silently shortened. |
| No fixed model name | Default `gemini-flash-latest` (Google's alias for the current Flash model). **Check key** fetches the live list of models the key can use. |
| Key security | Key sent only in the `x-goog-api-key` header, stored encrypted per company, shown masked (`AIza••••••••1234`), never logged. |
| Retry | Network errors and 429/5xx are retried with backoff (2s, 4s, 8s). Bad key, missing model, quota exhausted and usage limit stop at once. |
| Offline fallback | When Gemini cannot be used, the bill is read with RapidOCR (or Tesseract) and clearly marked *"read with the offline engine — check every line"*. Gemini itself always needs internet. |

### Key modes

* **SN Softech central key** — customers enter nothing.
  * SaaS server: the server's `GEMINI_API_KEY` (environment only, never in the database or UI).
  * Customer laptop: through the SN Softech Cloud AI proxy (`ocr_proxy/`) with the customer's licence key — the real key never leaves SN Softech's server.
  * Monthly request limit per customer: `api_monthly_limit` setting (default `CENTRAL_API_MONTHLY_LIMIT`). When used up, extraction falls back to offline OCR with a clear message.
* **My own Gemini key** — validated against Google before saving; never limited by SN Softech.

### Usage and cost

Every Gemini request is stored in the `api_usage` table: company, user, model,
key mode, pages, prompt/output tokens (from Google's own `usageMetadata`),
estimated cost in ₹ (rates in `.env`: `GEMINI_PRICE_INPUT_PER_M_USD`,
`GEMINI_PRICE_OUTPUT_PER_M_USD`, `USD_TO_INR`), and time taken.
**API Settings → Usage** shows the month and the last 100 requests.

## 2. Universal schema and document types

The AI also returns: document type, seller *and* buyer, place of supply,
reverse charge, PO number, e-way bill, transport details, payment terms,
free quantity, discount %, per-line tax rates, freight, packing, other charges,
amount in words, balance payable, and a confidence per field.

These are saved on the invoice (`document_type`, `freight`, `packing_charges`,
`other_charges`, `universal_json`) — the existing screens keep working
unchanged. `build_universal_document()` produces the full common JSON
(`document_type, supplier, customer, invoice_details, items, tax_summary,
charges, totals, extraction_confidence, validation_errors, source_document`).

`services/document_classifier.py` recognises: purchase invoice, sales invoice,
expense bill, debit note, credit note, transport bilty, delivery challan,
payment receipt, bank document, other. It cross-checks the AI with printed text
("CREDIT NOTE", "Lorry Receipt" …) and with your own GSTIN (if the *seller* is
you, it is a sales invoice). A bilty or challan is **never** forced into a
purchase voucher — the upload screen shows the detected type and the suggested
action, and the bill only sits in review until someone decides.

## 3. Validation additions

* Freight / packing / other charges are part of the total check.
* Amount in words (Indian numbering — crore, lakh, paise) is compared with the grand total.
* Invoice dates in the future, or more than a year old, are flagged.
* Final status on every bill: **Error → Needs Review → Verified → Ready for Import** (after a user confirms).
* The validator only reports. It never edits a figure.

## 4. Tally

* **New voucher types:** Journal, Contra, Debit Note, Credit Note
  (`AccountingVoucherPayload`). An unbalanced voucher is refused before any XML is produced.
* **Charges in vouchers:** freight → *Freight Inward* (Direct Expenses), packing → *Packing Charges*,
  other → *Other Charges* (Indirect Expenses); on sales the *… Recovered/Outward* ledgers under
  Indirect Incomes. Ledger names are editable in Tally mappings (`freight_ledger`,
  `packing_ledger`, `other_charges_ledger`).
* **Auto-create masters (on by default):** before posting, any missing ledger, unit or stock item
  is created in Tally under the right group, and the list is written to the audit log
  (`TALLY_MASTERS_AUTO_CREATED`). Works for Purchase, Sales and Bank (Payment/Receipt)
  vouchers. Bank: the bank ledger goes under *Bank Accounts*, a matched supplier under
  *Sundry Creditors*, a customer under *Sundry Debtors*, a ledger typed by hand (Rent,
  Bank Charges, Salary …) under *Indirect Expenses* on a payment and *Indirect Incomes*
  on a receipt. Switch off in **API Settings → Options**.
* **Products are created automatically:** a bill line with no similar product in Products
  is added to Products (name, HSN, unit, GST %, rate; zero opening stock) and linked at
  once — on Purchase, Sales and Excel import, and on bills saved earlier when they are
  opened or confirmed. The bill wording is remembered, so the next bill links straight
  away. A line that looks like an existing product is NOT duplicated: it is shown for a
  person to pick. Switch off in **API Settings → Options**.
* **Voucher numbers:** Tally Connection → *Voucher numbers* — for each of Purchase, Sales,
  Payment, Receipt, Journal, Contra, Debit Note, Credit Note choose *Tally decides*,
  *My own series* (prefix + start number + digits + suffix, e.g. `PUR/0101/26-27`) or
  *Same as bill number*. A number is used only after Tally accepts the voucher; Test Mode
  never uses one. Tally keeps the number only if the voucher type's numbering method is
  Manual or Automatic (Manual Override).
* **Bug fixed:** Sales and Payment/Receipt vouchers used `<n>` instead of `<NAME>` in bill
  allocations, so Tally ignored the bill reference. Now `<NAME>`.

## Still to come (next phases)

* Tally Bridge Agent (Windows) so the online SaaS can post into a customer's local Tally.
* Super Admin panel (all customers, central key, limits, usage, logs).
* Roles: Super Admin / Customer Admin / Accountant / Operator.
* Batch processing page, 5-sheet Excel export with filters.
* Packaging and testing report against a real TallyPrime company.

## Must be verified on a real TallyPrime before live use

The XML follows Tally's documented import format and passes the automated
tests against the mock connector. Debit/Credit Note and Contra vouchers, GST
classification on auto-created duty ledgers, and charge ledgers **must be
test-posted into a TallyPrime test company** before customers use them.
