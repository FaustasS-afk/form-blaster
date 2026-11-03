import os
import re
from data.db import get_connection
from datetime import datetime

class QueueManager:
    def __init__(self):
        self.conn = get_connection()

    def import_txt(self, filepath):
        if not os.path.exists(filepath):
            return 0, 0, 0 # added, duplicates, blacklisted/sent
            
        added_count = 0
        duplicate_count = 0
        filtered_count = 0
        
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        c = self.conn.cursor()

        # ── Load the lookup sets ONCE, in bulk ────────────────────────────────
        # The original ran 2 SELECTs per line inside a transaction. On a busy or
        # contending database that compounds into minutes of UI-thread stalls.
        # Loading both sets up front makes the per-line work pure Python.
        blacklist = {r[0] for r in c.execute("SELECT username FROM blacklist").fetchall()}
        already_sent = {r[0] for r in c.execute("SELECT username FROM sent_log").fetchall()}
        in_queue = {r[0] for r in c.execute("SELECT username FROM queue").fetchall()}

        ts = datetime.now().isoformat()
        batch = []

        for line in lines:
            username = line.strip()
            # Basic validation of instagram username
            if not username or not re.match(r'^[a-zA-Z0-9_\.]+$', username):
                continue

            if username in blacklist or username in already_sent:
                filtered_count += 1
                continue

            if username in in_queue:
                duplicate_count += 1
                continue

            batch.append((username, 'Pending', ts))
            in_queue.add(username)   # also de-dupes repeats WITHIN the same file

        # One executemany instead of N round-trips.
        if batch:
            c.executemany(
                "INSERT OR IGNORE INTO queue (username, status, timestamp) VALUES (?, ?, ?)",
                batch
            )
            added_count = len(batch)

        self.conn.commit()
        return added_count, duplicate_count, filtered_count

    def add_single(self, username):
        username = username.strip()
        if not username or not re.match(r'^[a-zA-Z0-9_\.]+$', username):
            return False, "Invalid username format"
            
        c = self.conn.cursor()
        c.execute("SELECT 1 FROM blacklist WHERE username = ?", (username,))
        if c.fetchone():
            return False, "Username is blacklisted"
            
        c.execute("SELECT 1 FROM sent_log WHERE username = ?", (username,))
        if c.fetchone():
            return False, "Username already messaged"
            
        try:
            c.execute(
                "INSERT INTO queue (username, status, timestamp) VALUES (?, 'Pending', ?)",
                (username, datetime.now().isoformat())
            )
            self.conn.commit()
            return True, "Added successfully"
        except Exception as e:
            if 'UNIQUE constraint failed' in str(e):
                return False, "Already in queue"
            return False, str(e)

    def get_queue(self):
        c = self.conn.cursor()
        c.execute("SELECT id, username, status, timestamp FROM queue ORDER BY id DESC")
        return [dict(row) for row in c.fetchall()]
        
    def get_pending(self):
        """
        Rows still needing work.

        'Engaged' means follow+like already happened in engage-only mode, but
        no DM was sent — so it is still outstanding for a DM run.
        """
        c = self.conn.cursor()
        c.execute(
            "SELECT id, username, status FROM queue "
            "WHERE status IN ('Pending', 'Pending Followup', 'Engaged') "
            "ORDER BY id ASC"
        )
        return [dict(row) for row in c.fetchall()]

    def update_status(self, queue_id, status, error_msg=""):
        c = self.conn.cursor()
        c.execute("UPDATE queue SET status = ?, error_msg = ? WHERE id = ?", (status, error_msg, queue_id))
        if status in ('Sent', 'Failed', 'Engaged'):
            try:
                c.execute("SELECT username FROM queue WHERE id = ?", (queue_id,))
                row = c.fetchone()
                if row:
                    username = row['username']
                    c.execute(
                        "INSERT INTO historical_log (username, status, timestamp, error_msg) VALUES (?, ?, ?, ?)",
                        (username, status, datetime.now().isoformat(), error_msg)
                    )
            except Exception:
                pass
        self.conn.commit()

    def get_sent_count(self, username):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM sent_log WHERE username = ?", (username,))
        row = c.fetchone()
        return row['cnt'] if row else 0

    def update_username(self, queue_id, new_username):
        c = self.conn.cursor()
        c.execute("UPDATE queue SET username = ? WHERE id = ?", (new_username, queue_id))
        self.conn.commit()
        
    def log_sent(self, username, account_id, message):
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO sent_log (username, account_id, sent_at, message_hash) VALUES (?, ?, ?, ?)",
            (username, account_id, datetime.now().isoformat(), str(hash(message)))
        )
        self.conn.commit()

    def remove(self, queue_id):
        c = self.conn.cursor()
        c.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
        self.conn.commit()
        
    def add_to_blacklist(self, username):
        c = self.conn.cursor()
        try:
            c.execute("INSERT INTO blacklist (username) VALUES (?)", (username,))
            self.conn.commit()
        except:
            pass # ignore duplicates

    def clear(self):
        c = self.conn.cursor()
        c.execute("DELETE FROM queue")
        self.conn.commit()

    def force_add_single(self, username):
        """Force-add to main queue, bypassing sent_log and blacklist — for follow-ups."""
        username = username.strip()
        if not username:
            return False, "Empty username"
        c = self.conn.cursor()
        try:
            c.execute(
                "INSERT OR REPLACE INTO queue (username, status, timestamp) VALUES (?, 'Pending Followup', ?)",
                (username, datetime.now().isoformat())
            )
            self.conn.commit()
            return True, "Added"
        except Exception as e:
            return False, str(e)
            
    def remove_duplicates(self):
        """Removes any redundant identical usernames keeping only the oldest."""
        c = self.conn.cursor()
        c.execute('''
            DELETE FROM queue 
            WHERE id NOT IN (
                SELECT MIN(id) 
                FROM queue 
                GROUP BY username
            )
        ''')
        removed = c.rowcount
        self.conn.commit()
        return removed

    # ── Follow-up queue ───────────────────────────────────────────────────

    def get_followup_queue(self):
        c = self.conn.cursor()
        c.execute("SELECT id, username, queued_at, status FROM followup_queue ORDER BY id DESC")
        return [dict(row) for row in c.fetchall()]

    def add_to_followup(self, username):
        c = self.conn.cursor()
        try:
            c.execute(
                "INSERT OR IGNORE INTO followup_queue (username, queued_at, status) VALUES (?, ?, 'Pending')",
                (username, datetime.now().isoformat())
            )
            # If ignore fired (already exists), let's ensure its status goes back to Pending if it was Sent previously?
            # Actually, doing REPLACE is better.
            if c.rowcount == 0:
                 c.execute("UPDATE followup_queue SET status = 'Pending', queued_at = ? WHERE username = ?", 
                           (datetime.now().isoformat(), username))
            self.conn.commit()
            return True
        except Exception:
            return False

    def remove_from_followup(self, username):
        c = self.conn.cursor()
        c.execute("DELETE FROM followup_queue WHERE username = ?", (username,))
        self.conn.commit()

    def get_pending_followups(self):
        c = self.conn.cursor()
        c.execute("SELECT username FROM followup_queue WHERE status = 'Pending'")
        return [row['username'] for row in c.fetchall()]

    def mark_followup_sent(self, username):
        c = self.conn.cursor()
        c.execute("UPDATE followup_queue SET status = 'Sent' WHERE username = ?", (username,))
        self.conn.commit()

