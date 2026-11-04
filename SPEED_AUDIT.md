# FORM BLASTER — SPEED + CAPTCHA AUDIT (2026-11-03)

All numbers measured live on this machine. Not estimates.

---

## WHAT WAS FIXED (real files, done)

| File | Change |
|---|---|
| `core/soup.py` | **NEW** — single parser helper, auto-fallback lxml>html5lib>html.parser, `safe_parse` never raises |
| `core/formdetect.py` | generator parse made safe (was killing every site); `_site_key()` tolerates www/non-www |
| `core/submitter_http.py` | 2 parses made safe; `_client()` now prefers **curl_cffi** |
| `core/embeds.py` | iframe parse made safe |
| `core/submitter_browser.py` | `_page_soup` uses safe parser |
| `core/unlocker.py` | BD timeout 90s -> 20s + circuit breaker |
| `lxml` | installed (6.1.3) — was MISSING, caused 100% failure |

Result: **0% success -> ~44% form found+filled.** The tool works now.

---

## MEASURED PROGRESSION

| Run | Wall (25 sites) | Per site | Forms | Note |
|---|---|---|---|---|
| Original | 1402s (24) | 58.4s | **0/24 FAILED** | lxml crash |
| + lxml + soup | 27.3s | 1.1s | — | but curl_cffi absent, all "blocked" |
| + curl_cffi, BD ON | 1291s | 53.8s | 11/24 | BD stalls |
| + BD off | 1080s | 43.2s | 11/25 | still slow |
| **Ceiling (5 reqs/site)** | **60.1s** | **1.00/s `_`** | 19/60 | **70 min for 4,192** |

Ceiling test = same client, but only homepage + 4 contact paths, no
link-crawl, no second-signal. **1.00 sites/sec, 70 min total.**

So the remaining 40x is in the SCAN, not the network.

---

## ROOT CAUSE 3 (the remaining one): the candidate scan

`submitter_http.attempt()` iterates `formdetect.candidate_urls()`, which emits
**up to 26 URLs per site**, each a separate blocking GET + parse + score. On
top, `_second_signal()` adds another GET on every ambiguous result — the
biggest bucket. That is where 43s/site lives.

Verified: 5 requests/site = 1.0 sites/sec. 26 requests/site = 0.02 sites/sec.

---

## CAPTCHA — honest verdict

Your `SOLVE_CAPTCHA_API_KEY` is live, **$5.00 balance**, on
`api.solvecaptcha.com` (NOT 2captcha.com — that host returns
ERROR_KEY_DOES_NOT_EXIST).

**But captchas are NOT the main bottleneck.** Census of 25 real pending sites:

| Marker | Sites |
|---|---|
| form present | 18 |
| mentions recaptcha | 15 |
| mentions hcaptcha | 14 |
| clean | 5 |
| **real cf_js_challenge** | **2** |
| real turnstile | 2 |

**Only ~2/25 (8%) are genuinely captcha-walled.** The `hcaptcha`/`recaptcha`
strings are mostly just JS on otherwise-fine pages — curl_cffi already gets the
real 200 (166KB-813KB). Median fetch 0.38s.

The 2 real ones (`twincitiesmagic.com`, `vintageandpearlsquilting.com`) are the
**hard mode**: `cf-mitigated: challenge`, no `data-sitekey`, sitekey hidden in a
JS blob. Solving those needs the JS injection from your doc (lines 736-758) to
hook `turnstile.render` and capture cData/chlPageData/action — a browser job,
not an HTTP job.

Cost if you did solve them: $0.80/1000 = **$0.0008 each**. Even 500 of them
= $0.40. Trivial. But it is ~8% of sites, so it is the LAST 8%, not the 10x.

---

## RANKED REMAINING WORK

1. **Trim the candidate scan** — try configured contact paths, and only fall
   back to link-crawl when nothing found. Biggest remaining win.
2. **Cap/skip `_second_signal`** — or memoise it.
3. **Raise concurrency 32 -> 64** once scan is trimmed.
4. **Turnstile/challenge solving** for the ~8% — needs a browser + your
   solvecaptcha key + the documented JS hook. Last.
5. Cloud phones — only for extra IPs, later.

## SETTING THAT MATTERS

`BRIGHTDATA_ENABLED=0` is the practical default now. curl_cffi covers what BD
was for, ~100x faster and free. Keep BD for the genuinely hard few.

## WARNING

`delay_between_sites_*` in config is **only used by the harvesters, never by
the submitter.** So the submitter has no artificial delay; all slowness is
real work. Don't waste time tuning that pair.

---

## 2026-11-03 (evening): scan fix, measured on the same 30 leads (scan only, nothing sent)

| | Wall | Requests | Forms found |
|---|---|---|---|
| Before | 335s | 692 | 5/30 |
| After  | **14s** | **149** | 6/30 |

Changes: homepage links tried before guessed paths; per-site fetch cache (homepage was fetched twice);
`max_candidate_pages` now honoured (was hard-coded 26); stop on dead/hanging hosts (were 144s each).
New `probe_forms.py`: finds forms only, writes `<leads>_forms.txt` for `run.py --input`.
