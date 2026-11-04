"""
overnight.py — run all night, unattended: harvest -> probe -> send, on a loop.

Cycle:
  1. harvest more leads (OSM, resumable, never repeats a unit)
  2. probe any new leads for a form (skips already-probed)
  3. send to any NEWLY found form pages (db-unique, so NEVER double-sends)

The no-double-send guarantee: run_batch.py only ever runs status='pending' rows,
so a page submitted to once is never submitted again. That is what the user
asked for -- no re-runs, no spam.

    python overnight.py                 # loop forever
    python overnight.py --rounds 3
"""

import argparse
import csv
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PY = str(BASE / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():
    PY = sys.executable
WORLD = BASE / "leads_world"
ALL_TXT = WORLD / "leads_world_all.txt"
FORMS_TXT = WORLD / "leads_world_all_forms.txt"
LOG = WORLD / "_overnight.log"

# countries to keep grinding; the harvester itself skips completed units
COUNTRIES = ["fr", "it", "es", "ca", "de", "nl", "be", "at", "se", "dk",
             "no", "fi", "au", "nz", "ae", "ie", "pt"]


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(args, timeout=7200):
    log("$ " + " ".join(str(a) for a in args[1:3]))
    try:
        p = subprocess.run(args, cwd=str(BASE), timeout=timeout,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        tail = (p.stdout or "").strip().splitlines()[-6:]
        for t in tail:
            log("   " + t)
        return p.returncode
    except subprocess.TimeoutExpired:
        log("   TIMEOUT")
        return -1


def count_leads():
    if not ALL_TXT.exists():
        return 0
    return sum(1 for l in open(ALL_TXT, encoding="utf-8") if l.strip())


def build_combined():
    """Rebuild leads_world_all.txt from every per-country csv."""
    seen, sites = set(), []
    for p in sorted(WORLD.glob("leads_*.csv")):
        if "world_all" in p.name:
            continue
        for r in csv.DictReader(open(p, encoding="utf-8")):
            if r["domain"] in seen:
                continue
            seen.add(r["domain"])
            if r.get("site"):
                sites.append(r["site"])
    ALL_TXT.write_text("\n".join(sorted(sites)) + "\n", encoding="utf-8")
    return len(sites)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=0)
    args = ap.parse_args()

    log("=" * 60)
    log("OVERNIGHT RUNNER START — harvest -> probe -> send")
    log("=" * 60)

    rnd = 0
    while True:
        rnd += 1
        log("")
        log("### ROUND %d ###" % rnd)

        # ── 1. harvest ──────────────────────────────────────────────────────
        for cc in COUNTRIES:
            run([PY, "harvest_mailru.py", cc], timeout=5400)
        n = build_combined()
        log("combined leads: %d" % n)

        # ── 2. probe new leads ──────────────────────────────────────────────
        run([PY, "probe_forms.py", str(ALL_TXT), "--concurrency", "24"],
            timeout=10800)

        # ── 3. send to any never-tried form page ────────────────────────────
        if FORMS_TXT.exists():
            run([PY, "run_batch.py", "--input", str(FORMS_TXT),
                 "--concurrency", "16"], timeout=10800)

        log("round %d done. leads=%d" % (rnd, count_leads()))
        if args.rounds and rnd >= args.rounds:
            break
        time.sleep(30)

    log("OVERNIGHT RUNNER FINISHED")


if __name__ == "__main__":
    main()
