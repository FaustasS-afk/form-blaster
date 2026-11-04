"""
live_watch.py - real-time blast monitor for THIS project.

Read-only: tails the run log and polls the sqlite db, neither of which the
runner locks. Safe to leave open while a blast runs.

    python app/live_watch.py

Scoped to ONE batch file so the numbers mean something. Without a scoped batch
the counters would include the ~10k legacy rows already in the db.
"""
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../form-blaster/app
BASE = HERE.parent                              # .../form-blaster
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "formblaster.db"
LOG_CANDIDATES = ["_send_apify.log", "run_batch.log", "_send.log",
                  "leads_world/_send.log", "logs_run_new_forms.txt", "blast.log"]
BATCH_CANDIDATES = ["_send_apify.txt", "leads_world/leads_world_all_forms.txt",
                    "_run_new_forms.txt", "leads_new_clean_forms.txt"]

from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QTextEdit, QFrame, QProgressBar, QComboBox)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QTextCursor

REFRESH_MS = 500

COLOURS = {
    "submitted":    "#3FB950",
    "needs_manual": "#D29922",
    "blocked":      "#8B949E",
    "no_form":      "#6E7681",
    "failed":       "#F85149",
}
TAGMAP = {
    "OK": "submitted", "MAN": "needs_manual", "BLK": "blocked",
    "NONE": "no_form", "FAIL": "failed",
}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def pick(names):
    for n in names:
        p = BASE / n
        if p.exists():
            return p
    return None


def sub_forms(forms, total):
    """Percent of the batch where a form was reached (not the same as sent)."""
    return (100.0 * forms / total) if total else 0.0


