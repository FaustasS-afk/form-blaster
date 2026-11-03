# FORM BLASTER — Project Memory

Durable notes. Read this before working on Form Blaster again.
Last updated: 2026-11-03

---

## WHAT IT IS

Standalone bulk contact-form submitter. Feed it URLs, it finds each site's
contact form, fills it with a personal message, and submits. Unattended and
crash-resumable.

Built for: Daniel / Easy Listings (info@easylistings.co.uk) — sells listing
management to US antique / vintage / jewellery / watch dealers.

Location: `C:\Users\SellJ\Desktop\FORM BLASTER`

---

## STATUS

| Part | State |
|---|---|
| Lead scraper | **DONE** — 9,978 unique US leads, free |
| Submitter (HTTP tier) | **WORKING** — ~30-44% detection |
| Message + [Name] | **DONE** |
| Bright Data captcha solving | **WIRED IN, needs testing** |
| GitHub repo | **NOT YET** — user wants to push |
| Desktop app | **NOT YET** — user wants this |

---

## DELIVERABLES

| File | Count |
|---|---|
| `leads_independent.txt` | 9,754 (chain-free) |
| `leads_master.txt` | 9,978 |
| `leads_master.csv` | + phone 80%, name 84% |
| Jewellery-only | 4,318 (`merge.py --jewellery-only`) |

Tooling: `harvest_all.py`, `harvest_atlas.py`, `merge.py`, `run.py`,
`scrape_leads.py`, `split_leads.py`, `batch_test.py`, `verify.py`,
`core/` (db, fieldmap, formdetect, submitter_http, submitter_browser, embeds, spin)

---

## BRIGHT DATA — KEY FACTS

**Env file:** `C:\Users\SellJ\Documents\deepseek-harness\default-workspace\.secrets\env.txt`
- Key name: `BRIGHTDATA_API_KEY`
- Value ends with `]` in that file — strip it when parsing
- Zone: `web_unlocker1`
- **CAPTCHA Solver: ENABLED** (confirmed via dashboard screenshot 2026-11-03)
- API Type: Web Unlocker API, $1.50/CPM pay-as-you-go

**Endpoint:**
```
POST https://api.brightdata.com/request
Authorization: Bearer <KEY>
{"zone":"web_unlocker1","url":"<TARGET>","format":"raw"}
```

**Dashboard error headers to know:**
- `x-brd-error: captcha or protection page found` → solver was off; retest now it's ON
- `x-brd-error: classified as Government and blocked` → gov.uk will never work
- Returns HTTP 200 with EMPTY BODY on failure — must check body length AND the header

**Measured before solver was on:**
- WON: platoscloset.com (403 → 200 + form), thehistoryroom.com (114b → 25KB)
- LOST: gov.uk (policy block), antiques-atlas.com (captcha, solver was off)
- NO CHANGE on 6 others; 3-13s per request (vs ~1s plain)

**Architecture decision:** use Bright Data as a FALLBACK ONLY — retry through it
when plain httpx gets 403/WAF/captcha. Using it for everything makes a 2h run
take 10h+.

---

## THE MESSAGE (installed, spintax off)

```
Hi [Name],

We've built Easy Listings to take the work out of getting stock online. It's
designed to help dealers list up to 10 times more stock in the same amount of time.

You can try it free, with no payment required, and watch the demo videos here:
https://www.easylistings.co.uk

I thought it might be useful for your business. If you have any questions, just let me know.

Best,
Daniel
The Easy Listings team
```

Subject: "Getting your stock online"
`[Name]` resolves to the business name from the CSV; falls back to domain-derived
name, then bare "Hi,". Handles LLC/Inc, long names, dangling `&`, camelCase.

Config fields: name=Daniel, email=info@easylistings.co.uk,
phone=+447399643492, company=Easy Listings, website=www.easylistings.co.uk

---

## HARD-WON TECHNICAL NOTES (do not relearn these)

1. **URL fragments** — `action="/contact#contact_form"` must be stripped before
   POSTing or it 404s. WordPress/Elementor do this constantly.
2. **Iframe forms** — Wufoo/JotForm/Typeform/Google Forms are invisible to plain
   HTTP. `core/embeds.py` fetches the iframe URL and treats it as a form page.
3. **Ordinal mapping** — `Field1`/`q3`/unnamed fields need positional fallback
   (`fieldmap.ordinal_fallback`).
4. **reCAPTCHA v3 is silent** and passes; only v2/hCaptcha/Turnstile need a human.
   Splitting these raised detection 36% → 42%.
5. **Newsletter/comment forms** score -4/-3; contact actions score +3.
6. **Playwright `with` block deadlocks** — use explicit `start()`/`stop()`.
7. **Action-404 fallback** — if the form action 404s, retry against the page URL.
8. **No `requests`/`dotenv` installed** — stdlib + httpx only.

---

## MEASURED PERFORMANCE

Detection: 30-44% across 4 samples (~4,000 of 9,754 forms fillable).
Speed: 13-26s/site single-threaded; ~1.3s/site effective at concurrency 8.
Real send confirmation: ~4-5% (3 thank-you emails from 67 attempts).

Biggest failure buckets: `no_form` (~30%), `needs_manual` (~30%),
hCaptcha (~18%), WAF/403 (~8%).

Outcome statuses: `submitted` (confirmed) | `needs_manual` (posted, no confirm) |
`blocked` | `no_form` | `failed` | `pending`.
`--assume-sent` counts needs_manual as done.

---

## NEXT TASKS (in order)

1. **Retest Bright Data with solver ON** against hCaptcha sites — the whole point
2. Wire it as a fallback tier in `submitter_http.attempt` (only on 403/WAF/captcha)
3. Finish the interrupted 100-lead run (db had ~120 done, 9,626 pending)
4. Push to GitHub — needs a repo name, README, licence, .gitignore for `data/`
5. Build as a desktop app — probably PyQt6, matching the Instagram tool's style
6. Phone field now set; confirm required-phone forms pass

---

## USER PREFERENCES (important)

- Hates AI-slop writing. Flagged the first message as "shittiest AI slop".
  Wants: fewer placeholders, no "where we found you", more curiosity/manipulation.
- Direct. Prefers blunt honesty over hedging.
- Wants things actually verified, not claimed.
- Goal is volume (originally 10,000 forms).
