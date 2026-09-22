# AI Bill-to-Tally Stock Update Agent

**SN Softech Solutions** — *Smart Software Solutions for Growing Businesses*
Support: **9993199719**

Photograph a purchase bill → AI reads it → a human verifies it → a purchase
voucher goes into Tally → stock updates. Nothing reaches your books without
someone approving it first.

---

## The one rule this software follows

> **Accounting data is never changed silently.**

The agent extracts, checks the arithmetic, matches products and *shows you what
it found*. If a total does not reconcile it says so in red and refuses to
proceed — it does not "fix" the number to make things add up. Every posting is
a deliberate click by someone with the right role, and every click is written
to an audit log.

---

## Quick start

### Windows — just double-click

**`START.bat`** does everything: finds Python, builds an isolated
environment, installs dependencies, generates a security key, creates the
database and launches the app. First run takes a few minutes; after that it
starts in seconds.

| File | What it does |
|---|---|
| **START.bat** | Set up if needed, then run. **This is the one to double-click.** |
| **INSTALL.bat** | Set up only, and verify with the test suite. For preparing a PC in advance. |
| **RUN.bat** | Start the app (already installed). |
| **RUN_ON_NETWORK.bat** | Start it so other PCs on the shop LAN can use it. Shows the address to give staff. |
| **BACKUP.bat** | Copy the database, config and bill images into `backups\`. Run daily. |
| **RESET_DEMO.bat** | Rebuild the demo company — with stock, with zero stock, or with no products. |
| **GENERATE_KEYS.bat** | VENDOR ONLY: mint licence keys into the Excel register. Do not ship to customers. |

If Python is missing, START.bat says so and points at the download. Install
Python 3.10 or newer and **tick "Add Python to PATH"** on the first installer
screen — that checkbox is the single most common cause of setup trouble.

### Linux / macOS

```bash
pip install -r requirements.txt
cp .env.example .env          # then set SECRET_KEY to a long random string
python seed_demo.py
streamlit run app.py
```

### Demo seeder options

The demo company ships with 5 products carrying opening stock, because
product matching cannot be demonstrated against an empty master and stock
figures need something to move from. If you would rather start clean:

```bash
python seed_demo.py --empty         # same 5 products, stock 0
python seed_demo.py --no-products   # no products at all
python seed_demo.py --reset         # delete the demo company and rebuild it
python seed_demo.py --no-wipe       # keep entered data (no 60-minute sweep)
```

Flags combine: `python seed_demo.py --reset --empty` rebuilds the demo with
everything at zero. `--reset` removes the demo company and every row belonging
to it, and touches no other company.

Open <http://localhost:8501> and sign in:

| | |
|---|---|
| Email | `demo@snsoftech.local` |
| Password | `Demo@1234` |

Out of the box it runs with the **mock** OCR engine and the **mock** Tally
connector, so you can click the entire workflow without an API key and without
touching a real Tally company.

> The demo company is wiped on a timer: anything you enter there is deleted
> after 60 minutes. That is deliberate — see [Licensing](docs/LICENSING.md).

To watch the whole pipeline run in your terminal:

```bash
python demo_end_to_end.py
```

---

## The workflow

```
 Upload / camera
        │
        ▼
 ┌──────────────┐   OCR + AI extraction, per-field confidence
 │  EXTRACTED   │
 └──────┬───────┘
        ▼
 ┌──────────────┐   arithmetic checks, GST checks, duplicate check,
 │REVIEW REQUIRED│  product matching — everything questionable is flagged
 └──────┬───────┘
        ▼   ← a human edits and approves (Manager or Admin)
 ┌──────────────┐
 │  CONFIRMED   │
 └──────┬───────┘
        ▼   ← pre-check against live Tally masters, then post
 ┌──────────────┐
 │    POSTED    │   voucher in Tally + stock updated + audit written
 └──────────────┘
