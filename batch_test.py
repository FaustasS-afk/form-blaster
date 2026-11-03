"""
batch_test.py — honest measurement of detection + submission on real leads.

Runs the HTTP tier over a random sample of the real harvested leads and writes
a full per-site report so failures can be diagnosed rather than guessed at.

  python batch_test.py --n 60
  python batch_test.py --n 60 --concurrency 8
"""

import argparse
import csv
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core import db, spin, submitter_http

db.init_db()
cfg = db.load_config()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--file", default="leads_independent.txt")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    urls = [l.strip() for l in open(BASE / args.file, encoding="utf-8") if l.strip()]
    random.seed(args.seed)
    sample = random.sample(urls, min(args.n, len(urls)))
    print(f"sampling {len(sample)} of {len(urls)} from {args.file}")
    if args.dry_run:
        print("DRY RUN — detecting only\n")
    print()

    import threading
    local = threading.local()
    results = []
    t0 = time.time()

    def work(u):
        if not hasattr(local, "client"):
            local.client = submitter_http._client(cfg)
        try:
            r = submitter_http.attempt(u, spin.spin_fields(cfg), local.client)
        except Exception as e:
            r = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:90]}
        r["url"] = u
        return r

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(work, u) for u in sample]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result(timeout=120)
            except Exception as e:
                r = {"status": "failed", "url": "?", "error": f"timeout: {e}"[:70]}
            results.append(r)
            st = r.get("status", "?")
            flag = {"submitted": "OK ", "needs_manual": "MAN", "blocked": "BLK",
                    "no_form": "---", "failed": "ERR"}.get(st, " ? ")
            short = r["url"].replace("https://", "").replace("http://", "").rstrip("/")[:46]
            d = (r.get("detail") or r.get("error") or "")[:44]
            print(f"  [{i:>3}] {flag} {st:<13} {short:<48} {d}", flush=True)

    el = time.time() - t0
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print()
    print("=" * 62)
    print(f"  sampled        : {len(sample)}")
    for k in ("submitted", "needs_manual", "blocked", "no_form", "failed"):
        if counts.get(k):
            print(f"  {k:<14} : {counts[k]}")
    found = counts.get("submitted", 0) + counts.get("needs_manual", 0)
    print(f"  form filled    : {found}/{len(sample)}  ({100*found//max(len(sample),1)}%)")
    print(f"  probable sent  : {found}")
    print(f"  elapsed        : {el/60:.1f} min  ({el/len(sample):.1f}s/site)")
    print("=" * 62)

    out = BASE / "batch_report.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url", "status", "tier", "http_code", "detail", "error",
                    "form_action", "fields_hit"])
        for r in results:
            w.writerow([r.get("url"), r.get("status"), r.get("tier"),
                        r.get("http_code"), r.get("detail", ""),
                        r.get("error", ""), r.get("form_action", ""),
                        r.get("fields_hit", "")])
    print(f"report -> {out.name}")


if __name__ == "__main__":
    main()
