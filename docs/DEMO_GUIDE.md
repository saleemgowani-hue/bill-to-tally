# Demo Guide

Four ready-made purchase bills for showing the agent to a customer, in
`samples/`. Every one is dated **01-09-2026** so it imports into Tally's
educational mode as well as a licensed Tally.

| File | Supplier | Total | Lines | What it shows |
|---|---|---|---|---|
| `demo_bill_1_balaji.png` | Shree Balaji Traders, Raipur | ₹6,890.40 | 3 | A clean bill. Every line matches the product master. |
| `demo_bill_2_sharda.png` | Maa Sharda Agencies, Bhilai | ₹8,231.00 | 4 | A line discount, a round-off, and one unknown product. |
| `demo_bill_3_nagpur.png` | Nagpur Trading Company, Maharashtra | ₹7,118.00 | 2 | Out-of-state supplier — IGST instead of CGST/SGST. |
| `demo_bill_4_gupta.png` | Gupta Kirana Bhandar, Raipur | ₹15,201.00 | 5 | One line read at 58% confidence — forces the review step. |

These work with the **mock** OCR provider, so you can demo with no API key and
no internet. The mock provider recognises each bill by its layout and returns
that bill's data.

---

## Clearing between customers

After a demo, open **Settings → 🧪 Demo** (or use the **Clear demo data** button
on the strip at the top of any screen) and clear it. The next prospect sees a
fresh install.

**Removed:** every bill from the demo, its lines, Tally records, stock
movements and audit trail, plus any product or supplier created while
demonstrating.

**Kept:** the company, its users, its licence, and the seven products it was
set up with — each back at its opening stock.

There is **no timer**. Nothing disappears while you are presenting; the demo is
cleared when you decide it is finished. (An unattended public demo can be put
on a timer with `DEMO_RETENTION_MINUTES` in `.env`.)

### The sample products and supplier

The demo is seeded with seven products carrying opening stock, and one supplier
called **Shreeji Distributors**. Nobody uploaded them: product matching cannot
be demonstrated against an empty master, and stock figures only visibly move if
there is something to add to.

They are labelled **Sample** in the Source column on Products and Suppliers, so
a customer asking "where did that come from?" gets an answer on the screen.

To run a demo with completely empty masters, either:

* **Settings → 🧪 Demo → Remove sample data** (anything already used by a bill
  is kept, so nothing is left pointing at a deleted row), or
* seed without them: `python seed_demo.py --reset --no-products --no-suppliers`

Matching will then find nothing until products exist — which is itself worth
showing, because it is what the customer's own first day looks like.

### Real customer accounts are different

A company created by signing up with a licence key is a **real** company. It
has no demo banner, no Demo tab and no clear button anywhere, and its data is
permanent. The clearing code refuses a non-demo company outright, so a
customer's purchase records cannot be cleared from this software at all.

Demo and real companies are separate rows with separate data. Clearing the demo
cannot reach a real company's bills, products or stock — this is covered by
tests, not just by intent.

---

## Before the customer arrives

```bash
python seed_demo.py --reset
```

Sign in as `demo@snsoftech.local` / `Demo@1234`. Seven products with opening
stock are created, so matching has something to match against. Add
`--no-products --no-suppliers` if you would rather start from nothing.

Sanity-check the build before a customer sees it: **Settings → System** should
show the version you were sent.

Check **Tally Connection** is on **Mock** with test mode ON, unless you intend
to show a real Tally import.

---

## A 10-minute demo that lands

**1. Start with the clean bill (Balaji).** Upload it. Point out that the AI read
the supplier, GSTIN, invoice number, date and every line, and that all three
products matched the master automatically. Confirm and post. Show the stock
figures moving in **Products**.

*The point: what used to be ten minutes of typing is now twenty seconds.*

**2. Then the messy one (Gupta).** Upload it. The Aashirvaad Atta line comes up
red at 58%. The bill **cannot be confirmed** until someone opens that line and
checks it. Open it, compare with the paper bill, press Save line, re-validate —
the block clears.

*The point, and this is the one that sells to an accountant: the software knows
what it is unsure about, and refuses to put a guess into your books.*

**3. The out-of-state bill (Nagpur).** Upload it. It picks up IGST rather than
CGST/SGST because the supplier's GSTIN starts with 27 and yours with 22.

*The point: it understands GST, it is not just reading text.*

**4. Upload the Balaji bill a second time.** It is caught as a duplicate and
blocked from posting.

*The point: the same bill cannot be paid or stocked twice by accident.*

**5. Finish on the discount bill (Sharda).** One product is unknown. Link it to
an existing product or create it, tick **Remember this mapping**, and explain
that the next bill from that supplier will match instantly.

*The point: it gets faster the more you use it.*

**6. Close on Purchase History.** Show the audit trail on any bill — who
uploaded it, who confirmed it, what was changed, the exact XML sent to Tally.

*The point: every figure can be traced back to a person and a photograph.*

---

## Importing into Tally during a demo

Educational mode only accepts vouchers dated the **1st, 2nd or 31st** of a
month, which is why these bills are dated 1 September.

Before importing, create these in the Tally company or the import will fail
with *"Referenced master is missing"*:

- **Ledgers:** the supplier (under Sundry Creditors), Purchase Account,
  CGST Input, SGST Input, IGST Input, Round Off
- **Unit:** PCS, and LTR for the sunflower oil
- **Stock items:** every product name on the bill, spelled exactly as the app
  sends it

Run **Run pre-check** in the app first — it lists whatever is missing before
you try. Names must match Tally exactly, including hyphens and capitals.

---

## Honest limits, worth knowing before a customer finds them

**The mock provider is not reading the bill.** It recognises which of the four
shipped images it is looking at and returns the matching data. Upload a
customer's own bill with the mock provider and it will fall back to a built-in
sample — the warning on screen says so. For a real bill, switch to Gemini,
OpenAI or Claude in **Settings → AI & OCR** and enter an API key.

If a customer asks to try their own bill, say yes and switch providers. It is a
far stronger demo than the canned one, and refusing looks evasive.

**Matching is by layout, so a photograph of a printed demo bill may not be
recognised.** Upload the PNG file itself.

---

## Regenerating the bills

```bash
python samples/make_sample_bills.py
```

Edit `BILLS` in that script to change suppliers, products or amounts. It
recomputes every total, redraws the images and rewrites `sample_bills.json`
with fresh fingerprints — the images and the data can never drift apart.

Run `python -m pytest tests/test_sample_bills.py -q` afterwards; it checks that
every bill still reconciles and that each image still returns its own data.
