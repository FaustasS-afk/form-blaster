"""
contact.py — contact a list of leads through their real contact forms.

  python contact.py leads.txt --dry-run      find + fill each contact form, DO NOT send;
                                             writes screenshots + a review page
  python contact.py leads.txt                send for real
  python contact.py leads.txt --show         watch the browsers work
  python contact.py leads.csv --workers 6    (csv needs a site or url column; name column
                                             fills [Name] in the message)

Never contacts a domain twice: data/contacted.csv is the ledger, and
--skip-db (default: the old tool's data/formblaster.db) adds every site that
tool already posted to. Results: data/runs/<time>.csv (+ review.html on dry runs).
"""

import argparse
import csv
import html
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# On this machine Playwright cannot mkdtemp in the system temp dir (EPERM);
# pin its temp dir inside the project, as run_batch.py does.
import os, tempfile
_PW_TMP = BASE / ".pw-temp"
_PW_TMP.mkdir(exist_ok=True)
for _k in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_k] = str(_PW_TMP)
tempfile.tempdir = str(_PW_TMP)
from core import db, spin
from core.contact_engine import contact_site

LEDGER = BASE / "data" / "contacted.csv"
SENT_LIKE = {"sent", "likely_sent", "unconfirmed"}     # may have reached them: never resend
COLS = ["url", "outcome", "detail", "form_page", "emails", "phones", "secs", "shot", "time"]
LABEL = {"sent": "SENT", "likely_sent": "SENT?", "unconfirmed": "UNCONF", "ready": "READY",
         "rejected": "REJECT", "captcha": "CAPTCHA", "no_form": "NOFORM",
         "unreachable": "DOWN", "failed": "FAIL", "skipped": "SKIP"}


def dom(u):
    n = urlparse(u if "//" in u else "https://" + u).netloc.lower()
    return n[4:] if n.startswith("www.") else n


def load_leads(path):
    p = Path(path)
    if p.suffix.lower() == ".csv":
        rows = list(csv.DictReader(open(p, encoding="utf-8")))
        out = [dict(r, url=r.get("site") or r.get("url") or r.get("form_page") or "") for r in rows]
    else:
        out = [{"url": l.strip()} for l in open(p, encoding="utf-8") if l.strip()]
    seen, leads = set(), []
    for r in out:
        if r["url"] and dom(r["url"]) not in seen:
            seen.add(dom(r["url"]))
            leads.append(r)
    return leads


