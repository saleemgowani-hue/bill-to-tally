# Vendor Setup — the licence signing secret

Read this once, before you issue a single key.

---

## What it is

Every licence key is signed with a secret value. The app recomputes the
signature to decide whether a key is genuine. There is **no default** in the
source code, and there never should be: a secret shipped inside the code is not
a secret, because anyone holding the code can mint unlimited valid keys.

The value lives in one place only: the `LICENSE_SIGNING_SECRET` line of `.env`.

---

## Setting it

```bash
python setup_secret.py            # generates one if .env has none
python setup_secret.py --show     # show what is set, masked
python setup_secret.py --set VALUE
```

`START.bat` and `INSTALL.bat` run this for you on a fresh install. It never
overwrites an existing value unless you pass `--set` and type REPLACE.

**Write the value down somewhere that is not this computer.** A photograph of a
notebook page beats a file on the same hard disk that will one day fail.

---

## The rule that matters

> The machine that **issues** keys and the machine that **uses** them must have
> the **same** secret.

You issue keys on your own PC with `generate_keys.py`. A customer's install
validates them. If their `.env` carries a different secret, every key you give
them is rejected — the key is fine, the two installs simply disagree about what
"genuine" means.

So when you install for a customer, put **your** secret in their `.env`. Do not
let `setup_secret.py` generate a fresh one for them.

---

## Two ways to run your business

**One secret for everyone (simplest, recommended).** Your secret goes into every
customer install. Any key you issue works on any customer's machine. This is how
most small software businesses operate.

Risk: a customer who opens `.env` can read the secret and mint their own keys.
For a shop owner running a purchase-entry tool this is unlikely, but it is real.

**A different secret per customer (tighter).** Each install gets its own secret,
and you keep a note of which customer has which. A key minted for customer A
does not work for customer B, and a leaked secret only affects one customer.

Cost: you must generate that customer's keys with *their* secret, which means
setting `LICENSE_SIGNING_SECRET` in your own `.env` to theirs before running
`generate_keys.py`. Losing your notes means losing the ability to renew them.

Start with one secret. Move to per-customer only if you ever have reason to.

---

## What you must never do

- **Never change a secret once keys are out.** Every key signed with the old one
  stops working immediately, including keys customers are already using. There
  is no recovery.
- **Never ship `generate_keys.py`, `GENERATE_KEYS.bat`, your key register, or
  `keys_issued_log.csv` to a customer.** Use the customer build described below.
- **Never commit `.env` to Git.** `.gitignore` already excludes it.

---

## Building a copy for someone else

The customer build leaves out the key-minting tools:

```
generate_keys.py
GENERATE_KEYS.bat
SN_Softech_Licence_Keys.xlsx
keys_issued_log.csv
.env                    (never ship yours — it holds the secret)
```

Everything else is the same code. On their machine:

1. Extract the folder
2. Run `START.bat` — it creates `.env` from `.env.example`
3. Edit `.env` and paste **your** secret into `LICENSE_SIGNING_SECRET=`
4. Start the app and sign up with a key from your register

Until step 3 is done the app shows a **Setup needed** screen rather than
accepting anything. That is deliberate.

---

## If something goes wrong

**"This installation has no licence signing secret"** — `.env` is missing the
value. Do step 3 above.

**A key that works on your PC is rejected on theirs** — the two `.env` files
hold different secrets. Copy yours across.

**You lost the secret and have no backup** — every existing key is dead. Set a
new secret, issue replacement keys to every customer, and keep the new one
somewhere safe. There is no shortcut, which is the whole reason to write it
down on day one.

---

## What this protects against, and what it does not

**It does protect against:** a copy of the software being installed and used
without a key you issued. Without your secret, keys minted elsewhere are
rejected, and the app refuses to run its licence checks at all until a secret
is configured.

**It does not protect against a determined customer who has your secret.** This
ships as Python source. The signing algorithm is visible in
`services/license_service.py`, so anyone able to read the code and holding your
secret can mint their own keys in a few minutes. Removing the key-generation
script raises the effort slightly; it does not close the door.

That is a property of shipping source code, not a flaw you can patch away. The
honest options are:

- **Accept it.** Your customers are shop owners who want purchase bills entered,
  not people who will reverse-engineer an HMAC. For most of this market this is
  the right call.
- **Use a per-customer secret.** A customer who extracts their secret can only
  make keys for their own install, not for anyone else's.
- **Move the check to a server** if the product ever justifies it. That means
  the customer's PC must reach the internet to validate, which is exactly what
  the offline design was chosen to avoid — and most shops running Tally have
  patchy connectivity.

Decide deliberately rather than assuming the current setup is airtight. For a
product at this price point, per-customer secrets plus ordinary business trust
is a reasonable place to stand.

---

## Shipping to a customer — the easy way

Do not ask a shop owner to edit `.env`. Build them a package instead:

```bash
python make_customer_build.py "Novelty Store"
```

That produces `dist/BillToTallyAgent_Novelty_Store.zip` with:

- the key-minting tools, your key register and your vendor docs removed
- a ready `.env` carrying your secret, so your keys work immediately
- a plain `READ_ME_FIRST.txt` for the customer

Their whole experience: extract the folder, double-click `START.bat`, sign up
with the key you handed over. Nothing to configure, no file to edit.

`START.bat` runs `setup_secret.py`, which sees the secret is already set and
leaves it alone — it never overwrites an existing value.

For a customer with their own secret:

```bash
python make_customer_build.py "Novelty Store" --secret THEIR_SECRET
```

Remember to mint their keys with that same value, or they will be rejected.

The script refuses to produce a zip if any key-minting file slips in, so a
mistake here fails loudly rather than quietly shipping your ability to issue
licences.
