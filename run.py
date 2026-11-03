"""
run.py — Form Blaster CLI.

  python run.py --input urls.txt                  submit to every URL
  python run.py --input urls.txt --dry-run        detect only, no submission
  python run.py --limit 20                        first 20 pending only
  python run.py --http-only                       skip the browser tier
  python run.py --browser-only                    skip the HTTP tier
  python run.py --reset                           requeue failed/needs_manual
  python run.py --stats                           show progress
  python run.py --export out.csv                  dump results
"""

import argparse
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core import db, spin

C_OK = "\033[92m"
C_WARN = "\033[93m"
C_ERR = "\033[91m"
C_DIM = "\033[90m"
C_END = "\033[0m"


def load_urls(path):
    p = Path(path)
    if not p.exists():
        print(f"{C_ERR}Input file not found: {path}{C_END}")
        sys.exit(1)
    urls = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line.split(",")[0].strip() if "," in line else line)
    return urls


def bar(done, total, width=32):
    if not total:
        return ""
    filled = int(width * done / total)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {done}/{total}"


def emit(res, counters):
    st = res.get("status")
    counters[st] = counters.get(st, 0) + 1
    url = res.get("url", "")
    short = url.replace("https://", "").replace("http://", "").rstrip("/")[:52]
    tier = res.get("tier") or "-"
    if st == "submitted":
        print(f"  {C_OK}OK{C_END}   [{tier}] {short}  {C_DIM}{res.get('detail','')}{C_END}")
    elif st == "needs_manual":
        print(f"  {C_WARN}MAN{C_END}  [{tier}] {short}  {C_DIM}{res.get('detail','')}{C_END}")
    elif st == "blocked":
        print(f"  {C_WARN}BLK{C_END}  [{tier}] {short}  {C_DIM}{res.get('detail','')}{C_END}")
    elif st == "no_form":
        print(f"  {C_DIM}NONE [{tier}] {short}{C_END}")
    else:
        print(f"  {C_ERR}FAIL{C_END} [{tier}] {short}  {C_DIM}{str(res.get('error',''))[:60]}{C_END}")


def persist(res):
    db.mark(
        res["id"], res.get("status", "failed"), tier=res.get("tier"),
        http_code=res.get("http_code"), detail=res.get("detail", ""),
        error=res.get("error", ""), form_action=res.get("form_action", ""),
        fields_hit=res.get("fields_hit", ""),
    )


# Statuses that mean "a form was found, filled and posted, but the site did
# not print a confirmation". With --assume-sent these count as done.
PROBABLE_SENT = {"needs_manual"}


def is_done(res, assume_sent=False):
    st = res.get("status")
    if st == "submitted":
        return True
    return bool(assume_sent and st in PROBABLE_SENT)


