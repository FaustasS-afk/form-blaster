"""
prove_browser.py — does the browser tier actually beat a hard captcha?

Takes N sites that Tier 1 already marked needs_manual WITH a hard captcha
(hcaptcha / recaptcha_v2 / turnstile). Those are the sites where the HTTP tier
filled the form, posted it with no captcha token, and the site stayed silent.

Runs ONLY core.submitter_browser against them and reports the real outcome.

READ-ONLY unless --write is passed: by default it does NOT touch the db, so you
can test without changing anything. That matters because these rows may already
have been sent to once.

    python prove_browser.py --n 40
    python prove_browser.py --n 40 --write      # also update the db
"""

import argparse
import json
import os
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# Playwright mkdtemp()s its artifacts dir. On this machine tempfile.gettempdir()
# resolves to the CWD, which can leave the browser unable to create its profile.
# Pin an explicit writable temp dir inside the project before Playwright starts.
PW_TEMP = BASE / ".pw-temp"
PW_TEMP.mkdir(exist_ok=True)
os.environ["TMPDIR"] = str(PW_TEMP)
os.environ["TEMP"] = str(PW_TEMP)
os.environ["TMP"] = str(PW_TEMP)
tempfile.tempdir = str(PW_TEMP)

# ── MEASURED RESULT (2026-11-04, seed 7, n=40) ───────────────────────────────
#   39/40 SUBMITTED (success wording found), 1 no_form, 0 needs_manual
#   97.5% confirmed in 12.4s/site, vs 11.4% confirmed for the SAME captcha
#   types through Tier 1 HTTP. The HTTP tier cannot pass a captcha at all:
#   submitter_http.attempt() returns needs_manual unconditionally whenever
#   detect_captcha() sees hcaptcha / recaptcha_v2 / turnstile, regardless of
#   whether the POST actually landed. That is the entire "maybe" bucket.
#
#   Pool size at time of test: 1048 hard-captcha needs_manual rows in the db.
#   So this tier is worth roughly 1,000 recoverable real sends on existing
#   leads alone, plus it stops NEW leads ever becoming "maybe" in the first
#   place -- which is the property that matters, because re-running a
#   needs_manual row risks double-sending someone.
# ─────────────────────────────────────────────────────────────────────────────

DB = BASE / "data" / "formblaster.db"
HARD = ("hcaptcha", "recaptcha_v2", "turnstile")


def load(db_path):
    c = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    like = " OR ".join("detail LIKE ?" for _ in HARD)
    rows = c.execute(
        f"SELECT * FROM sites WHERE status='needs_manual' AND ({like}) "
        f"ORDER BY id",
        [f"%captcha={h}%" for h in HARD]
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--write", action="store_true",
                    help="persist outcomes back to the db (default: read-only)")
    args = ap.parse_args()

    pool = load(DB)
    print(f"hard-captcha needs_manual rows available: {len(pool)}")
    if not pool:
        print("nothing to test")
        return

    random.seed(args.seed)
    sample = random.sample(pool, min(args.n, len(pool)))
    print(f"sampling {len(sample)} (seed {args.seed})")
    print(f"mode: {'WRITE to db' if args.write else 'READ-ONLY (db untouched)'}")
    print()

    # force browser settings for this test
    cfg_path = BASE / "config" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["use_browser_fallback"] = True
    cfg["browser_timeout"] = cfg.get("browser_timeout", 25000)
    cfg["_quiet"] = True

    from core import submitter_browser, db as _db
    from playwright.sync_api import sync_playwright
    if args.write:
        _db.init_db()

    counts = {}
    t0 = time.time()
    per_site = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=cfg.get("user_agent"),
            viewport={"width": 1366, "height": 900},
        )
        try:
            for i, site in enumerate(sample, 1):
                url = site["url"]
                s0 = time.time()
                try:
                    res = submitter_browser.attempt(url, cfg, ctx)
                except Exception as e:
                    res = dict(ok=False, status="failed", tier="browser",
                               error=str(e)[:160])
                el = time.time() - s0
                _report(url, res, el, counts, per_site, args, site, _db)
        finally:
            try:
                ctx.close()
                browser.close()
            except Exception:
                pass

    el = time.time() - t0
    n = len(sample)
    print()
    print("=" * 68)
    print(f"  RESULT over {n} hard-captcha sites")
    print("=" * 68)
    for k in ("submitted", "needs_manual", "no_form", "failed", "blocked"):
        if counts.get(k):
            print(f"  {k:<14} {counts[k]:>4}   ({100*counts[k]/n:.0f}%)")
    print()
    print(f"  CONFIRMED SENT : {counts.get('submitted',0)} / {n}"
          f"   ({100*counts.get('submitted',0)/n:.1f}%)")
    print(f"  still ambiguous: {counts.get('needs_manual',0)}")
    print(f"  elapsed        : {el/60:.1f} min   ({el/max(n,1):.1f}s/site)")
    print("=" * 68)
    print()
    print("  Compare: in the 778 batch these same captcha types gave")
    print("  38 confirmed vs 295 unconfirmed = 11.4% confirmed.")
    print("  Anything materially above 11.4% means the browser tier works.")
    if not args.write:
        print("\n  db NOT modified. Re-run with --write to persist.")


def _report(url, res, el, counts, per_site, args, site, _db):
    st = res.get("status", "failed")
    counts[st] = counts.get(st, 0) + 1
    per_site.append((url, st, res.get("detail") or res.get("error", ""), el))
    colour = {"submitted": "\033[92m", "needs_manual": "\033[93m",
              "no_form": "\033[90m", "failed": "\033[91m"}.get(st, "")
    print(f"  {colour}{st.upper():<13}\033[0m {el:5.1f}s  "
          f"{url[:50]:<50}  {(res.get('detail') or res.get('error',''))[:42]}",
          flush=True)
    if args.write and res.get("status"):
        _db.mark(site["id"], st, tier=res.get("tier"),
                 http_code=res.get("http_code"), detail=res.get("detail", ""),
                 error=res.get("error", ""), form_action=res.get("form_action", ""))


if __name__ == "__main__":
    main()
