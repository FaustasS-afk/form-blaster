"""
Probe the 3,239 NEVER-TRIED pending rows, then send the ones with forms.

These are raw lead URLs sitting in the db that were never processed. Probing
them is pure upside: no new harvesting needed.

  python probe_pending.py            # probe + send everything still pending
"""
import csv
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
PY = str(BASE / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():
    PY = sys.executable

import sqlite3
c = sqlite3.connect(str(BASE / "data" / "formblaster.db"))
rows = c.execute("select url from sites where status='pending' and tried=0 "
                 "order by id").fetchall()
c.close()

urls = [r[0] for r in rows]
out = BASE / "_pending_raw.txt"
out.write_text("\n".join(urls) + "\n", encoding="utf-8")
print("never-tried pending: %d -> %s" % (len(urls), out.name))

# probe them (sends nothing)
p = subprocess.run([PY, "probe_forms.py", str(out), "--concurrency", "24"],
                   cwd=str(BASE))
print("probe rc:", p.returncode)

# send to whatever has a form
fp = out.with_name(out.stem + "_forms.txt")
if fp.exists():
    n = sum(1 for l in open(fp, encoding="utf-8") if l.strip())
    print("forms found: %d" % n)
    if n:
        subprocess.run([PY, "run_batch.py", "--input", str(fp),
                        "--concurrency", "8", "--site-budget", "70"],
                       cwd=str(BASE))
else:
    print("no forms file produced")
