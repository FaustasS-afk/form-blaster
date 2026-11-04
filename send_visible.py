"""
send_visible.py — run a send that is UNBUFFERED and watchable.

Two problems this fixes:
  1. `python ... | Tee-Object` buffers, so a long run shows NOTHING until it
     ends. Here we write straight to the log file from the child process, and
     tail it separately.
  2. The db writes via run_batch are fine, but we add a live progress line so
     the user always sees movement.

  python send_visible.py <batchfile> [--show-browser]
"""
import subprocess, sys, time, os
from pathlib import Path

BASE = Path(__file__).resolve().parent
PY = str(BASE / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():
    PY = sys.executable

batch = sys.argv[1]
show = "--show-browser" in sys.argv
logp = BASE / (Path(batch).stem + "_live.log")

env = dict(os.environ)
env["PYTHONUNBUFFERED"] = "1"

args = [PY, "-u", "run_batch.py", "--input", batch,
        "--concurrency", "8", "--site-budget", "70"]
if show:
    args += ["--show-browser", "--slow-mo", "150"]

print("logging to", logp.name, flush=True)
f = open(logp, "w", encoding="utf-8")
p = subprocess.Popen(args, cwd=str(BASE), env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True, encoding="utf-8", errors="replace", bufsize=1)
for line in p.stdout:
    f.write(line)
    f.flush()
    # stdout can be cp1252 and the child emits ANSI escapes + box chars, so a
    # raw write kills the whole run with UnicodeEncodeError. Force ascii-safe.
    try:
        sys.stdout.write(line)
    except UnicodeEncodeError:
        sys.stdout.write(line.encode("ascii", "replace").decode("ascii"))
    sys.stdout.flush()
p.wait()
f.close()
print("\nDONE rc=%s" % p.returncode)
