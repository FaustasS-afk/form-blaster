# Form Blaster — Leads Guide

**DONE: 9,978 unique US antique/vintage/jewellery/watch leads harvested, free.**

| File | Count | Use |
|---|---|---|
| `leads_independent.txt` | **9,754** | ← your outreach list |
| `leads_master.txt` | 9,978 | everything |
| `leads_chains.txt` | 224 | national chains |
| `leads_master.csv` | 9,978 | + phone (80%), name (84%) |

Jewellery/antique/watch subset: **4,318** (`python merge.py --jewellery-only`).

---

## ✅ HOW IT WAS BUILT — rerun any time

**Source 1 — general antique/vintage (biggest):**
```
python harvest_all.py
```
Walks `localvintagestores.com` (state → city → shop → the shop's own website).
Resumable, checkpoints every state. **8,595 sites across all 50 states.**

**Source 2 — jewellery niche (highest quality):**
```
python harvest_atlas.py --theme jewelry
```
`antiquestoreatlas.com` theme pages list member websites directly — one fetch
per state. **1,924 jewellery sites.** Other themes: `antique-malls`,
`furniture`, `consignment`, `estate-sales`.

**Combine and split:**
```
python merge.py                  # dedupe + split chains
python merge.py --jewellery-only # niche subset
```

Both harvesters are safe to run at once — they write separate files.
Sources 1+2 = 10,519 raw → **9,978 after dedupe** (~540 overlaps removed).



---

## Why this works

Each store detail page publishes schema.org JSON-LD containing the business's
**own website and phone number**. That is the outbound link you need.

Crawl path: `/{state}/` → `/{state}/{city}/` → `/{state}/{city}/store/{slug}/` → website

Verified output sample:
```
antiqueslewes.com         Lewes Mercantile Antique Gallery   +1 302-645-7900
milk-truck-vintage.com    Milk Truck Vintage                 +1 302-588-2160
kurtzcollection.com       Kurtz Collection                   +1 302-654-0442
```

---

## Second source (now automated)

`harvest_atlas.py --theme jewelry` — the `antiquestoreatlas.com` jewellery slice.
Their state pages list member websites directly, so it is one fetch per state.

⚠ Rate-limits under fast fetching (403) — the script retries with backoff.
⚠ Their free GitHub CSVs are only state/city aggregates. You must scrape HTML.


---

## Running the outreach

```
python run.py --input leads_independent.txt --dry-run        # check detection
python run.py --input leads_independent.txt                  # real run
python run.py --input leads_independent.txt --assume-sent     # treat probable-sends as done
python run.py --export results.csv                            # results
python run.py --stats                                         # progress
python run.py --reset                                         # requeue failures
```

**Measured detection rate: ~36%** on real leads (9 of 25 forms found and filled).

| Outcome | Means | Action |
|---|---|---|
| `submitted` | confirmed | done |
| `needs_manual` | form filled + posted, site gave no confirmation | probably sent; use `--assume-sent` |
| `blocked` | WAF / rate-limited | retry later or via browser |
| `no_form` | no contact form found | skip |
| `failed` | HTTP error / dead URL | skip |

---

## Known realities

- **~36% detection.** Many small shops have no contact form at all, or only a
  phone number. This is the ceiling, not a bug.
- **Chains are noise.** Goodwill / Habitat / Plato's Closet have corporate forms.
  `split_leads.py` separates them.
- **Some directory URLs are dead.** Expect 404s.

---

## If you want 10,000 instead of 5,000

Apify Google Maps scraper fills the gap for ~$12-70:
- `superslowsloth/google-maps-scraper` — ~$12.50/10k (newer, only ~79 runs; test first)
- `compass/crawler-google-places` — ~$50-70/10k (45.7M runs, reliable)

Set the "has website" filter so you don't pay for businesses without one.
Then `pradio/website-email-phone` (~$16) resolves contact-page URLs.
