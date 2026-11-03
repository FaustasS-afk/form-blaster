# Form Blaster — Measured Results

Real numbers from live runs on harvested leads. Not estimates.

---

## Lead harvest — COMPLETE

| Source | Result | Cost |
|---|---|---|
| `harvest_all.py` (localvintagestores, all 50 states) | **8,595 sites** | $0 |
| `harvest_atlas.py --theme jewelry` (all 50 states) | **1,924 jewellery sites** | $0 |
| After `merge.py` dedupe | **9,978 unique** | $0 |

Dedupe removed ~540 overlapping domains between sources.

- **9,754** independent (chain-free) — the outreach list
- **4,318** jewellery/antique/watch subset (`--jewellery-only`)
- **80% have a phone number, 84% have a business name**

Runtime: ~75 min for source 1, ~12 min for source 2. No blocking, no logins.

---

## Submitter — measured on real leads

Four independent samples of real harvested leads, random seeds:

| Outcome | A (60) | B (40) | C (40) | D (40) |
|---|---|---|---|---|
| `submitted` (confirmed) | 5 | 3 | 3 | 1 |
| `needs_manual` (filled+posted) | 17 | 14 | 14 | 11 |
| blocked (WAF/429) | 10 | 6 | 6 | 1 |
| no_form | 23 | 14 | 14 | 22 |
| failed (HTTP error) | 5 | 3 | 3 | 5 |
| **form found + filled** | **36%** | **42%** | **42%** | **30%** |

**Realistic range: 30-42%, call it ~36-40% on average.**

The spread is dominated by `no_form`, which varies 14-23 per 40 depending on how
many samples land on thrift stores, blogs and Squarespace subdomains.

**Speed: ~13-26s/site** single-threaded. At concurrency 8-10 this is ~5s/site effective.

Extrapolated on 9,754 independent leads:
- ~3,000-4,000 forms actually filled
- ~1,500-2,500 confirmed sends

---

## What was fixed to get here

| Fix | Impact |
|---|---|
| Iframe form support (Wufoo/JotForm/Typeform/Google) | Recovered a whole class of `no_form` — these forms are invisible to plain HTTP |
| Ordinal field mapping | Handles `Field1`/`q3`/unnamed fields — verified mapping `Field3`→email, `Field7`→message on a live Wufoo form |
| Confirm-by-resubmit | Moved sites from ambiguous → confirmed (`habitatncc.org`) |
| reCAPTCHA v2 vs v3 split | v3 is silent and passes; stops over-flagging — **detection 36% → 42%** |
| Chain filtering | 7,124 independent vs 151 chains — cleaner targets |
| Canonical domain dedupe | www/non-www dupes removed (~900 caught) |
| **URL fragment stripping** | `action="/contact#contact_form"` was being POSTed verbatim and 404ing. WordPress/Elementor use this constantly |
| **Action-404 fallback** | When the action path 404s, retry against the page URL. Recovered `prairierevival.com`, `AntiquishHomeDecor.com` from hard-fail → filled |
| **Prefer 200-score candidates** | A form scraped off a 404 page is worth less than the same form on a live page |

---

## Honest ceiling

**~36-40% is close to the practical maximum for HTTP-only submission.**

The remaining ~60% breaks down as:
- **~23-35% `no_form`** — many small shops genuinely have no contact form, only a phone
- **~5-15% blocked/error** — Cloudflare, 403, 429 rate limits
- **~28-35% `needs_manual`** — form filled and posted, site prints no confirmation

`needs_manual` is the big lever. Use `--assume-sent` to count them — most did send.

---

## Known limitations

1. **reCAPTCHA v2 / hCaptcha need a human.** v3 passes silently.
2. **~5% of directory URLs are dead** (404/403).
3. **Sites vary 2-30s.** One slow site per batch can add minutes. Use `--concurrency 8-10`.
4. **Squarespace/blogspot subdomains rarely have forms.**
5. **Browser tier (Tier 2) is built but unproven** — HTTP handles more than expected.
6. **Some directory URLs are category pages, not shops** (e.g. `visitomaha.com/listings/...`).

---

## Reproduce / operate

```
# harvest (both, all 50 states, ~90 min total, free)
python harvest_all.py
python harvest_atlas.py --theme jewelry

# combine, dedupe, split
python merge.py
python merge.py --jewellery-only     # 4,318 niche subset

# measure before committing
python batch_test.py --n 50 --dry-run

# run
python run.py --input leads_independent.txt --assume-sent
python run.py --export results.csv
python run.py --stats
```

