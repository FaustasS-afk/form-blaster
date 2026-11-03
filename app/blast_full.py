"""
Fire the FULL blast: every pending lead, headless, from the app's own engine.

Runs in the app venv so it uses the same DB and config the GUI shows.
Writes progress to blast_full.log and checkpoints so a kill loses nothing.
"""
import sys, os, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

APP = r"C:\Users\SellJ\Desktop\INSTAGRAM OUTREACH"
sys.path.insert(0, APP)
os.chdir(APP)

from outreach import db, spin, submitter_http

LIMIT = 0            # 0 = all pending
CONCURRENCY = 40

db.init_db()
cfg = db.load_config()

todo = db.pending(limit=LIMIT or None)
total = len(todo)
print(f"BLAST START â€” {total} pending leads, {CONCURRENCY} threads", flush=True)
print(f"from: {cfg.get('name')} <{cfg.get('email')}>", flush=True)
print(flush=True)

counts = {}
done = {"n": 0}
t0 = time.time()
lock = threading.Lock()
local = threading.local()
clients = {}


def work(site):
    tid = threading.get_ident()
    if not hasattr(local, "client"):
        c = submitter_http._client(cfg)
        local.client = c
        with lock:
            clients[tid] = c
    site_cfg = spin.spin_fields(cfg, values=site)
    try:
        return submitter_http.attempt(site["url"], site_cfg, local.client)
    except Exception as e:
        return dict(ok=False, status="failed", tier="http", error=f"{type(e).__name__}: {e}"[:150])


def handle(site, res):
    st = res.get("status", "failed")
    with lock:
        counts[st] = counts.get(st, 0) + 1
        done["n"] += 1
        n = done["n"]

    db.mark(site["id"], st, tier=res.get("tier"), http_code=res.get("http_code"),
            detail=res.get("detail", ""), error=res.get("error", ""),
            form_action=res.get("form_action", ""), fields_hit=res.get("fields_hit", ""))

    flag = {"submitted": "OK  ", "needs_manual": "MAYBE", "blocked": "BLK ",
            "no_form": "none", "failed": "ERR "}.get(st, "?   ")
    short = site["url"].replace("https://", "").replace("http://", "").rstrip("/")[:50]
    detail = (res.get("detail") or res.get("error") or "")[:44]
    line = f"[{n:>5}/{total}] {flag} {st:<13} {short:<52} {detail}"
    print(line, flush=True)

    if n % 25 == 0:
        el = time.time() - t0
        rate = n / el * 60 if el else 0
        eta = (total - n) / (n / el) / 60 if n and el else 0
        print(f"    --- {n}/{total}  {rate:.0f}/min  eta {eta:.0f}min  "
              f"{ {k: v for k, v in counts.items()} } ---", flush=True)
        db.checkpoint()


try:
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(work, s): s for s in todo}
        for fut in as_completed(futs):
            site = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = dict(ok=False, status="failed", error=str(e)[:150])
            handle(site, res)
finally:
    for c in clients.values():
        try:
            c.close()
        except Exception:
            pass
    db.checkpoint()

el = time.time() - t0
print()
print("=" * 70)
for k in ("submitted", "needs_manual", "blocked", "no_form", "failed"):
    if counts.get(k):
        print(f"  {k:<14} {counts[k]}")
sub = counts.get("submitted", 0)
man = counts.get("needs_manual", 0)
print(f"  {'-'*14}")
print(f"  PROBABLE SENT  {sub + man}  (confirmed {sub} + maybe {man})")
print(f"  elapsed        {el/60:.1f} min")
print("=" * 70)
print("BLAST COMPLETE")
