"""
live_watch.py — real-time blast monitor. Always-updating, read-only.

Shows: progress bar, live counters, rate/ETA, and a colour-coded per-action
feed of every site as it is hit. Refreshes 4x/second off the DB + log.
"""
import sys, os, time
sys.path.insert(0, r"C:\Users\SellJ\Desktop\INSTAGRAM OUTREACH")
os.chdir(r"C:\Users\SellJ\Desktop\INSTAGRAM OUTREACH")

from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QTextEdit, QFrame, QProgressBar)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QTextCursor

from outreach import db

LOG = "blast_full.log"
REFRESH_MS = 250

COLOURS = {
    "submitted":    ("#3FB950", "SENT "),
    "needs_manual": ("#D29922", "MAYBE"),
    "blocked":      ("#8B949E", "BLOCK"),
    "no_form":      ("#6E7681", "none "),
    "failed":       ("#F85149", "FAIL "),
}


class Watch(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Form Blaster — LIVE")
        self.resize(1040, 700)
        self.setStyleSheet("background:#0D1117; color:#C9D1D9;")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(9)

        head = QHBoxLayout()
        title = QLabel("FORM BLAST — LIVE")
        title.setStyleSheet("font-size:18px; font-weight:800; letter-spacing:1px; color:#E6EDF3;")
        head.addWidget(title)
        head.addStretch()
        self.pulse = QLabel("●")
        self.pulse.setStyleSheet("color:#3FB950; font-size:18px;")
        head.addWidget(self.pulse)
        root.addLayout(head)

        self.status = QLabel("connecting...")
        self.status.setStyleSheet("color:#8B949E; font-size:12px;")
        root.addWidget(self.status)

        self.bar = QProgressBar()
        self.bar.setFixedHeight(24)
        self.bar.setStyleSheet(
            "QProgressBar{background:#161B22;border:1px solid #30363D;"
            "border-radius:4px;color:#E6EDF3;font-size:11px;font-weight:600;}"
            "QProgressBar::chunk{background-color:#238636;border-radius:3px;}")
        root.addWidget(self.bar)

        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.cards = {}
        for key, label, colour in [
            ("submitted", "SENT", "#3FB950"),
            ("needs_manual", "MAYBE SENT", "#D29922"),
            ("blocked", "BLOCKED", "#8B949E"),
            ("no_form", "NO FORM", "#6E7681"),
            ("failed", "FAILED", "#F85149"),
        ]:
            f = QFrame()
            f.setStyleSheet("background:#161B22;border:1px solid #30363D;border-radius:6px;")
            fl = QVBoxLayout(f)
            fl.setContentsMargins(12, 7, 12, 7)
            t = QLabel(label)
            t.setStyleSheet(f"color:{colour};font-size:9px;font-weight:700;letter-spacing:.5px;")
            v = QLabel("0")
            v.setStyleSheet(f"color:{colour};font-size:21px;font-weight:800;")
            fl.addWidget(t)
            fl.addWidget(v)
            self.cards[key] = v
            cards.addWidget(f)
        root.addLayout(cards)

        lbl = QLabel("LIVE ACTION FEED")
        lbl.setStyleSheet("color:#8B949E;font-size:10px;font-weight:700;letter-spacing:1px;")
        root.addWidget(lbl)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet(
            "background:#010409;color:#C9D1D9;border:1px solid #30363D;"
            "font-family:Consolas,monospace;font-size:11px;")
        root.addWidget(self.log, 1)

        self._last_n = 0
        self._t0 = time.time()
        self._last_n_seen = 0
        self._rate = 0.0
        self._blink = True

        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(REFRESH_MS)
        self.tick()

    def tick(self):
        try:
            c = db.get_conn().cursor()
            bs = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) n FROM sites GROUP BY status")}
            total = c.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        except Exception:
            bs, total = {}, 0

        for k, lbl in self.cards.items():
            lbl.setText(str(bs.get(k, 0)))

        pending = bs.get("pending", 0)
        processed = total - pending
        self.bar.setMaximum(max(total, 1))
        self.bar.setValue(processed)
        self.bar.setFormat(f"{processed} / {total}      {pending} remaining")

        # rate + eta from observed progress, smoothed
        now = time.time()
        if now - self._t0 >= 2:
            delta = processed - self._last_n_seen
            if delta > 0:
                inst = delta / (now - self._t0) * 60
                self._rate = inst if self._rate == 0 else (self._rate * 0.7 + inst * 0.3)
            self._last_n_seen = processed
            self._t0 = now

        sub = bs.get("submitted", 0)
        man = bs.get("needs_manual", 0)
        eta = (pending / self._rate) if self._rate > 0 else 0
        eta_s = f"{eta:.0f} min" if eta < 60 else f"{eta/60:.1f} h"
        self.status.setText(
            f"probable sent {sub + man}  ({sub} confirmed · {man} maybe)   ·   "
            f"{self._rate:.0f} sites/min   ·   ETA {eta_s}")

        self._blink = not self._blink
        self.pulse.setStyleSheet(
            f"color:{'#3FB950' if self._blink else '#1F6F3D'}; font-size:18px;")

        # stream new log lines
        try:
            with open(LOG, encoding="utf-8", errors="ignore") as f:
                f.seek(0)
                data = f.read()
            lines = data.splitlines()
            if len(lines) > self._last_n:
                fresh = lines[self._last_n:]
                self._last_n = len(lines)
                for ln in fresh:
                    if not ln.strip():
                        continue
                    esc = (ln.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
                    if ln.startswith("    ---"):
                        self.log.append(f'<span style="color:#58A6FF">{esc}</span>')
                        continue
                    col = None
                    for st, (cc, _flag) in COLOURS.items():
                        if f" {st} " in ln or f"  {st}" in ln:
                            col = cc
                            break
                    if col:
                        self.log.append(f'<span style="color:{col}">{esc}</span>')
                    else:
                        self.log.append(esc)
                self.log.moveCursor(QTextCursor.MoveOperation.End)
        except Exception:
            pass


app = QApplication(sys.argv)
w = Watch()
w.show()
app.exec()
