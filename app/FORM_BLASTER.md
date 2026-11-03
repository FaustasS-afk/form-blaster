# Form Blaster — App Edition

A desktop app for bulk contact-form outreach. Import leads, blast them, and
track exactly what was sent, what failed, and what was blocked.

This is **Form Blaster integrated into a PyQt6 desktop app**, so everything
lives in one window with a live log, progress, and a permanent record.

---

## What it does

| Tab | Purpose |
|---|---|
| **Leads** | Import/paste URL lists, filter, dedupe, delete, export |
| **Blast** | Start/pause/stop a live run, watch progress and per-site log |
| **Form Tracker** | Every lead and its outcome, with evidence and CSV export |

Outcomes tracked per lead: `submitted` (confirmed) · `needs_manual` (posted,
no confirmation) · `blocked` · `no_form` · `failed` · `pending`.

Runs resume — a crash never loses progress and never double-submits.

---

## Run it

```
venv\Scripts\python.exe main.py
```

or double-click `START ENGINE.bat`.

---

## Using it

1. **Leads tab** → `Import TXT/CSV` or `Paste URLs`. One per line.
   (9,754 US antique/jewellery leads ship in `outreach/leads_independent.txt`.)
2. **Blast tab** → set a limit, pick threads (8 is sane), `▶ Start Blast`.
3. **Form Tracker tab** → see results, filter by outcome, export CSV.

`Dry run` detects without touching the database. `Assume sent` counts
`needs_manual` as sent in the summary — most of those did arrive.

Requeue anything from the Tracker to retry it.

---

## Configuration

`outreach/blaster_config.json` — your details and the message.

```json
{
  "name": "Your Name",
  "email": "you@example.com",
  "phone": "",
  "company": "Your Company",
  "website": "example.com",
  "subject": "Your subject",
  "message": "Hi [Name],\n\n...",
  "spintax": false
}
```

`[Name]` resolves per lead from the business name, falling back to a
domain-derived name. `spintax: true` enables `{Hi|Hello}` variation.

### Bright Data (optional)

Set `BRIGHTDATA_API_KEY` in a `.env` beside `main.py`. Used **only as a
fallback** when plain HTTP gets blocked. Enable CAPTCHA Solver on the zone.

Without a key the app runs on plain HTTP alone.

---

## Harvesting leads

```
venv\Scripts\python.exe outreach\harvest_all.py
venv\Scripts\python.exe outreach\harvest_atlas.py --theme jewelry
venv\Scripts\python.exe outreach\merge.py
```

Scrapes `localvintagestores.com` and `antiquestoreatlas.com` across all 50 US
states. Free, ~90 minutes, yields ~10,000 unique dealer websites.

---

## How it works

- Plain HTTP first (fast). Bright Data fallback when blocked.
- Scores every form on a page and submits to the best one, so a real contact
  form beats a footer newsletter box.
- Follows forms hidden in iframes (Wufoo, JotForm, Typeform, Google Forms).
- Maps fields by name, id, placeholder, aria-label and label text, with
  positional fallback for meaningless names.
- Stores results in SQLite with WAL, checkpointed on exit.

**Measured: 30–44% form detection** across samples of real leads.

---

## Requirements

Python 3.10+. See `requirements.txt`.

---

## Notes

- Only about half of small dealer sites have a usable contact form.
- reCAPTCHA v3 passes silently; v2 and hCaptcha need solving.
- The Tracker table caps at 5,000 rows for speed; exports the full set.
