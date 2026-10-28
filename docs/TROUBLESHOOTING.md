# Troubleshooting

Grouped by where the problem shows up. Tally-specific errors are in
[TALLY_INTEGRATION.md](TALLY_INTEGRATION.md) §9.

---

## Windows setup (START.bat)

**Do I need to run START.bat again after updating?**
Running it again is safe — it detects the existing environment and database and
skips straight to launching. But it will *not* refresh your data: the seed step
only runs when no database exists. To clear demo data, close the app and use
**RESET_DEMO.bat** first, then START.bat.

**I updated the files but the app looks unchanged**
The old app is probably still running in its window. Close it (or press Ctrl+C)
and start it again — Streamlit does not reload files that were replaced on disk
while it was running.


**"Python was not found"** — even though you installed it
The *Add Python to PATH* box was not ticked during installation. Re-run the
Python installer, choose **Modify** (or reinstall), and tick it. Alternatively
Windows may be intercepting `python` and opening the Microsoft Store: turn that
off in *Settings → Apps → Advanced app settings → App execution aliases* by
switching off both **python.exe** entries.

**The window flashes open and closes instantly**
Something failed before the first `pause`. Open Command Prompt, `cd` to the
folder and run `START.bat` from there — the error stays on screen.

**"Could not create the environment"**
Usually folder permissions. Move the folder out of `C:\Program Files` (try
`C:\BillToTallyAgent` or your Desktop), or right-click START.bat → *Run as
administrator*.

**Installation fails while downloading**
No internet, or a firewall blocking `pypi.org`. On a locked-down office network
ask IT to allow `pypi.org` and `files.pythonhosted.org`.

**The window seems frozen on the first run**
Dependency installation takes 2–5 minutes and is quiet for long stretches. Leave
it. If it is still unchanged after 15 minutes, close it and run `INSTALL.bat`,
which prints more detail.

**Antivirus blocks it**
Some antivirus products flag scripts that create environments. Add the folder to
the exclusion list, or run `INSTALL.bat` once with antivirus paused.

**Other PCs cannot reach RUN_ON_NETWORK.bat**
Allow Python through Windows Defender Firewall on the **Private** profile, and
check both PCs are on the same network. Never open this to the internet without
a VPN.

**I want it to start automatically when Windows starts**
Press `Win+R`, type `shell:startup`, and put a shortcut to `START.bat` in the
folder that opens.

---

## Licence keys

**"This licence key is not valid"**
Almost always a typo. Dashes, spaces and lower case are all fine, and the app
already corrects the characters people confuse (`O`/`0`, `I`/`1`, `L`/`1`,
`U`/`V`), so the problem is usually a genuinely wrong or missing character. Read
it back against the Excel register.

**"This licence key has already been used"**
Each key activates one company. The message names the company holding it. If
that was a mistaken activation, issue a fresh key and mark the old one
`Cancelled` in your register.

**Every key suddenly stopped working**
`LICENSE_SIGNING_SECRET` in `.env` was changed. Keys are signed with it, so
changing it invalidates every key ever issued. Restore the previous value.

**The app is showing a renewal screen**
The licence expired. No data has been deleted — enter a new key on that screen
and everything comes straight back. Contact SN Softech Solutions on
**9993199719** for a key.

**Customer is moving to a new PC**
Copy `data/billtotally.db` across. The licence lives in the database, so it
travels with the data and no new key is needed.

**I need to run without a licence for testing**
Set `LICENSE_REQUIRED=false` in `.env`. Never ship a customer build with this
off.

---

## Demo account

**Where did the stock in Product master come from? I only uploaded one bill**
From `seed_demo.py`, not from your bill. The demo company is created with five
products carrying opening stock (120, 300, 40, 80 and 60). Two of them —
Britannia Marie Gold and Fortune Sunflower Oil — are not on the sample bill at
all, which is the quickest way to confirm it. Run `python seed_demo.py --reset
--empty` to rebuild the demo with every product at zero.

**I posted a bill but stock did not change**
Stock moves only when Tally accepts the voucher, never on upload or confirm.
Check the bill in Purchase History: if the status is not `Posted`, no stock was
meant to move. On the XML route the status stops at `Exported` on purpose —
stock waits until you confirm the import.


**How do I clear the demo between customers?**
**Settings → 🧪 Demo → Clear demo data**, or the button on the demo strip at
the top of the screen. It asks for confirmation first.

**Will demo data disappear while I am presenting?**
No. There is no timer by default — the demo is cleared only when you press the
button. If a previous build left `DEMO_RETENTION_MINUTES` above 0 in your
`.env`, set it to 0.

