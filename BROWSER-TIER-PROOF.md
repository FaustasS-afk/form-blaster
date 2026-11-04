# Browser tier vs captchas — MEASURED (2026-11-04)

The short version: **the browser tier confirms 97.5% of hard-captcha sites.
The HTTP tier confirms 11.4% of the same sites and calls the rest "maybe".**

---

## The measurement

`prove_browser.py` — 40 randomly sampled sites (seed 7) drawn from rows the
HTTP tier had already marked `needs_manual` *with a hard captcha*
(`hcaptcha` / `recaptcha_v2` / `turnstile`). Run read-only, `submitter_browser`
only, fresh Playwright context.

| Outcome | Count | % |
|---|---|---|
| `submitted` (real success wording found) | **39** | 97.5% |
| `no_form` | 1 | 2.5% |
| `needs_manual` | **0** | 0% |

- 8.2 min total, **12.4 s/site**
- Pool available for this test: **1,048 rows**

Same captcha types via Tier 1 HTTP, measured on the 778 batch:
**38 confirmed vs 295 unconfirmed = 11.4%.** That is an **8.5x** improvement,
and the "still ambiguous: 0" is the important part.

## WHY the HTTP tier can never pass a captcha

This is a logic fact, not a tuning problem. `core/submitter_http.py`:

```python
hard_captcha = cap in ("recaptcha_v2", "hcaptcha", "turnstile")
if not hard_captcha:
    ... submit  # v3 silent, fine
return dict(ok=False, status="needs_manual", ...)   # <-- line 478
```

There is no solver wired in, and no browser, so no JS runs and no captcha token
is ever produced. The POST goes out with an empty token, the site stays silent,
and the row is filed as `needs_manual` **unconditionally**. Every hard-captcha
site is a guaranteed "maybe" while the browser tier is off.

## Two separate bugs found

1. **`core/submitter_http.py::_unlocked_fetch()` is dead code.** It is defined
   at line 58 and never called from anywhere. Bright Data Web Unlocker is fully
   written, correctly handshaked (async `/unblocker/req` -> `/unblocker/get_result`,
   KYC-error sniffing, circuit breaker) and simply not plugged into the submit
   path. This is why the `.hub.biz` + Cloudflare buckets stay `blocked`.

2. **No captcha solver module exists.** Grepping the whole repo for
   `SOLVE_CAPTCHA|2captcha|anticaptcha|NONECAP|capsolver` returns **nothing**.
   Keys sit in `.secrets/env.txt` unused. A solver alone would still not fix
   the "maybe" problem unless the line-478 return is also changed.

## Consequence for strategy

`db.reset_failed()` requeues `needs_manual`. So a blind re-run **double-sends**
everyone who already got the form posted. The browser tier removes the need to
re-run at all: new leads get confirmed on the first pass and never enter the
"maybe" bucket, so nothing ever needs a second send.

Priority order, settled:

1. **Browser tier** — free, biggest win, no code change needed, just config.
2. **Wire in Bright Data** — ~3 lines, already written and paid for.
3. **Solver API** — last resort, costs money, only for what the browser misses.

## Machine-specific gotcha

`tempfile.gettempdir()` on this box resolves to the **CWD**, so Playwright's
`mkdtemp` for its artifact dir failed with `EPERM`. Fix, applied in
`prove_browser.py`: pin `TEMP`/`TMP`/`TMPDIR` and `tempfile.tempdir` to
`.pw-temp/` in the project root before importing Playwright. The same guard
needs to go wherever the browser tier is invoked from the main runner.

## Reproduce

```bash
python prove_browser.py --n 40 --seed 7          # read-only, db untouched
python prove_browser.py --n 40 --seed 7 --write  # persist outcomes
```

Note `--n 40` is ~8 min. The browser tier is ~12 s/site vs ~0.13 s/site for
HTTP, so it belongs on the *retry/fallback* path, never as the default tier.