```

A bill can also land in **DUPLICATE**, **FAILED** or **CANCELLED**. Stock moves
**only** after Tally accepts the voucher, and only once — re-posting the same
bill is blocked by an idempotency key.

---

## Licensing

Creating a company account requires a licence key. Keys look like
`SNST-YKPQ-3W7M-XZ2N-8HJD` and are validated **offline**, so a shop PC with no
internet can still activate.

| Plan | Validity |
|---|---|
| Yearly | 365 days |
| Monthly | 30 days |
| Trial | 14 days |

The key is entered at **sign up** — no key, no account. After that, users sign
in with just their email and password; the key is not asked for again.

One key activates one company, and another company's key is refused. When a
licence expires the app is replaced by a renewal screen — **no data is deleted
or hidden**, and entering a new key there restores everything at once. A licence
can also be renewed any time from **Settings → Licence**. Renewing early extends
from the current expiry, so no paid days are lost.

To mint keys (vendor only):

```bash
python generate_keys.py                 # 25 yearly + 25 monthly -> Excel register
python generate_keys.py --monthly 100
python generate_keys.py --check SNST-...
```

Full detail — issuing, renewals, demo behaviour, recovering from mistakes — is
in **[docs/LICENSING.md](docs/LICENSING.md)**.

---

## Screens

| Screen | What it does |
|---|---|
| **Dashboard** | 8 colour-coded KPI tiles, monthly purchase chart, status mix, what needs attention, top suppliers, low stock, recent activity |
| **Upload Bill** | Multi-file upload, camera capture, manual entry; image straighten/crop/enhance |
| **Review Bills** | The verification screen — edit every field, resolve matches, confirm, post |
| **Products** | Product master, CSV import, learned name mappings, stock movements |
| **Suppliers** | Supplier master with Tally ledger names and purchase totals |
| **Tally Connection** | Connector setup, ledger mapping, fetch live masters, test connection |
| **Purchase History** | Filterable history, per-bill detail, exact XML sent, full audit trail |
| **Reports** | Purchase summary, GST summary, stock valuation, audit log, error log |
| **Settings** | AI provider + API key, thresholds, company details, users and roles, licence status and renewal |

---

## Configuring the AI / OCR engine

Set it per company in **Settings → AI & OCR**, or globally in `.env`.

| Provider | Value | Notes |
|---|---|---|
| Mock | `mock` | Fixed sample bill. No key needed. Use for demos and tests. |
| Google Gemini | `gemini` | Recommended. Fast, cheap, strong on Indian invoices. |
| OpenAI | `openai` | `gpt-4o-mini` or better. |
| Anthropic Claude | `claude` | Strong on messy handwriting and poor scans. |
| Tesseract | `tesseract` | Offline, free. Text only — much lower accuracy on tables. |
| PaddleOCR | `paddle` | Offline. Better than Tesseract on layout. |

API keys entered in Settings are **encrypted at rest** (Fernet, derived from
`SECRET_KEY`) and shown masked. Keys never appear in logs.

The cloud providers send the bill image to that provider. If your customer
cannot allow that, use Tesseract or PaddleOCR and expect to correct more fields
by hand.

---

## Connecting to Tally

Full detail is in **[docs/TALLY_INTEGRATION.md](docs/TALLY_INTEGRATION.md)**.
The short version:

1. In Tally Prime: **F1 → Settings → Connectivity → Client/Server configuration**
   → set *Tally acts as* **Both**, port **9000**.
2. Keep the company open in Tally. Tally must be running.
3. In this app: **Tally Connection → Connection**, choose **Tally XML (HTTP)**,
   enter host/port, click **Test connection**.
4. Map your ledgers in **Tally Connection → Ledger mapping**. Nothing is
   hard-coded — purchase ledger, tax ledgers, round-off, godown and voucher type
   all come from this mapping.
5. **Leave TEST MODE on** until you have imported a few vouchers and checked
   them in Tally. In test mode the XML is generated and shown but nothing is
   sent.

Three connector modes are available:

- **Mock** — no Tally at all. Every result is labelled `MOCK`.
- **Tally XML (HTTP)** — the real integration, over Tally's XML gateway. Stock
  moves as soon as Tally accepts the voucher.
- **File export** — writes a `.xml` file you import via *Import → Vouchers*.
  Use this when Tally is on a machine this app cannot reach. The bill waits in
  **Exported** and **stock does not move** until you confirm the import, because
  writing a file is not the same as Tally accepting it.

Export one bill at a time. Tally's import is not all-or-nothing: in a batch of
twenty, if one voucher fails the other nineteen are still created and you only
get a count, so working out which failed costs more time than the batching
saved.

> Tally has no cloud API. The HTTP gateway is LAN-only. If Tally runs on a
> different site, use file export or a VPN.

---

## Testing with a sample bill

Four ready-made demo bills ship in `samples/`, from four different suppliers,
all dated **01-09-2026** so they import into Tally's educational mode too. They
work with the mock OCR provider, so you can demo with no API key:

| File | Supplier | Total | Shows |
|---|---|---|---|
| `demo_bill_1_balaji.png` | Shree Balaji Traders | ₹6,890.40 | a clean bill, everything matches |
| `demo_bill_2_sharda.png` | Maa Sharda Agencies | ₹8,231.00 | line discount, round-off, new product |
| `demo_bill_3_nagpur.png` | Nagpur Trading Company | ₹7,118.00 | out-of-state, IGST |
| `demo_bill_4_gupta.png` | Gupta Kirana Bhandar | ₹15,201.00 | a line read at 58%, forces review |

A demo script is in **[docs/DEMO_GUIDE.md](docs/DEMO_GUIDE.md)**.

The older single bill is still there as `samples/sample_purchase_bill.png`.

1. Sign in and go to **Upload Bill**.
2. Upload the sample (with `mock` selected the extraction is deterministic).
3. You land on **Review Bills**. Note that the *Amul Butter* line is
   deliberately low-confidence (64%) and is flagged red — the bill cannot be
   confirmed until you look at it.
4. Link any unmatched product, tick **Remember this mapping** so the next bill
   from that supplier matches instantly.
5. **Confirm purchase**, then **Post to Tally** (mock/test mode).
6. Check **Products** — stock has gone up. Check **Purchase History** — the
   exact XML and the full audit trail are there.

Run the automated suite:

```bash
python -m pytest tests/ -q      # 227 tests
python tests/smoke_pages.py     # renders all 9 screens headlessly
python tests/smoke_licence.py   # checks the licence gate and expiry block
```

---

## Project layout

```
BillToTallyAgent/
├── app.py                   # entry point, auth, sidebar router
├── seed_demo.py             # demo company + sample products
├── generate_keys.py         # VENDOR: mint licence keys into an Excel register
├── demo_end_to_end.py       # runs the whole pipeline in the terminal
├── app_pages/               # one module per screen
├── services/
│   ├── ocr_service.py       # provider abstraction (mock/tesseract/paddle/gemini/openai/claude)
│   ├── ai_invoice_parser.py # prompt, strict JSON schema, coercion, repair
│   ├── product_matcher.py   # alias → barcode → SKU → weighted fuzzy
│   ├── invoice_validator.py # arithmetic, GST, confidence, duplicates
│   ├── tally_connector.py   # XML build + HTTP gateway + mock + file export
│   ├── purchase_service.py  # orchestration, confirm, post, stock, idempotency
│   ├── license_service.py   # key generation, offline validation, activation
│   ├── demo_service.py      # 60-minute wipe for demo companies
│   └── audit_service.py
├── database/                # SQLAlchemy models + session/tenant helpers
├── utils/                   # config, security, image tools, logging, UI helpers
├── tests/                   # pytest suite
├── docs/                    # Tally integration, licensing, demo guide, troubleshooting
└── samples/                 # 4 demo bills + the data the mock provider returns
```

The screen modules live in `app_pages/`, not `pages/`, on purpose: Streamlit
auto-routes anything inside a `pages/` folder, which would bypass the login
check. Routing is done explicitly in `app.py`.

---

## Multi-tenancy

Every business table carries a `company_id`, and queries go through
`scoped_query()`, which raises if a model has no `company_id` or the id is
missing. One customer's data cannot appear in another's screens.

Roles:

| Role | Upload | Edit | Confirm | Post to Tally | Users & settings |
|---|---|---|---|---|---|
| Operator | ✅ | ✅ | ❌ | ❌ | ❌ |
| Manager | ✅ | ✅ | ✅ | ✅ | ❌ |
| Admin | ✅ | ✅ | ✅ | ✅ | ✅ |

---

## Deployment

**Single shop (simplest).** Run it on the same Windows PC as Tally:

```bash
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

