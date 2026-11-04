"""
probe_forms.py — find which leads have a usable contact form. Sends NOTHING.

  python probe_forms.py leads_new_clean.txt
  python probe_forms.py leads_new_clean.txt --concurrency 16

Writes:
  <name>_probe.csv       every lead: url, status, form_page (resumable: re-run
                         and already-probed leads are skipped)
  <name>_forms.txt       just the form pages, ready for:
                         python run.py --input <name>_forms.txt

The send run then starts on the form page itself, so each site costs 1-2
requests instead of a full scan, and leads with no form are never touched.
"""

import argparse
import csv
import os
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core import db, submitter_http


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("leads")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--stall", type=int, default=180,
                    help="give up on sites still hanging after this many seconds of no progress")
    args = ap.parse_args()

    src = Path(args.leads)
    out_csv = src.with_name(src.stem + "_probe.csv")
    out_txt = src.with_name(src.stem + "_forms.txt")

    db.init_db()
    cfg = dict(db.load_config(), scan_only=True)

    urls = list(dict.fromkeys(l.strip() for l in open(src, encoding="utf-8") if l.strip()))
    done = set()
    if out_csv.exists():
        with open(out_csv, encoding="utf-8", newline="") as f:
            done = {r["url"] for r in csv.DictReader(f)}
    todo = [u for u in urls if u not in done]
    print(f"{len(urls)} leads, {len(done)} already probed, {len(todo)} to go")

    local = threading.local()
    lock = threading.Lock()
    new_file = not out_csv.exists()
    f = open(out_csv, "a", encoding="utf-8", newline="")
    w = csv.writer(f)
    if new_file:
        w.writerow(["url", "status", "form_page"])

    def one(u):
        if not hasattr(local, "client"):
            local.client = submitter_http._client(cfg)
        try:
            r = submitter_http.attempt(u, cfg, local.client)
        except Exception as e:
            r = dict(status="failed", error=str(e)[:200])
        return u, r.get("status", "failed"), r.get("form_page", "")

    found = n = 0
    ex = ThreadPoolExecutor(max_workers=args.concurrency)
    futs = {ex.submit(one, u): u for u in todo}
    # Watchdog: one hung site (a server that drips bytes forever) used to
    # block the whole run at 3369/3370 so the _forms.txt was never written.
    # If NOTHING finishes for --stall seconds, mark the stragglers and stop.
    pending = set(futs)
    while pending:
        finished, pending = wait(pending, timeout=args.stall, return_when=FIRST_COMPLETED)
        if not finished:
            print(f"  {len(pending)} site(s) hung for {args.stall}s, marked 'timeout'", flush=True)
            for fu in pending:
                w.writerow([futs[fu], "timeout", ""])
            break
        for fut in finished:
            u, status, page = fut.result()
            n += 1
            found += status == "form_found"
            with lock:
                w.writerow([u, status, page])
                f.flush()
            if n % 25 == 0 or n == len(todo):
                print(f"  {n}/{len(todo)}  forms so far: {found}", flush=True)
    f.close()
    ex.shutdown(wait=False, cancel_futures=True)
    write_forms(out_csv, out_txt)
    if n < len(todo):
        os._exit(0)   # abandon the hung thread(s) instead of waiting forever


def write_forms(out_csv, out_txt):
    with open(out_csv, encoding="utf-8", newline="") as fi:
        pages = [r["form_page"] for r in csv.DictReader(fi)
                 if r["status"] == "form_found" and r["form_page"]]
    pages = list(dict.fromkeys(pages))
    with open(out_txt, "w", encoding="utf-8") as fo:
        fo.write("\n".join(pages) + "\n")
    print(f"done: {len(pages)} leads with a form -> {out_txt.name}")


if __name__ == "__main__":
    main()