**A real customer account has no Clear button**
Correct, and deliberate. Only demo companies can be cleared; a real company's
records are permanent. If a customer's account was created from the demo by
mistake, see below.

**My data disappeared**
If you are signed in as `demo@snsoftech.local`, it was cleared with the button —
demo data is never deleted on a timer unless you set one. A purple banner at the
top says you are on the demo. Use a licensed account for real purchase records.

**A supplier I never uploaded is in the supplier list**
Look at its **Source** column. `Sample` means it was seeded with the demo so
that product matching has something to match against — "Shreeji Distributors"
and the seven sample products are all seeded. `Entered` means it came from a
bill. To start a demo with empty masters:
**Settings → 🧪 Demo → Remove sample data**, or seed with
`python seed_demo.py --reset --no-products --no-suppliers`.

**I want the demo to keep data for longer**
Raise `DEMO_RETENTION_MINUTES` in `.env`, or stop the sweep entirely by clearing
the demo flag:

```python
python -c "
from database.database import get_session
from database.models import Company
db = get_session()
c = db.query(Company).filter(Company.name=='SN Demo Traders').one()
c.is_demo = False
db.commit(); print('demo mode off')"
```

**A real customer was set up on the demo account**
Turn off the demo flag immediately with the snippet above, before the next sweep
runs. Then create them a proper company with their own licence key and move
their bills across.

---

## Installation and startup

**`streamlit: command not found`**
Streamlit installed for a different Python. Run `python -m streamlit run app.py`,
or reinstall with `python -m pip install -r requirements.txt`.

**`ModuleNotFoundError: No module named 'database'`**
You are not in the project folder. `cd` into `BillToTallyAgent` and run from
there — the app resolves imports relative to its own root.

**`ImportError: libGL.so.1` on Linux**
OpenCV needs system libraries:
`sudo apt install -y libgl1 libglib2.0-0`, or use `opencv-python-headless`
(already what `requirements.txt` specifies).

**Port 8501 already in use**
`streamlit run app.py --server.port 8502`

**The app starts but the browser shows nothing**
Check the terminal for a traceback. If it says the database is locked, close
other copies of the app — SQLite allows one writer.

---

## Login and users

**"Company name, your name and email are required" — but they are filled in**
This happens when a value on screen never reached the app. The usual cause is
the browser autofilling a field (autofill does not always notify the page), or
clicking the button while a field still has focus.

Both sign-in and sign-up now use a submit form, which sends every field
together, so this should no longer occur. If you do see it, the message names
the specific field — click into that field, retype the last character so the
app registers a change, then submit again.


**"This licence key has already been used" at sign up**
That key activated a different company. Keys are not transferable — one key,
one company. Use a fresh key from your register.

**Forgot the admin password**
Use **Forgot password** on the sign-in screen; the reset token is shown on
screen (this is a LAN app, there is no mail server). Tokens expire in 2 hours.

**Locked out completely**
Reset the password directly:

```python
python -c "
from database.database import get_session
from database.models import User
from utils.security import hash_password
db = get_session()
u = db.query(User).filter(User.email=='you@example.com').one()
u.password_hash = hash_password('NewPassw0rd')
db.commit(); print('done')"
```

**"Your role does not allow this"**
Operators cannot confirm or post. Change the role in **Settings → Users**, which
only an Admin can do. An Admin cannot remove their own admin rights — that guard
exists so a company can never end up with no administrator.

---

## Upload and extraction

**"File type not allowed"**
Only JPG, PNG, WEBP and PDF are accepted, and the file's actual content is
checked against its extension. Renaming a `.heic` to `.jpg` will be rejected —
convert it properly. iPhones can be set to shoot JPEG in
*Settings → Camera → Formats → Most Compatible*.

**"File exceeds the 15 MB limit"**
Raise `MAX_UPLOAD_MB` in `.env` **and** `maxUploadSize` in
`.streamlit/config.toml`; both apply. Better: photograph at a lower resolution —
above ~2200px the app downscales anyway and larger files just cost you API time.

**Extraction is slow**
Cloud OCR takes 5–20 seconds per page; multi-page PDFs are per page. Tesseract
is faster but much less accurate on tables. This is normal, not a hang.

**Everything comes back empty or nonsense**
1. Check **Settings → AI & OCR → Test engine** — a bad or expired API key
   usually shows up here immediately.
2. Confirm the provider is not still set to `mock` (mock always returns the same
   sample bill regardless of what you upload).
