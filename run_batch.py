"""
run_batch.py — run an EXPLICIT set of URLs only, ignoring everything else
already sitting 'pending' in the database.

The stock run.py does:

    SELECT * FROM sites WHERE status='pending' ORDER BY id ASC

so any older pending rows queue-jump a newly imported file. This runner scopes
the work to one input file, so "run the new batch" actually means the new batch.

    python run_batch.py --input leads.txt               # this file only
    python run_batch.py --input leads.txt --dry-run     # detect only
    python run_batch.py --input leads.txt --tag new     # label the cohort

Everything else (tiers, retries, persistence) is identical to run.py.
"""

import argparse
import sys
import threading
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

# statuses that mean a form was found, filled and posted
FORM_HIT = ("submitted", "needs_manual")


def load_urls(path):
    p = Path(path)
    if not p.exists():
        print(f"{C_ERR}Input file not found: {path}{C_END}")
        sys.exit(1)
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line.split(",")[0].strip() if "," in line else line)
    return out


def norm(u):
    u = u.strip()
    return u if u.startswith("http") else "https://" + u


def bar(done, total, width=32):
    if not total:
        return ""
    filled = int(width * done / total)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {done}/{total}"


def emit(res):
    st = res.get("status")
    url = res.get("url", "")
    short = url.replace("https://", "").replace("http://", "").rstrip("/")[:52]
    tier = res.get("tier") or "-"
    if st == "submitted":
        print(f"  {C_OK}OK{C_END}   [{tier}] {short}  {C_DIM}{res.get('detail','')}{C_END}", flush=True)
    elif st == "needs_manual":
        print(f"  {C_WARN}MAN{C_END}  [{tier}] {short}  {C_DIM}{res.get('detail','')}{C_END}", flush=True)
    elif st == "blocked":
        print(f"  {C_WARN}BLK{C_END}  [{tier}] {short}  {C_DIM}{res.get('detail','')}{C_END}", flush=True)
    elif st == "no_form":
        print(f"  {C_DIM}NONE [{tier}] {short}{C_END}", flush=True)
    else:
        print(f"  {C_ERR}FAIL{C_END} [{tier}] {short}  {C_DIM}{str(res.get('error',''))[:60]}{C_END}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Form Blaster - run ONE batch only")
    ap.add_argument("--input", "-i", required=True, help="file of URLs, one per line")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--http-only", action="store_true")
    ap.add_argument("--concurrency", type=int, default=0)
    ap.add_argument("--reset-first", action="store_true",
                    help="requeue this batch's already-tried rows before starting")
    ap.add_argument("--include-retried", action="store_true",
                    help="also re-attempt rows in this batch already attempted")
    args = ap.parse_args()

    db.init_db()
    cfg = db.load_config()

    urls = [norm(u) for u in load_urls(args.input)]
    print(f"Batch file: {args.input}  ({len(urls)} urls)")

    added, _ = db.add_urls(urls)
    print(f"  {added} were new to the db, {len(urls)-added} already known")

    # ── scope strictly to this file ─────────────────────────────────────────
    conn = db.get_conn()
    qmarks = ",".join("?" * len(urls))

    if args.reset_first:
        n = conn.execute(
            f"UPDATE sites SET status='pending' WHERE url IN ({qmarks})", urls).rowcount
        conn.commit()
        print(f"  reset {n} rows in this batch back to pending")

    where = f"url IN ({qmarks})"
    if not args.include_retried:
        where += " AND status='pending'"

    sql = f"SELECT * FROM sites WHERE {where} ORDER BY id ASC"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    todo = [dict(r) for r in conn.execute(sql, urls)]

    if not todo:
        print("\nNothing to do for this batch. "
              "(use --include-retried or --reset-first to re-run it)")
        return

    conc = args.concurrency or cfg.get("concurrency", 8)
    print()
    print("=" * 58)
    print(f"  BATCH-ONLY RUN — {len(todo)} site(s) from {Path(args.input).name}")
    print(f"  concurrency={conc}   dry_run={args.dry_run}")
    print(f"  from: {cfg.get('name')} <{cfg.get('email')}>")
    print("=" * 58)
    print()

    counters = {}
    done = {"n": 0}
    total = len(todo)
    t0 = time.time()

    def handle(res):
        counters[res.get("status")] = counters.get(res.get("status"), 0) + 1
        emit(res)
        if not args.dry_run:
            db.mark(
                res["id"], res.get("status", "failed"), tier=res.get("tier"),
                http_code=res.get("http_code"), detail=res.get("detail", ""),
                error=res.get("error", ""), form_action=res.get("form_action", ""),
                fields_hit=res.get("fields_hit", ""),
            )
        done["n"] += 1
        if done["n"] % 10 == 0 or done["n"] == total:
            el = time.time() - t0
            rate = done["n"] / el if el else 0
            eta = (total - done["n"]) / rate if rate else 0
            forms = sum(counters.get(k, 0) for k in FORM_HIT)
            fpm = forms / (el / 60) if el else 0
            print(f"  {C_DIM}{bar(done['n'], total)}  {rate*60:.0f}/min  "
                  f"forms {forms} ({fpm:.1f}/min)  eta {eta/60:.1f}m{C_END}", flush=True)

    # ── Tier 1: HTTP ────────────────────────────────────────────────────────
    print(f"{C_DIM}--- Tier 1: HTTP ---{C_END}")
    from core import submitter_http

    local = threading.local()

    def work(site):
        if not hasattr(local, "client"):
            local.client = submitter_http._client(cfg)
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
            handle(fut.result())

    # ── summary ─────────────────────────────────────────────────────────────
    el = time.time() - t0
    sub = counters.get("submitted", 0)
    man = counters.get("needs_manual", 0)
    print()
    print("=" * 58)
    print(f"  BATCH COMPLETE — {Path(args.input).name}")
    print(f"  submitted     : {sub}")
    print(f"  needs_manual  : {man}")
    print(f"  >>> forms filled + posted : {sub + man}")
    print(f"  blocked       : {counters.get('blocked', 0)}")
    print(f"  no_form       : {counters.get('no_form', 0)}")
    print(f"  failed        : {counters.get('failed', 0)}")
    print(f"  elapsed       : {el/60:.1f} min")
    if total:
        print(f"  form hit rate : {100*(sub+man)/total:.1f}%  ({sub+man}/{total})")
    print("=" * 58)


if __name__ == "__main__":
    main()
