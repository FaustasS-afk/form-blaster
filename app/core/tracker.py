"""
core/tracker.py — outreach tracking + comment rotation.

Records, per target, whether they were followed / liked / commented / DM'd,
and when. This is what lets you engage one day and DM another without
double-following anyone or losing track of who is owed a message.
"""

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from data.db import get_connection

# Fallback pool if config/comments.json is missing.
# Deliberately generic + safe for jewellery / vintage / resale / thrift niches.
DEFAULT_COMMENTS = [
    "Love this 😍",
    "This is stunning",
    "So beautiful 🔥",
    "What a find!",
    "Gorgeous piece",
    "Obsessed with this one",
    "Absolutely stunning ✨",
    "This is gorgeous",
    "Such a great find 👏",
    "Beautiful! 😍",
    "Wow this is lovely",
    "Love your feed",
    "What a beauty 😍",
    "Stunning work 🔥",
    "This caught my eye immediately",
]


def _now():
    return datetime.now().isoformat()


class Tracker:
    def __init__(self):
        self.conn = get_connection()
        self.comments = self._load_comments()

    # ── comments ──────────────────────────────────────────────────────────────

    def _load_comments(self):
        p = Path(__file__).parent.parent / 'config' / 'comments.json'
        if p.exists():
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list) and data:
                    return [str(c) for c in data if str(c).strip()]
                if isinstance(data, dict) and data.get('comments'):
                    return [str(c) for c in data['comments'] if str(c).strip()]
            except Exception:
                pass
        return list(DEFAULT_COMMENTS)

    def get_random_comment(self):
        return random.choice(self.comments) if self.comments else ""

    # ── record keeping ────────────────────────────────────────────────────────

    def _ensure(self, username):
        c = self.conn.cursor()
        c.execute("SELECT username FROM outreach_log WHERE username = ?", (username,))
        if not c.fetchone():
            c.execute(
                "INSERT OR IGNORE INTO outreach_log (username, first_seen) VALUES (?, ?)",
                (username, _now())
            )
            self.conn.commit()

    def mark_followed(self, username):
        self._ensure(username)
        c = self.conn.cursor()
        c.execute(
            "UPDATE outreach_log SET followed = 1, followed_at = ? WHERE username = ?",
            (_now(), username)
        )
        self.conn.commit()

    def mark_liked(self, username, count=1):
        self._ensure(username)
        c = self.conn.cursor()
        c.execute(
            "UPDATE outreach_log SET liked = 1, liked_count = liked_count + ?, liked_at = ? "
            "WHERE username = ?",
            (count, _now(), username)
        )
        self.conn.commit()

    def mark_commented(self, username, text=''):
        self._ensure(username)
        c = self.conn.cursor()
        c.execute(
            "UPDATE outreach_log SET commented = 1, comment_text = ?, commented_at = ? "
            "WHERE username = ?",
            (text, _now(), username)
        )
        self.conn.commit()

    def mark_dmed(self, username):
        self._ensure(username)
        c = self.conn.cursor()
        c.execute(
            "UPDATE outreach_log SET dmed = 1, dmed_at = ? WHERE username = ?",
            (_now(), username)
        )
        self.conn.commit()

    # ── queries for the tracker UI ────────────────────────────────────────────

    def get_all(self, limit=2000):
        c = self.conn.cursor()
        c.execute(
            "SELECT * FROM outreach_log ORDER BY "
            "COALESCE(dmed_at, commented_at, followed_at, first_seen) DESC LIMIT ?",
            (limit,)
        )
        return [dict(r) for r in c.fetchall()]

    def stats(self):
        c = self.conn.cursor()
        q = lambda sql: c.execute(sql).fetchone()[0]
        return {
            "total":      q("SELECT COUNT(*) FROM outreach_log"),
            "followed":   q("SELECT COUNT(*) FROM outreach_log WHERE followed = 1"),
            "liked":      q("SELECT COUNT(*) FROM outreach_log WHERE liked = 1"),
            "commented":  q("SELECT COUNT(*) FROM outreach_log WHERE commented = 1"),
            "dmed":       q("SELECT COUNT(*) FROM outreach_log WHERE dmed = 1"),
            "engaged_not_dmed": q(
                "SELECT COUNT(*) FROM outreach_log "
                "WHERE (followed = 1 OR liked = 1 OR commented = 1) AND dmed = 0"
            ),
            "dmed_not_followed": q(
                "SELECT COUNT(*) FROM outreach_log WHERE dmed = 1 AND followed = 0"
            ),
        }

    def get_engaged_not_dmed(self):
        """
        People already followed/liked/commented but never DM'd.
        This is the 'owes a DM' list.
        """
        c = self.conn.cursor()
        c.execute(
            "SELECT username, followed, liked, commented, "
            "COALESCE(commented_at, liked_at, followed_at) AS last_engage "
            "FROM outreach_log "
            "WHERE (followed = 1 OR liked = 1 OR commented = 1) AND dmed = 0 "
            "ORDER BY last_engage DESC"
        )
        return [dict(r) for r in c.fetchall()]

    def eligible_for_dm(self, min_days=0):
        """
        Engaged-but-not-DM'd, where at least min_days have passed since the
        last engagement. min_days=0 returns everyone.
        """
        rows = self.get_engaged_not_dmed()
        if min_days <= 0:
            return rows
        cutoff = datetime.now() - timedelta(days=min_days)
        out = []
        for r in rows:
            ts = r.get("last_engage")
            try:
                if ts and datetime.fromisoformat(ts) <= cutoff:
                    out.append(r)
            except Exception:
                out.append(r)
        return out

    def get_unfollowed_dmed(self):
        """DM'd but never followed - useful to go back and follow."""
        c = self.conn.cursor()
        c.execute(
            "SELECT username, dmed_at FROM outreach_log "
            "WHERE dmed = 1 AND followed = 0 ORDER BY dmed_at DESC"
        )
        return [dict(r) for r in c.fetchall()]

    # ── backfill ──────────────────────────────────────────────────────────────

    def backfill_from_history(self):
        """
        Seed outreach_log from existing data so past runs are tracked too.
        Marks everyone in sent_log as dmed, and everyone still queued as
        first_seen. Idempotent.
        """
        c = self.conn.cursor()
        added = 0

        # already-DM'd people
        for r in c.execute("SELECT username, sent_at FROM sent_log").fetchall():
            u = r["username"]
            c.execute("SELECT 1 FROM outreach_log WHERE username = ?", (u,))
            if not c.fetchone():
                c.execute(
                    "INSERT INTO outreach_log (username, dmed, dmed_at, first_seen) "
                    "VALUES (?, 1, ?, ?)",
                    (u, r["sent_at"], r["sent_at"])
                )
                added += 1
            else:
                c.execute(
                    "UPDATE outreach_log SET dmed = 1, "
                    "dmed_at = COALESCE(dmed_at, ?) WHERE username = ?",
                    (r["sent_at"], u)
                )

        # queued people get a first_seen row so they show in the tracker
        for r in c.execute("SELECT username FROM queue").fetchall():
            u = r["username"]
            c.execute("SELECT 1 FROM outreach_log WHERE username = ?", (u,))
            if not c.fetchone():
                c.execute(
                    "INSERT INTO outreach_log (username, first_seen) VALUES (?, ?)",
                    (u, _now())
                )
                added += 1

        self.conn.commit()
        return added