3. Look at **Reports → Error log** for the actual provider response.

**Accuracy is poor on real bills**
In order of impact:
1. Photograph the bill **flat, in even light, filling the frame**. This matters
   more than any setting.
2. Use the crop and straighten tools on the upload screen before processing.
3. Switch to a stronger provider (Gemini or Claude beat Tesseract by a wide
   margin on Indian tax invoices).
4. Handwritten bills will not work reliably on any engine. Use manual entry.

**Dates come out wrong**
Indian bills are `DD/MM/YYYY`; some software prints `MM/DD/YYYY`. The parser
prefers day-first and only swaps when the first number cannot be a day. Always
check the date on the review screen — it is one of the fields most worth your
two seconds.

---

## Validation

**"Invoice total mismatch: extracted ₹X vs calculated ₹Y"**
The line items do not add up to the printed total. Usually one line was misread.
Compare each line against the paper bill and correct the wrong one. The app will
not adjust a figure to make the total fit — that is the whole point.

Legitimate causes of a small gap: rounding (set a round-off ledger, raise
`ROUNDING_TOLERANCE`), freight or insurance printed only in the totals block, or
a discount applied to the invoice rather than the lines.

**"CGST and IGST cannot both apply"**
A misread tax column, or a genuinely unusual bill. Check the supplier's state
against yours: same state → CGST + SGST, different state → IGST.

**Tax scope shows UNKNOWN**
Neither GSTIN was readable and the tax columns were ambiguous. Fill in the
supplier GSTIN on the review screen, and set your own GSTIN in
**Settings → Company**.

**A line is flagged red at 64% confidence**
Working as designed. The engine is telling you it is unsure. Read that line off
the paper bill and correct or confirm it.

**"1 blocking issue" because a line was read at low confidence**
Open that line's expander, check every field against the paper bill, correct
anything wrong, then press **Save line**. Saving records that a person has
verified it, and the block clears — no override needed.

Override is only for when the app is wrong and you have checked the paper bill,
because the reason you type is stored in the audit trail against your name.
Arithmetic and GST errors never clear this way: those are facts about the
numbers, and they keep blocking until the numbers are right.

**"Overall extraction confidence is 58% — below the 70% threshold"**
Open the line that is flagged red, check it against the paper bill, and press
**Save line**. The invoice-level figure is recomputed from the lines, so
correcting the one doubtful line clears the invoice block too — you do not have
to re-save lines that were read correctly.

If two lines are doubtful, fix both.

**"Invoice looks inter-state but CGST/SGST was read" when both GSTINs are from
the same state**
Your own GSTIN is wrong or missing. The app compares the first two digits of
the supplier's GSTIN with yours: 22 is Chhattisgarh, 23 Madhya Pradesh, 27
Maharashtra. Set the correct one in **Settings → Company**. On the demo company
it should be `22ZZZZZ9999Z1Z9`.

A demo database seeded by an older build carries `23ZZZZZ...` (Madhya Pradesh),
which makes every Chhattisgarh supplier look inter-state. Fix it in Settings, or
run `RESET_DEMO.bat`.

**I need to post despite a blocking error**
Confirm with an override reason. It is stored in the audit log against your
name. Use it when you have checked the paper bill and the app is wrong — not to
get past a warning you did not read.

---

## Product matching

**The same product is matched differently each time**
Tick **Remember this mapping** when you correct a match. The alias is stored and
that supplier's spelling matches instantly from then on. This is the single
biggest speed-up available — a few bills of teaching and most lines auto-match.

**Wrong product is being auto-linked**
Lower `MATCH_AUTO_THRESHOLD` is *not* the fix — raise it (e.g. 0.95) so fewer
matches are automatic. Then correct the bad ones so aliases are learned.

**Everything shows as NEW PRODUCT**
Your product master is empty or the names are very different. Import your
catalogue in **Products → CSV import**, or create products as you go from the
review screen.

**Duplicate products are piling up**
Someone is clicking *Create product* instead of selecting the existing one.
Merge them in **Products**, and retrain the user — the dropdown is right above
the create button.

---

## Posting and stock

**"No module named 'openpyxl'" (or any other package)**
Close the app and double-click **START.bat**. It checks the packages on every
launch and installs anything missing by itself, so an update that adds a
feature brings its packages with it.

If you want to see what it found, run this in the application folder:

```
.venv\Scripts\python.exe check_deps.py --list
```

Your data is never touched by any of this — only program files are updated.