class Watch(QWidget):
    def __init__(self, batch_file):
        super().__init__()
        self.batch_file = batch_file
        self.batch_urls = self._read_batch(batch_file)

        self.setWindowTitle("Form Blaster - LIVE")
        self.resize(1120, 760)
        self.setStyleSheet("background:#0D1117; color:#C9D1D9;")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(9)

        head = QHBoxLayout()
        t = QLabel("FORM BLAST - LIVE")
        t.setStyleSheet("font-size:18px;font-weight:800;letter-spacing:1px;color:#E6EDF3;")
        head.addWidget(t)
        head.addStretch()
        self.pulse = QLabel("\u25cf")
        self.pulse.setStyleSheet("color:#3FB950;font-size:18px;")
        head.addWidget(self.pulse)
        root.addLayout(head)

        self.scope = QLabel("")
        self.scope.setStyleSheet("color:#58A6FF;font-size:11px;")
        root.addWidget(self.scope)

        self.status = QLabel("connecting...")
        self.status.setStyleSheet("color:#8B949E;font-size:13px;font-weight:600;")
        root.addWidget(self.status)

        self.bar = QProgressBar()
        self.bar.setFixedHeight(26)
        self.bar.setStyleSheet(
            "QProgressBar{background:#161B22;border:1px solid #30363D;border-radius:4px;"
            "color:#E6EDF3;font-size:11px;font-weight:600;}"
            "QProgressBar::chunk{background-color:#238636;border-radius:3px;}")
        root.addWidget(self.bar)

        # signal cards
        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.cards = {}
        for key, label, colour in [
            ("submitted", "SENT (CONFIRMED)", "#3FB950"),
            ("needs_manual", "UNCONFIRMED", "#D29922"),
            ("blocked", "BLOCKED", "#8B949E"),
            ("no_form", "NO FORM", "#6E7681"),
            ("failed", "FAILED", "#F85149"),
            ("remaining", "REMAINING", "#D2A8FF"),
        ]:
            f = QFrame()
            f.setStyleSheet("background:#161B22;border:1px solid #30363D;border-radius:6px;")
            fl = QVBoxLayout(f)
            fl.setContentsMargins(11, 6, 11, 6)
            lab = QLabel(label)
            lab.setStyleSheet(f"color:{colour};font-size:9px;font-weight:700;letter-spacing:.5px;")
            val = QLabel("0")
            val.setStyleSheet(f"color:{colour};font-size:21px;font-weight:800;")
            fl.addWidget(lab)
            fl.addWidget(val)
            self.cards[key] = val
            cards.addWidget(f)
        root.addLayout(cards)

        # rate strip
        self.rates = QLabel("")
        self.rates.setStyleSheet(
            "color:#C9D1D9;font-size:13px;font-weight:700;"
            "background:#161B22;border:1px solid #30363D;border-radius:6px;padding:8px 12px;")
        root.addWidget(self.rates)

        # footnote strip: explains what the numbers do and do NOT mean
        self.foot = QLabel("")
        self.foot.setStyleSheet("color:#6E7681;font-size:10px;padding:0 2px;")
        self.foot.setWordWrap(True)
        root.addWidget(self.foot)

        row = QHBoxLayout()
        lbl = QLabel("LIVE ACTION FEED")
        lbl.setStyleSheet("color:#8B949E;font-size:10px;font-weight:700;letter-spacing:1px;")
        row.addWidget(lbl)
        row.addStretch()
        row.addWidget(QLabel("log:"))
        self.logpick = QComboBox()
        self.logpick.setStyleSheet("background:#161B22;color:#C9D1D9;border:1px solid #30363D;")
        for n in LOG_CANDIDATES:
            if (BASE / n).exists():
                self.logpick.addItem(n)
        self.logpick.currentTextChanged.connect(self._switch_log)
        row.addWidget(self.logpick)
        root.addLayout(row)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet(
            "background:#010409;color:#C9D1D9;border:1px solid #30363D;"
            "font-family:Consolas,monospace;font-size:11px;")
        root.addWidget(self.log, 1)

        self.logpath = pick(LOG_CANDIDATES)
        self._last_n = 0
        self._t0 = time.time()
        self._seen = 0
        self._rate = 0.0
        self._form_seen = 0
        self._form_rate = 0.0
        self._blink = True
        self._form_hist = []

        self.scope.setText(
            f"BATCH SCOPE: {self.batch_file.name}   ({len(self.batch_urls):,} urls)   "
            f"db: data/formblaster.db")

        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(REFRESH_MS)
        self.tick()

    # ── helpers ─────────────────────────────────────────────────────────────
    def _read_batch(self, p):
        if not p or not p.exists():
            return set()
        out = set()
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.add(line.split(",")[0].strip())
        return out

    def _switch_log(self, name):
        p = BASE / name
        if p.exists():
            self.logpath = p
            self._last_n = 0
            self.log.clear()

    def _batch_conn(self):
        """Read-only connection so we never contend with the runner."""
        return sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=5)

    # ── main loop ───────────────────────────────────────────────────────────
    def tick(self):
        counts, total = {}, 0
        try:
            conn = self._batch_conn()
            try:
                if self.batch_urls:
                    q = ",".join("?" * len(self.batch_urls))
                    args = list(self.batch_urls)
                    for st, n in conn.execute(
                            f"SELECT status, COUNT(*) FROM sites WHERE url IN ({q}) "
                            f"GROUP BY status", args):
                        counts[st] = n
                    total = sum(counts.values())
                else:
                    for st, n in conn.execute(
                            "SELECT status, COUNT(*) FROM sites GROUP BY status"):
                        counts[st] = n
                    total = sum(counts.values())
            finally:
                conn.close()
        except Exception:
            counts, total = {}, 0

        submitted = counts.get("submitted", 0)
        manual = counts.get("needs_manual", 0)
        forms = submitted + manual
        pending = counts.get("pending", 0)
        processed = total - pending

        for k in self.cards:
            self.cards[k].setText(str({
                "submitted": submitted,
                "needs_manual": manual,
                "blocked": counts.get("blocked", 0),
                "no_form": counts.get("no_form", 0),
                "failed": counts.get("failed", 0),
                "remaining": pending,
            }[k]))

        self.bar.setMaximum(max(total, 1))
        self.bar.setValue(processed)
        pct = (processed / total * 100) if total else 0
        self.bar.setFormat(f"{processed} / {total}   ({pct:.0f}%)      {pending} remaining")

        # rate + ETA
        now = time.time()
        dt = now - self._t0
        if dt >= 1.0:
            d_proc = processed - self._seen
            d_form = forms - self._form_seen
            if d_proc > 0:
                inst = d_proc / dt
                self._rate = inst if self._rate == 0 else self._rate * 0.7 + inst * 0.3
            if d_form > 0:
                instf = d_form / dt
                self._form_rate = instf if self._form_rate == 0 else self._form_rate * 0.7 + instf * 0.3
            self._seen = processed
            self._form_seen = forms
            self._t0 = now

        eta_s = (pending / self._rate) if self._rate > 0 else 0
        if eta_s <= 0:
            eta_txt = "--:--"
        elif eta_s < 3600:
            eta_txt = f"{int(eta_s//60):02d}:{int(eta_s%60):02d}"
        else:
            eta_txt = f"{eta_s/3600:.1f} h"

        min_left = eta_s / 60
        reached = sub_forms(forms, total)
        self.status.setText(
            f"CONFIRMED SENT {submitted}   ·   unconfirmed {manual}   ·   "
            f"forms reached {forms}")
        self.foot.setText(
            f"forms reached = confirmed + unconfirmed (the two are NOT additive truth). "
            f"Confirmed is the only number you can prove. "
            f"hit rate {reached:.1f}% of {total}")

        self.rates.setText(
            f"sites/min  {self._rate*60:6.1f}      "
            f"forms/min  {self._form_rate*60:5.1f}      "
            f"countdown  {eta_txt}   "
            f"({min_left:.0f} min left)")

        self._blink = not self._blink
        self.pulse.setStyleSheet(
            f"color:{'#3FB950' if self._blink else '#1F6F3D'};font-size:18px;")

        self._stream_log()

    def _stream_log(self):
        if not self.logpath or not self.logpath.exists():
            return
        try:
            with open(self.logpath, encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except Exception:
            return
        if len(lines) <= self._last_n:
            if len(lines) < self._last_n:
                self._last_n = 0
                self.log.clear()
            return
        fresh = lines[self._last_n:]
        self._last_n = len(lines)
        for ln in fresh:
            raw = ANSI.sub("", ln)
            if not raw.strip():
                continue
            esc = (raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            if raw.strip().startswith("---") or "BATCH" in raw:
                self.log.append(f'<span style="color:#58A6FF">{esc}</span>')
                continue
            col = None
            m = re.match(r"\s*([A-Z]{2,4})\s", raw)
            if m and m.group(1) in TAGMAP:
                col = COLOURS[TAGMAP[m.group(1)]]
            if col:
                self.log.append(f'<span style="color:{col}">{esc}</span>')
            elif "eta" in raw:
                self.log.append(f'<span style="color:#484F58">{esc}</span>')
            else:
                self.log.append(esc)
        self.log.moveCursor(QTextCursor.MoveOperation.End)


def main():
    log = pick(LOG_CANDIDATES)
    batch = pick(BATCH_CANDIDATES)
    if batch is None:
        print("WARNING: no batch file found - counters will be db-wide")

    app = QApplication(sys.argv)
    w = Watch(batch)
    w.show()

    # auto-tail: if the runner writes a fresh log, follow it
    def follow():
        nonlocal log
        for name in LOG_CANDIDATES:
            p = BASE / name
            if p.exists():
                if log is None or p.stat().st_mtime > log.stat().st_mtime:
                    log = p
                    if w.logpick.findText(name) < 0:
                        w.logpick.addItem(name)
                    w.logpath = p
                    break
    t = QTimer()
    t.timeout.connect(follow)
    t.start(2000)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
