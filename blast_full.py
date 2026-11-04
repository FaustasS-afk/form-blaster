"""
blast_full.py — fire the FULL blast from this repo, headless.

This is the standalone-repo equivalent of the original blast_full.py that lived
inside INSTAGRAM OUTREACH. Differences that matter:

  * Paths are relative to THIS file, so the repo runs from any location instead
    of being hardcoded to one folder on one machine.
  * CONCURRENCY is 8, not 40. At 40 the old run tripped the WAF constantly
    (948 leads ended up 'blocked', under 50 sites/min). 8 was always the
    figure MEMORY.md recommended.
  * Writes blast.log, which live_watch.py tails for the live dashboard.
  * Checkpoints every 25 sites, so a kill or crash loses at most 25 draws.

Resumes automatically: db.pending() only returns rows still 'pending', so a
re-run never double-submits a site already marked.
"""
import sys, os, time, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

from core import db, spin, submitter_http

LIMIT = 0          # 0 = every pending lead
# 8 was the old figure, chosen when the run was HTTP-only AND Bright Data was a
# 90s stall per blocked page. Measured 2026-11-03: with curl_cffi + BD off,
# 32 threads is stable and ~4x faster. The old WAF-tripping at 40 was Bright
# Data latency, not raw connection count. Override with BLAST_CONC.
CONCURRENCY = int(os.environ.get("BLAST_CONC", "32"))

LOG = HERE / "blast.log"


class Tee:
    """stdout -> console AND blast.log, so live_watch.py can tail it."""

    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, s):
        sys.__stdout__.write(s)
        self.f.write(s)

    def flush(self):
        sys.__stdout__.flush()
        self.f.flush()


def main():
    db.init_db()
    cfg = db.load_config()

    todo = db.pending(limit=LIMIT or None)
    total = len(todo)

    tee = Tee(LOG)
    sys.stdout = tee
    print("=" * 70, flush=True)
    print(f"BLAST START — {total} pending leads, {CONCURRENCY} threads", flush=True)
    print(f"from: {cfg.get('name')} <{cfg.get('email')}>", flush=True)
    print(f"db  : {db.DB_PATH}", flush=True)
    print(flush=True)

    counts = {}
    done = {"n": 0}
    t0 = time.time()
    lock = threading.Lock()
    local = threading.local()
    clients = {}

    def work(site):
        if not hasattr(local, "client"):
            c = submitter_http._client(cfg)
            local.client = c
            with lock:
                clients[threading.get_ident()] = c
        site_cfg = spin.spin_fields(cfg, values=site)
        try:
            return submitter_http.attempt(site["url"], site_cfg, local.client)
        except Exception as e:
            return dict(ok=False, status="failed", tier="http",
                        error=f"{type(e).__name__}: {e}"[:150])

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
        print(f"[{n:>5}/{total}] {flag} {st:<13} {short:<52} {detail}", flush=True)

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
    print("=" * 70, flush=True)
    for k in ("submitted", "needs_manual", "blocked", "no_form", "failed"):
        if counts.get(k):
            print(f"  {k:<14} {counts[k]}", flush=True)
    sub = counts.get("submitted", 0)
    man = counts.get("needs_manual", 0)
    print(f"  {'-'*14}")
    print(f"  PROBABLE SENT  {sub + man}  (confirmed {sub} + maybe {man})", flush=True)
    print(f"  elapsed        {el/60:.1f} min", flush=True)
    print("=" * 70, flush=True)
    print("BLAST COMPLETE", flush=True)
    tee.flush()


if __name__ == "__main__":
    main()
