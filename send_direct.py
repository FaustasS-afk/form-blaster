"""
send_direct.py — go STRAIGHT to the browser on known form pages.

WHY: Tier 1 (HTTP) re-scans every site (homepage crawl + up to 24 contact path
guesses, scan_good_enough=15) before Tier 2 even starts. On 1,246 URLs that is
~10 min for 20 sites -- 100x slower than needed, because these URLs are ALREADY
known form pages from the probe.

So: skip Tier 1 entirely and run the browser on the list. The browser tier is
the one that actually CONFIRMS (97.5% proven) and it renders JS so an
already-resolved form page loads correctly first try.

  python send_direct.py <batchfile> [--headless]
"""
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

_P = BASE / ".pw-temp"
_P.mkdir(exist_ok=True)
os.environ["TMPDIR"] = os.environ["TEMP"] = os.environ["TMP"] = str(_P)
tempfile.tempdir = str(_P)

from core import db, spin, submitter_browser


def main():
    batch = sys.argv[1]
    show = "--headless" not in sys.argv
    urls = [l.strip() for l in open(batch, encoding="utf-8") if l.strip()]

    db.init_db()
    cfg = db.load_config()
    cfg["show_browser"] = show
    if show:
        cfg["slow_mo"] = 150
    cfg["browser_site_budget"] = 60

    conn = db.get_conn()
    q = ",".join("?" * len(urls))
    todo = [dict(r) for r in conn.execute(
        "select * from sites where url in (%s) and status='pending' order by id" % q, urls)]

    print("batch: %d urls, %d pending -> BROWSER DIRECT" % (len(urls), len(todo)))
    print("visible: %s" % show)
    print()

    counters, seen_ids = {}, {}

    def handle(res):
        rid = res.get("id")
        prev = seen_ids.get(rid)
        if prev:
            counters[prev] = counters.get(prev, 1) - 1
        seen_ids[rid] = res.get("status")
        counters[res.get("status")] = counters.get(res.get("status"), 0) + 1
        db.mark(res["id"], res.get("status", "failed"), tier=res.get("tier"),
                http_code=res.get("http_code"), detail=res.get("detail", ""),
                error=res.get("error", ""), form_action=res.get("form_action", ""),
                fields_hit=res.get("fields_hit", ""))
        st = res.get("status")
        mark = {"submitted": "OK  ", "needs_manual": "MAN ",
                "no_form": "NONE", "failed": "FAIL", "timeout": "TIME"}.get(st, st)
        print("  %-5s %-58s %s" % (mark, res["url"][:58],
                                   (res.get("detail") or "")[:44]), flush=True)

    submitter_browser.run_batch(todo, cfg, on_result=handle)

    print()
    print("=" * 60)
    print("  BROWSER DIRECT COMPLETE")
    for k in ("submitted", "needs_manual", "no_form", "failed", "timeout"):
        if counters.get(k):
            print("  %-14s %d" % (k, counters[k]))
    print("  CONFIRMED     : %d" % counters.get("submitted", 0))
    print("=" * 60)


if __name__ == "__main__":
    main()