Other machines on the shop LAN reach it at `http://<pc-ip>:8501`. SQLite is
fine here.

**Multi-customer SaaS.** Switch to PostgreSQL and put it behind a reverse proxy
with TLS:

```
DATABASE_URL=postgresql+psycopg2://user:password@host:5432/billtotally
```

Then each customer needs either Tally on their own LAN with **file export**, or
a VPN/agent back to their Tally machine. Do not expose port 9000 to the
internet — the Tally gateway has no authentication.

Before going live: set a strong `SECRET_KEY`, change the demo password, turn
`TALLY_TEST_MODE` off only after verifying vouchers, and back up the database
(and `data/uploads/`) on a schedule.

---

## Known limitations

- **Handwritten bills** are unreliable on every engine. Expect manual entry.
- **Very poor photos** (blur, glare, heavy shadow) drop accuracy sharply. Use
  the crop/straighten tools before processing; better still, photograph flat in
  good light.
- **The Tally gateway is LAN-only.** No cloud API exists.
- **Voucher format varies** between Tally setups (batch/godown tracking, tax
  ledger naming). Test in test mode against *your* company before going live.
- **The GST report is a working summary, not a statutory return.** Do not file
  from it.
- **Barcode reading** needs `pyzbar` plus the zbar library; without it the app
  falls back to OpenCV and reads fewer symbologies.
- **No OCR engine is 100% accurate.** The human verification step is not
  optional and should not be trained away.

---

## Recommended next steps

1. Run 30–50 real bills from one supplier in test mode and measure how often
   fields need correcting. That number tells you which OCR provider to sell.
2. Add supplier-specific extraction hints once you see repeat formats — the
   biggest accuracy win available.
3. Add e-invoice (IRN/QR) parsing: where a supplier sends a signed e-invoice,
   the QR gives you exact data with no OCR at all.
4. Add a WhatsApp intake so shop staff can send a bill photo from the counter.
5. Move to PostgreSQL and add scheduled backups before the first paying
   customer.

---

© SN Softech Solutions. Built for resale as a product.
