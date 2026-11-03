"""
core/db.py — SQLite store with crash-safe resume.

One row per URL. Re-running the tool picks up only rows still 'pending',
so a crash at site 4,000 costs you nothing.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "blaster.db"
CONFIG_PATH = BASE / "blaster_config.json"

_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL UNIQUE,
    domain       TEXT,
    status       TEXT DEFAULT 'pending',
    tier         TEXT,
    http_code    INTEGER,
    detail       TEXT DEFAULT '',
    error        TEXT DEFAULT '',
    form_action  TEXT DEFAULT '',
    fields_hit   TEXT DEFAULT '',
    tried        INTEGER DEFAULT 0,
    first_seen   TEXT,
    last_try     TEXT
);

CREATE INDEX IF NOT EXISTS idx_sites_status ON sites (status);
CREATE INDEX IF NOT EXISTS idx_sites_domain ON sites (domain);
"""


def get_conn():
    """
    Shared SQLite connection.

    WAL is ON for concurrent GUI + worker access, but WAL means data can sit in
    the -wal sidecar and look 'missing' to another process reading the main
    file. `checkpoint()` below flushes it, and `close_conn()` is called on app
    exit so the DB is never left mid-transaction.
    """
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=30000")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def checkpoint():
    """Flush the WAL into the main DB file. Safe to call any time."""
    try:
        c = get_conn()
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.commit()
        return True
    except Exception:
        return False


def close_conn():
    """Commit and close. Call on app shutdown."""
    global _conn
    if _conn is not None:
        try:
            _conn.commit()
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _conn.close()
        except Exception:
            pass
        _conn = None


def init_db():
    c = get_conn()
    c.executescript(SCHEMA)
    c.commit()


def load_config():
    """
    Read the blaster config.

    utf-8-sig, not utf-8: PowerShell's Set-Content writes a BOM by default and
    json.load chokes on it with 'Unexpected UTF-8 BOM'. utf-8-sig strips it when
    present and behaves identically when absent.
    """
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_config(cfg):
    """Write the config back without a BOM."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _now():
    return datetime.now().isoformat()


def _domain(url):
    try:
        from urllib.parse import urlparse
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def add_urls(urls):
    """Insert URLs, ignoring dupes. Returns (added, skipped)."""
    c = get_conn()
    added = 0
    for u in urls:
        u = u.strip()
        if not u:
            continue
        if not u.startswith("http"):
            u = "https://" + u
        try:
            c.execute(
                "INSERT OR IGNORE INTO sites (url, domain, status, first_seen) "
                "VALUES (?, ?, 'pending', ?)",
                (u, _domain(u), _now())
            )
            if c.total_changes:
                added += 1
        except Exception:
            pass
    c.commit()
    total = c.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
    return added, total - added


def pending(limit=None):
    c = get_conn()
    sql = "SELECT * FROM sites WHERE status = 'pending' ORDER BY id ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in c.execute(sql)]


def mark(site_id, status, tier=None, http_code=None, detail="", error="",
         form_action="", fields_hit=""):
    c = get_conn()
    c.execute(
        "UPDATE sites SET status=?, tier=COALESCE(?, tier), http_code=?, "
        "detail=?, error=?, form_action=?, fields_hit=?, "
        "tried = tried + 1, last_try=? WHERE id=?",
        (status, tier, http_code, detail[:2000], error[:2000],
         form_action, fields_hit, _now(), site_id)
    )
    c.commit()


def stats():
    c = get_conn()
    q = lambda s: c.execute(s).fetchone()[0]
    rows = c.execute("SELECT status, COUNT(*) AS n FROM sites GROUP BY status").fetchall()
    out = {"total": q("SELECT COUNT(*) FROM sites"), "by_status": {}}
    for r in rows:
        out["by_status"][r["status"]] = r["n"]
    out["by_tier"] = {}
    for r in c.execute("SELECT tier, COUNT(*) AS n FROM sites WHERE tier IS NOT NULL GROUP BY tier"):
        out["by_tier"][r["tier"]] = r["n"]
    return out


def export_csv(path):
    import csv
    c = get_conn()
    rows = c.execute(
        "SELECT url, domain, status, tier, http_code, detail, error, "
        "form_action, fields_hit, tried, last_try FROM sites ORDER BY id"
    ).fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url", "domain", "status", "tier", "http_code", "detail",
                    "error", "form_action", "fields_hit", "tried", "last_try"])
        for r in rows:
            w.writerow(list(r))
    return len(rows)


def reset_failed():
    c = get_conn()
    c.execute("UPDATE sites SET status='pending' WHERE status IN ('failed','needs_manual')")
    n = c.total_changes
    c.commit()
    return n
