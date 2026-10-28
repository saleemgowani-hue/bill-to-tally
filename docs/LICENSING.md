# Licensing Guide

How licence keys work, how to issue them, and how to handle renewals.
This document is for **SN Softech Solutions** — not for customers.

---

## 1. How a key works

```
SNST-YKPQ-3W7M-XZ2N-8HJD
^^^^ ^                ^^^^^^
|    |                signature
|    plan: Y = yearly, M = monthly, T = trial
fixed prefix
```

A key is validated **offline**. There is no licence server, because the PC
running this software is usually the same one running Tally, and that machine
often has no internet at all. The signature is an HMAC over the rest of the key,
computed with `LICENSE_SIGNING_SECRET`. The app recomputes it and compares.

Consequences worth understanding:

- A customer can activate a key with no internet connection.
- Nobody can invent a working key without your secret — random guesses fail.
- Changing a character anywhere in a key invalidates it, so a mistyped key is
  rejected rather than silently accepted.
- The plan is inside the signed portion, so a monthly key cannot be edited into
  a yearly one.

**Anyone who has `LICENSE_SIGNING_SECRET` can mint unlimited keys.** Keep it out
of customer builds and out of any repository you share.

---

## 2. Issuing keys

```bash
python generate_keys.py                    # 25 yearly + 25 monthly -> Excel
python generate_keys.py --yearly 50        # 50 yearly only
python generate_keys.py --monthly 100 --csv
python generate_keys.py --trial 5          # 14-day trials
python generate_keys.py --check SNST-...   # verify a single key
```

Output:

| File | Purpose |
|---|---|
| `SN_Softech_Licence_Keys.xlsx` | The register you work from — keys plus columns to track who bought what |
| `keys_issued_log.csv` | Append-only record of every key ever generated. Never edit it. |

The Excel file has a **Summary** sheet with live formulas counting sold and
unsold keys, and one sheet per plan. Yellow columns are yours to fill in:
Status, Customer Name, Contact, Sold On, Activated On, Expires On, Notes.

Generating more keys later overwrites the Excel file but **appends** to the CSV
log. If you have filled in customer details, rename your working copy first or
pass `--out` a different filename.

---

## 3. Selling and activating

1. Give the customer one key.
2. They enter it on the **Sign up** screen. Without a valid key no account is
   created — the check runs before anything is written, so a bad key never
   leaves a half-made company behind.
3. After sign up the key is not asked for again at login. Users sign in with
   their email and password; the licence lives with the company.
4. Validity is counted from the day they **activate**, not the day you sold it.
   A key sitting in your Excel sheet is not losing days.
5. One key activates exactly **one company**. Trying it on a second company is
   refused, and the message names the company that already has it.

Mark the row `Sold`, then `Activated`, so you always know what is outstanding.

---

## 4. Renewals

Send a new key. The customer enters it in either place:

- **Settings → Licence → Activate**
- the block screen that replaces the app if they have already been locked out

Renewing early does not waste days. A new key extends from the **current expiry**
if that is still in the future, and from today if it has already passed. So a
customer with 40 days left who activates a yearly key gets 405 days, not 365.

---

## 5. What expiry actually does

When a licence runs out:

- the app is replaced by a renewal screen,
- **no data is touched, deleted or hidden**,
- the customer can still sign out,
- entering a valid key restores everything immediately.

This matters when you explain it to a worried customer: an expired licence is a
locked door, not a deleted business.

Ten days before expiry (`LICENSE_WARN_DAYS`) the sidebar badge turns amber, and
the dashboard pill shows the countdown. That is your cue to call them.

---

## 6. Demo accounts

`python seed_demo.py` creates a demo company with a 14-day trial key and marks
it as a demo. Anything entered in a demo company is deleted once it is older
than `DEMO_RETENTION_MINUTES` (60 by default), and the seeded products are reset
to their original stock levels.

Never deleted: the company, its users, its settings, or the products and
suppliers created at seed time. The demo always looks freshly installed rather
than empty.

A purple banner tells the visitor this is happening and roughly when. Use the
demo for exhibitions and walk-in enquiries — and make sure a real customer is
never handed the demo login by mistake, since their records would disappear
within the hour.

---

## 7. Recovering from mistakes

**A customer lost their key.** Look it up in `keys_issued_log.csv` by the batch
and date you sold it. If they have already activated it, the app shows a masked
version in Settings → Licence; the full key is only in your records.

**You need to move a customer to a new PC.** Copy `data/billtotally.db` across.
The licence lives in the database, so it moves with them and no new key is
needed.

**A customer's database was lost and they have no backup.** Their activated key
cannot be reused on the fresh install — issue a replacement key and mark the old
row `Cancelled` with a note. This is a good reason to insist on `BACKUP.bat`
running daily.

**You changed the signing secret by accident.** Every issued key stops
validating. Restore the old secret from your backup of `.env`. There is no way
to recover keys otherwise, which is why the secret should be written down
somewhere safe outside the machine.

---

## 8. Quick reference

| Plan | Key char | Validity |
|---|---|---|
| Yearly | `Y` | 365 days |
| Monthly | `M` | 30 days |
| Trial | `T` | 14 days |

| Setting | Default | Meaning |
|---|---|---|
| `LICENSE_SIGNING_SECRET` | — | Vendor secret. Change once, never again. |
| `LICENSE_REQUIRED` | `true` | `false` disables the key check entirely |
| `LICENSE_WARN_DAYS` | `10` | When the amber warning starts |
| `DEMO_RETENTION_MINUTES` | `60` | How long demo data survives |
