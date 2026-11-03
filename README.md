# Form Blaster

Bulk contact-form outreach. Give it a list of URLs; it finds each site's
contact form, fills it with a personal message, and submits - unattended and
crash-resumable.

Built to reach small US antique, vintage, jewellery and watch dealers whose
only contact route is a web form.

**Two ways to use it:**

| | |
|---|---|
| **Desktop app** (`/app`) | PyQt6 GUI - import leads, blast, track results live |
| **CLI** (this folder) | Headless scripts for automation and scale |

---

## Desktop app

```
cd app
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python.exe main.py
```

Three tabs: **Leads** (import/paste URLs), **Blast** (start/stop, live log,
progress) and **Form Tracker** (every lead and its outcome, with CSV export).

See [app/FORM_BLASTER.md](app/FORM_BLASTER.md) for details.

---

## CLI

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt

# build a lead list (free, ~90 min for all 50 US states)
python harvest_all.py
python harvest_atlas.py --theme jewelry
python merge.py

# check detection before committing
python batch_test.py --n 50 --dry-run

# run it
python run.py --input leads_independent.txt --assume-sent
```

| Command | Does |
|---|---|
| `harvest_all.py` | Scrape localvintagestores.com (state -> city -> shop site) |
| `harvest_atlas.py --theme jewelry` | Scrape antiquestoreatlas.com jewellery slice |
| `merge.py` | Dedupe, split chains, write `leads_independent.txt` |
| `run.py -i leads.txt` | Submit |
| `run.py --dry-run` | Detect only |
| `run.py --limit 100` | First 100 only |
| `run.py --assume-sent` | Count `needs_manual` as sent |
| `run.py --stats` / `--export out.csv` / `--reset` | Progress, results, requeue |

---

## What ships

**9,978 unique US leads** harvested free - 9,754 chain-free, 4,318
jewellery/antique/watch only. Names on 84%, phone numbers on 80%.

`app/outreach/leads_independent.txt` is ready to import.

## Results

**30-44% form detection** measured across four samples of real leads.
Outcome statuses:

| Status | Means |
|---|---|
| `submitted` | Confirmed - redirect, thank-you page, or form cleared |
| `needs_manual` | Form filled and posted, site printed no confirmation |
| `blocked` | WAF / rate-limit wall |
| `no_form` | No usable contact form |
| `failed` | HTTP error or dead URL |

`--assume-sent` treats `needs_manual` as done. Most of them did send.

---

## Configuration

Your details and message live in `config/config.json` (CLI) and
`app/outreach/blaster_config.json` (app).

`[Name]` resolves per target from the business name. `spintax: true` enables
`{Hi|Hello}` variation.

### Bright Data (optional)

Copy `.env.example` to `.env` and set `BRIGHTDATA_API_KEY`. Used **only as a
fallback** when plain HTTP is blocked. Enable CAPTCHA Solver on your zone.

Without a key it runs on plain HTTP alone.

---

## How it works

- Plain HTTP first (fast, ~1s/site). Browser and Bright Data as fallbacks.
- Scores every form on a page and submits to the best, so a real contact form
  beats a footer newsletter box.
- Follows forms inside iframes (Wufoo, JotForm, Typeform, Google Forms).
- Maps fields by name, id, placeholder, aria-label and label text, with
  positional fallback for meaningless names.
- Per-URL status in SQLite. Re-run and it resumes, never double-submits.

## Notes

- Only about half of small dealer sites have a usable contact form.
- reCAPTCHA v3 passes silently; v2 and hCaptcha need solving.
- Requirements: Python 3.10+.
