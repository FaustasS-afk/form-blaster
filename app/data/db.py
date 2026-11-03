"""
data/db.py — SQLite store for the DM engine, queue, accounts and tracker.

This is the ``data.db`` module (``get_connection`` / ``init_db``), NOT the
contact-form engine's ``outreach/db.py`` (``get_conn``). Both files are named
db.py and both hold a ``blaster.db`` — this one lives next to this file, in
``data/``, while the outreach one lives in ``outreach/``.

Design notes:

* A single shared connection is cached in ``_conn``. ``check_same_thread=False``
  because the GUI (main thread) and the DM worker (``QThread``) both use it.
* WAL keeps the GUI reading while the worker writes. The cost is that freshly
  written rows can live in the ``-wal`` sidecar until a checkpoint, so
  ``checkpoint()`` is exposed for anything that needs the main file current.
* ``busy_timeout`` + ``synchronous=NORMAL`` mirror the outreach store, and stop
  the classic "database is locked" freeze when both sides write at once.
* ``init_db()`` is CREATE ... IF NOT EXISTS only, so calling it on every start
  (and from several panels) is idempotent and never destroys existing data.

Schema is derived from the actual SQL in the consumers — see the column
comments. Nothing here is speculative: every column below is read or written by
``core/account_manager.py``, ``core/queue_manager.py``, ``core/tracker.py`` or a
GUI panel.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "blaster.db"

_conn = None

# ── Schema ────────────────────────────────────────────────────────────────────
# Tables owned by this DB. `sites` is mirrored from outreach/db.py's SCHEMA so
# that a connection opened through this module also has the leads table; the
# column list is copied verbatim from that authoritative definition.
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

-- Instagram accounts the engine sends from.
-- add_account() relies on the UNIQUE(username) constraint: it catches
-- 'UNIQUE constraint failed' to report "Account already exists".
-- get_next_available() filters on status='Active' AND dms_sent_today <
-- daily_limit, and _check_resets() writes last_reset/dms_sent_today.
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    daily_limit     INTEGER DEFAULT 50,
    dms_sent_today  INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'Active',
    last_reset      TEXT,
    proxy           TEXT DEFAULT ''
);

-- Main engine queue. add_single() catches 'UNIQUE constraint failed' to report
-- "Already in queue", and force_add_single() uses INSERT OR REPLACE, so
-- username must be UNIQUE. status values observed in the code: 'Pending',
-- 'Pending Followup', 'Engaged', 'Sent', 'Failed', 'Skipped'.
CREATE TABLE IF NOT EXISTS queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    status      TEXT DEFAULT 'Pending',
    timestamp   TEXT,
    error_msg   TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue (status);

-- One row per DM actually sent. Used as the "already messaged" filter and
-- counted per username for the multi-DM badge in the follow-up panel.
CREATE TABLE IF NOT EXISTS sent_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL,
    account_id   INTEGER,
    sent_at      TEXT,
    message_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_sent_log_username ON sent_log (username);

-- Hard-block list. add_to_blacklist() INSERTs without a guard and swallows the
-- error, so UNIQUE(username) keeps it clean.
CREATE TABLE IF NOT EXISTS blacklist (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT NOT NULL UNIQUE,
    added_at  TEXT
);

-- Append-only audit of every terminal queue outcome, written by
-- update_status(); the dashboard reads it for totals, today's count and the
-- success rate (status IN ('Sent','Failed')).
CREATE TABLE IF NOT EXISTS historical_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT,
    status     TEXT,
    timestamp  TEXT,
    error_msg  TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_historical_status ON historical_log (status);
CREATE INDEX IF NOT EXISTS idx_historical_timestamp ON historical_log (timestamp);

-- Follow-up campaign queue. add_to_followup() uses INSERT OR IGNORE and then
-- UPDATEs on rowcount == 0, so username must be UNIQUE.
CREATE TABLE IF NOT EXISTS followup_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL UNIQUE,
    queued_at  TEXT,
    status     TEXT DEFAULT 'Pending'
);

CREATE INDEX IF NOT EXISTS idx_followup_status ON followup_queue (status);

-- Engagement tracker: one row per target, flags instead of separate tables.
-- _ensure() relies on the UNIQUE(username) constraint for INSERT OR IGNORE,
-- and its "does this row exist" SELECT guards every mark_*() call.
-- mark_liked() does `liked_count = liked_count + ?`, which needs a non-NULL
-- default or the arithmetic yields NULL.
CREATE TABLE IF NOT EXISTS outreach_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    followed      INTEGER DEFAULT 0,
    followed_at   TEXT,
    liked         INTEGER DEFAULT 0,
    liked_count   INTEGER DEFAULT 0,
    liked_at      TEXT,
    commented     INTEGER DEFAULT 0,
    comment_text  TEXT DEFAULT '',
    commented_at  TEXT,
    dmed          INTEGER DEFAULT 0,
    dmed_at       TEXT,
    first_seen    TEXT
);

CREATE INDEX IF NOT EXISTS idx_outreach_dmed ON outreach_log (dmed);
CREATE INDEX IF NOT EXISTS idx_outreach_followed ON outreach_log (followed);
"""


def get_connection():
    """
    Shared SQLite connection to data/blaster.db.

    Callers are core/* and gui/*; several of them keep ``self.conn`` for the
    life of a panel, so this must stay cheap and cached.
    """
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _conn = conn
    return _conn


def init_db():
    """
    Create every table/index if missing. Idempotent and non-destructive, so it
    is safe to call on app start and again from any panel that needs the schema.
    """
    c = get_connection()
    c.executescript(SCHEMA)
    c.commit()
    return c


def checkpoint():
    """Flush the WAL into the main DB file, so a reader of the file sees it."""
    try:
        c = get_connection()
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


def _now():
    return datetime.now().isoformat()


__all__ = [
    "DB_PATH",
    "SCHEMA",
    "get_connection",
    "init_db",
    "checkpoint",
    "close_conn",
]