**My customer does not use Tally — can they still use this?**
Yes. In *Tally Connection* choose **No Tally — keep records in Excel**. Bills
are recorded in the app, stock is tracked there, and each posted bill is saved
as a workbook. The full purchase register downloads from **Reports → 📗 Excel
export**.

**In Excel mode, when does stock update?**
As soon as the bill is posted. With Tally the app waits for Tally to accept the
voucher, because Tally is the book of record. Without Tally this app is the
book of record, so there is nothing to wait for.

**Can I get a report for just one supplier?**
Yes — **Reports → 📗 Excel export → By supplier**, pick the supplier(s). Tick
*All dates* for their whole history, or untick it to limit the period. The
combined workbook also carries a **Supplier Summary** sheet with one row per
supplier whatever filter you used.

**Can I get a separate Excel file for each bill?**
Yes — choose **A separate file per bill (ZIP)** as the file layout. Each
workbook is named `Supplier_InvoiceNo_Date.xlsx`.

**Can I export only certain bills?**
**Pick specific bills** lets you choose them from a list showing date,
supplier, number and amount.

**Can I get one bill as Excel even though I use Tally?**
Yes — open the bill in *Purchase History* and press **Build an Excel copy**.

**A bill says "Exported" and stock has not changed**
That is correct. In file-export mode the XML has been written but Tally has not
seen it yet. Import the file in Tally, check the voucher in Day Book, then open
the bill in Review Bills and press **I have imported this in Tally**. Stock
updates at that moment.

If the import did not work, press **Import failed — send it back**; the bill
returns to Confirmed and you can fix the cause and export again.

**Can I import several bills into Tally in one go?**
The app makes one file per bill, and that is the safer way to work. Tally's
import is not all-or-nothing: if one voucher in a batch fails, the rest are
still created and Tally reports only a total count, so identifying the failed
one takes longer than the batching saved. If Tally is reachable on the network,
use HTTP mode instead and skip files entirely.

**Stock did not increase after posting**
Stock moves only when the post succeeded. Check **Purchase History** — if the
status is `FAILED`, nothing moved by design. Fix the cause and post again.

**Stock increased twice**
It should not be possible; posting is guarded by an idempotency key and a
`stock_applied` flag. If you see it, someone likely also adjusted stock manually
in **Products**. Check **Reports → Audit log** for `STOCK_ADJUSTED` entries.

**"This invoice has already been posted"**
Working as designed. To post a corrected version, cancel the original in Tally
and in the app, then process the corrected bill as a new one.

---

## Reports

**I added a bill but the report does not show it**
A report counts bills that are **entered**, which means `Posted` (sent to
Tally, or recorded in Excel mode) or `Exported` (XML written, waiting for you
to import it). Anything earlier is still yours to finish.

The report now names what is waiting and which button moves it on, for example:

> **1 bill uploaded, none of them entered yet.**
> - **1 × Review Required** — open Review bills, fix the flagged lines, then Confirm

So the usual sequence for a bill that is "missing" is: **Review Bills → fix any
red line → Confirm → Purchase History → Post to Tally**.

**Why is a Confirmed bill not in the report but is in the Excel export?**
Confirmed means you have checked the figures but nothing has been sent
anywhere. It is fine in a spreadsheet and wrong in the books, so the Excel
export includes it by default and the summaries do not. Untick it in the
**Status** box if you would rather leave it out.

**An option described in this manual is missing from my screen**
You are on an older build. Check **Settings → System → App version** and
*What is in this build* against the version you were sent. To update: close the
app completely (the old window keeps serving the old files), extract the new
zip **over** the existing folder so the files are replaced, then run
**START.bat**. Your `data\` folder and database are not touched by an update.

---

## Performance

**The app slows down after a few thousand bills**
Move to PostgreSQL (`DATABASE_URL=postgresql+psycopg2://...`). SQLite is fine
for a single shop but not for a busy multi-tenant install.

**Uploads folder is huge**
Bill images accumulate in `data/uploads/`. Archive older ones to cheap storage;
the database keeps the file hash and the extracted data, so history and
duplicate detection still work after images are moved.

---

## Logs

| Where | What |
|---|---|
| `data/logs/app.log` | Rotating application log (2 MB × 5) |
| **Reports → Error log** | Processing failures with tracebacks, markable resolved |
| **Reports → Audit log** | Every business action, exportable to CSV |
| **Purchase History → bill detail** | Per-bill audit trail plus the exact XML sent |

When reporting a problem to SN Softech Solutions (**9993199719**), include the
invoice number, what you expected, what happened, and the matching entry from
the error log.
