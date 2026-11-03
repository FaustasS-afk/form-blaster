# Form Blaster

Bulk contact-form outreach. Give it a list of URLs; it finds each site's
contact form, fills it with a personal message, and submits — unattended and
crash-resumable.

Built to reach small US antique, vintage, jewellery and watch dealers whose
only contact route is a web form.

---

## What it does

```
leads.txt  →  find contact form  →  map fields  →  submit  →  results.csv
```

- **Two tiers.** Plain HTTP first (fast, ~1s/site). Bright Data Web Unlocker as
  a fallback only when a site blocks us.
- **Crash-safe.** Per-URL status in SQLite. Re-run and it resumes, never
  double-submits.
- **Per-target personalisation.** `[Name]` resolves to the business's real name.

## Results so far

- **9,978 US leads** harvested free (9,754 chain-free)
- **30–44% detection** measured across four samples of real leads
- ~1.3s/site effective at concurrency 8

---

## Quick start

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
python run.py --export results.csv
```

## Commands

| Command | Does |
|---|---|
| `python harvest_all.py` | Scrape localvintagestores.com (state → city → shop site) |
| `python harvest_atlas.py --theme jewelry` | Scrape antiquestoreatlas.com jewellery slice |
| `python merge.py` | Dedupe, split chains, write `leads_independent.txt` |
| `python run.py -i leads.txt` | Submit |
| `python run.py --dry-run` | Detect only, no submission |
| `python run.py --limit 100` | First 100 only |
| `python run.py --assume-sent` | Count `needs_manual` as sent |
| `python run.py --stats` | Progress |
| `python run.py --export out.csv` | Results |
| `python run.py --reset` | Requeue failures |

## Outcome statuses

| Status | Means |
|---|---|
| `submitted` | Confirmed — redirect, thank-you page, or form cleared |
| `needs_manual` | Form filled and posted, site printed no confirmation |
| `blocked` | WAF / rate-limit wall |
| `no_form` | No usable contact form on the site |
| `failed` | HTTP error or dead URL |
| `pending` | Not yet attempted |

`--assume-sent` treats `needs_manual` as done. Most of them did send.

---

## Configuration

`config/config.json` — your details and the message.

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

- `[Name]` is replaced per target with the business name. Falls back to the
  domain, then to a bare greeting.
- `spintax: true` enables `{Hi|Hello}` randomisation.

### Bright Data (optional)

Copy `.env.example` to `.env` and set `BRIGHTDATA_API_KEY`. Used as a fallback
only. **Enable CAPTCHA Solver on your Web Unlocker zone** in the dashboard.

If no key is set, the tool runs on plain HTTP alone.

---

## How forms are found

1. Tries the given URL, then common contact paths (including CMS-specific ones
   like Shopify's `/pages/contact`).
2. Falls back to crawling the homepage for contact links.
3. Follows forms hidden in iframes (Wufoo, JotForm, Typeform, Google Forms).
4. Scores every candidate and submits to the **highest-scoring** one, so a real
   contact form beats a footer newsletter box.

Field mapping matches each input against its `name`, `id`, `placeholder`,
`aria-label` and `<label>` text. Forms with meaningless names (`Field1`, `q3`)
fall back to positional mapping.

---

## Requirements

Python 3.10+. `httpx`, `beautifulsoup4`, `lxml`, `playwright` (browser tier
only, optional).

---

## Notes

- Only ~half of small dealer sites have a working contact form. Detect rates
  are a ceiling, not a bug.
- Some sites print no confirmation. That is why `needs_manual` exists.
- reCAPTCHA v3 passes silently; v2 and hCaptcha need solving.