def main():
    ap = argparse.ArgumentParser(description="Form Blaster - bulk contact form submitter")
    ap.add_argument("--input", "-i", help="file of URLs, one per line")
    ap.add_argument("--limit", type=int, default=0, help="max URLs this run")
    ap.add_argument("--dry-run", action="store_true", help="detect only, do not submit")
    ap.add_argument("--http-only", action="store_true")
    ap.add_argument("--browser-only", action="store_true")
    ap.add_argument("--reset", action="store_true", help="requeue failed + needs_manual")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--export", help="write results CSV")
    ap.add_argument("--concurrency", type=int, default=0)
    ap.add_argument("--assume-sent", action="store_true",
                    help="treat 'needs_manual' as sent (forms that post but do not confirm)")
    args = ap.parse_args()

    db.init_db()
    cfg = db.load_config()

    if args.reset:
        n = db.reset_failed()
        print(f"Requeued {n} failed / needs_manual rows.")

    if args.export:
        n = db.export_csv(args.export)
        print(f"Exported {n} rows to {args.export}")
        return

    if args.stats:
        s = db.stats()
        print(f"total   : {s['total']}")
        for k, v in sorted(s["by_status"].items()):
            print(f"  {k:14s}: {v}")
        if s["by_tier"]:
            print("by tier:")
            for k, v in s["by_tier"].items():
                print(f"  {k:14s}: {v}")
        return

    # ── ingest ──────────────────────────────────────────────────────────────
    if args.input:
        urls = load_urls(args.input)
        added, dupes = db.add_urls(urls)
        print(f"Loaded {len(urls)} urls -> {added} new, {dupes} already known")

    todo = db.pending(limit=args.limit or None)
    if not todo:
        print("Nothing pending. Use --stats or --export.")
        return

    if args.dry_run:
        cfg["_dry"] = True

    conc = args.concurrency or cfg.get("concurrency", 8)
    print()
    print(f"Processing {len(todo)} site(s)   concurrency={conc}   dry_run={args.dry_run}")
    print(f"  from: {cfg.get('name')} <{cfg.get('email')}>")
    print()

    counters = {}
    done = {"n": 0}
    total = len(todo)
    t0 = time.time()

    def handle(res):
        """
        Tier 2 re-uses an id from Tier 1. Count only the FINAL verdict per id,
        otherwise a rescued site is counted as both a no_form and a submitted.
        """
        rid = res.get("id")
        prev = counters.get(("id", rid))
        if prev is not None:
            counters[prev] = counters.get(prev, 1) - 1
        counters[("id", rid)] = res.get("status")
        counters[res.get("status")] = counters.get(res.get("status"), 0) + 1

        emit(res, counters)

        if not args.dry_run:
            persist(res)
        done["n"] += 1
        if done["n"] == total or done["n"] % 10 == 0:
            el = time.time() - t0
            rate = done["n"] / el if el else 0
            eta = (total - done["n"]) / rate if rate else 0
            print(f"  {C_DIM}{bar(done['n'], total)}  {rate:.2f}/s  eta {eta/60:.1f}m{C_END}")

    # ── Tier 1: HTTP ────────────────────────────────────────────────────────
    t1_results = []
    if not args.browser_only:
        print(f"{C_DIM}--- Tier 1: HTTP ---{C_END}")
        from core import submitter_http
        import threading

        local = threading.local()

        def work(site):
            if not hasattr(local, "client"):
                local.client = submitter_http._client(cfg)
            # Resolve spintax + [Name]-style placeholders per target.
            site_cfg = spin.spin_fields(cfg, values=site)
            try:
                r = submitter_http.attempt(site["url"], site_cfg, local.client)
            except Exception as e:
                r = dict(ok=False, status="failed", tier="http", error=str(e)[:200])
            r["id"] = site["id"]
            r["url"] = site["url"]
            return r

        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = [ex.submit(work, s) for s in todo]
            for fut in as_completed(futs):
                res = fut.result()
                t1_results.append(res)
                handle(res)

    # ── Tier 2: browser fallback ────────────────────────────────────────────
    if cfg.get("use_browser_fallback") and not args.http_only:
        retry = [r for r in t1_results
                 if r["status"] in ("no_form", "failed", "needs_manual", "blocked")]
        retry_ids = {r["id"] for r in retry}
        if retry_ids:
            print()
            print(f"{C_DIM}--- Tier 2: browser for {len(retry_ids)} unresolved ---{C_END}")
            lookup = {s["id"]: s for s in todo}
            batch = [lookup[i] for i in retry_ids if i in lookup]
            from core import submitter_browser
            submitter_browser.run_batch(batch, cfg, on_result=handle)

    # ── summary ─────────────────────────────────────────────────────────────
    el = time.time() - t0
    print()
    print("=" * 58)
    sub = counters.get("submitted", 0)
    man = counters.get("needs_manual", 0)
    print(f"  submitted     : {sub}")
    print(f"  needs_manual  : {man}  (form filled+posted, no confirmation)")
    if args.assume_sent:
        print(f"  >>> probable sent (submitted + needs_manual): {sub + man}")
    print(f"  blocked       : {counters.get('blocked', 0)}  (WAF / rate-limited)")
    print(f"  no_form       : {counters.get('no_form', 0)}")
    print(f"  failed        : {counters.get('failed', 0)}")
    print(f"  elapsed       : {el/60:.1f} min")
    if total:
        print(f"  success rate  : {100*sub/total:.1f}%  ({sub}/{total})")
    print("=" * 58)
    if not args.dry_run:
        print(f"  results saved to data/formblaster.db")
        print(f"  export:  python run.py --export results.csv")


_CTX = None

if __name__ == "__main__":
    import threading
    main()
