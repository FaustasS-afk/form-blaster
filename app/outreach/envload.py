"""
core/envload.py — read API keys from a .env file or the environment.

Keys are looked up in this order:
  1. real environment variables
  2. .env next to the project root
  3. config/.env
  4. a shared secrets file (configured by FORM_BLASTER_ENV)
"""

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
APP_ROOT = BASE.parent

ENV_FILES = [
    APP_ROOT / ".env",
    BASE / ".env",
    Path(r"C:\Users\SellJ\Documents\deepseek-harness\default-workspace\.secrets\env.txt"),
]

_loaded = False


def _parse_line(line):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    k, _, v = line.partition("=")
    k = k.strip()
    v = v.strip().strip('"').strip("'")
    # the shared secrets file can have a stray ']' glued from a curl comment
    v = v.rstrip("]").strip()
    # a stray shell-style export prefix
    k = k.replace("export ", "").strip()
    if not k or " " in k:
        return None
    return k, v


def load_env(force=False):
    """Populate os.environ from any .env files found. Idempotent."""
    global _loaded
    if _loaded and not force:
        return
    extra = os.environ.get("FORM_BLASTER_ENV")
    files = ([Path(extra)] if extra else []) + ENV_FILES
    for f in files:
        try:
            if not f.exists():
                continue
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    kv = _parse_line(line)
                    if kv:
                        os.environ.setdefault(kv[0], kv[1])
        except Exception:
            continue
    _loaded = True


def get(*names, default=""):
    """First non-empty value among `names`, or default."""
    load_env()
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


def brightdata():
    """(api_key, zone) for Bright Data. ('', '') if not configured."""
    key = get("BRIGHTDATA_API_KEY", "BRIGHT_DATA_API_KEY", "BRD_API_KEY",
              "BRIGHTDATA_TOKEN", "BRIGHT_DATA_KEY")
    zone = get("BRIGHTDATA_ZONE", "BRD_ZONE", "BRIGHTDATA_UNLOCKER_ZONE",
               default="web_unlocker1")
    return key, zone