def already_contacted(skip_db):
    done = set()
    if LEDGER.exists():
        for r in csv.DictReader(open(LEDGER, encoding="utf-8")):
            if r["outcome"] in SENT_LIKE:
                done.add(r["domain"])
    if skip_db and Path(skip_db).exists():
        c = sqlite3.connect(f"file:{Path(skip_db).as_posix()}?mode=ro", uri=True)
        for (d,) in c.execute("SELECT domain FROM sites WHERE status IN ('submitted','needs_manual')"):
            done.add((d or "").removeprefix("www."))
        c.close()
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("leads")
    ap.add_argument("--dry-run", action="store_true", help="fill forms but never click send")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", action="store_true", help="visible browser windows")
    ap.add_argument("--skip-db", default=str(BASE / "data" / "formblaster.db"),
                    help="old tool's database: its sent/maybe sites are skipped")
    args = ap.parse_args()

    cfg = db.load_config()
    leads = load_leads(args.leads)
    done = already_contacted(args.skip_db)
    todo = [l for l in leads if dom(l["url"]) not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(leads)} leads, {len(leads) - len([l for l in leads if dom(l['url']) not in done])} "
          f"already contacted (skipped), {len(todo)} to do   "
          f"{'DRY RUN, nothing is sent' if args.dry_run else 'SENDING FOR REAL'}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = BASE / "data" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    out_csv = run_dir / f"{stamp}{'-dry' if args.dry_run else ''}.csv"
    shot_dir = run_dir / f"{stamp}-shots"
    shot_dir.mkdir(exist_ok=True)

    lock = threading.Lock()
    fout = open(out_csv, "w", newline="", encoding="utf-8")
    w = csv.DictWriter(fout, fieldnames=COLS, extrasaction="ignore")
    w.writeheader()
    new_ledger = not LEDGER.exists()
    fled = open(LEDGER, "a", newline="", encoding="utf-8")
    lw = csv.writer(fled)
    if new_ledger:
        lw.writerow(["domain", "outcome", "form_page", "time"])
    counts, n = {}, [0]
    t0 = time.time()
    queue = list(todo)

    def worker():
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            try:
                browser = pw.chromium.launch(channel="msedge", headless=not args.show)
            except Exception:
                browser = pw.chromium.launch(headless=not args.show)
            ctx = browser.new_context(viewport={"width": 1366, "height": 900},
                                      ignore_https_errors=True, locale="en-US")
            while True:
                with lock:
                    if not queue:
                        break
                    lead = queue.pop(0)
                site_cfg = spin.spin_fields(cfg, values=lead)
                res = contact_site(ctx, lead["url"], site_cfg, dry_run=args.dry_run, shot_dir=shot_dir)
                res["time"] = time.strftime("%H:%M:%S")
                with lock:
                    n[0] += 1
                    counts[res["outcome"]] = counts.get(res["outcome"], 0) + 1
                    w.writerow(res)
                    fout.flush()
                    if not args.dry_run:
                        lw.writerow([dom(lead["url"]), res["outcome"], res["form_page"], res["time"]])
                        fled.flush()
                    print(f"[{n[0]:>4}/{len(todo)}] {LABEL.get(res['outcome'], res['outcome']):<7} "
                          f"{dom(lead['url'])[:40]:<40} {res['detail'][:70]}", flush=True)
            ctx.close()
            browser.close()
        finally:
            pw.stop()

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = [ex.submit(worker) for _ in range(max(1, args.workers))]
        for f in futs:
            try:
                f.result()
            except Exception as e:
                print(f"  worker crashed: {type(e).__name__}: {e}", flush=True)
    fout.close()
    fled.close()

    el = time.time() - t0
    print("\n" + "=" * 60)
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:<12} {v}")
    print(f"  time         {el/60:.1f} min   results: {out_csv.relative_to(BASE)}")
    if args.dry_run:
        page = review_page(out_csv, run_dir / f"{stamp}-review.html")
        print(f"  review       {page.relative_to(BASE)}  (screenshot of every form it chose)")
    print("=" * 60)


def review_page(csv_path, out):
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    cards = []
    for r in rows:
        img = (f'<img src="{html.escape(Path(r["shot"]).parent.name + "/" + Path(r["shot"]).name)}">'
               if r.get("shot") else "")
        cards.append(f'<div class="c {html.escape(r["outcome"])}"><b>{html.escape(r["outcome"].upper())}</b> '
                     f'<a href="{html.escape(r["form_page"] or r["url"])}">{html.escape(dom(r["url"]))}</a>'
                     f'<p>{html.escape(r["detail"])}</p>'
                     f'<p class="m">{html.escape(r["emails"])} {html.escape(r["phones"])}</p>{img}</div>')
    out.write_text(
        "<!doctype html><meta charset=utf-8><title>Dry run review</title><style>"
        "body{font:14px system-ui;margin:16px;background:#f6f7f9}"
        ".g{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}"
        ".c{background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px}"
        ".c img{max-width:100%;border:1px solid #eee;margin-top:6px}.m{color:#666}"
        ".ready b{color:#16a34a}.no_form b,.unreachable b{color:#888}.captcha b,.failed b{color:#d97706}"
        "</style><h1>Dry run review</h1><div class=g>" + "".join(cards) + "</div>",
        encoding="utf-8")
    return out


if __name__ == "__main__":
    main()
