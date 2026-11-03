"""
gui/blast.py — Form Blast panel.

Fast HTTP-only blaster for the scraped, platform-built contact forms. Runs a
thread pool of plain httpx workers (no browser), so thousands of sites can be
pushed through in minutes.

Engine entry points used (already ported, untouched here):
    outreach.db            -> init_db/load_config/pending/mark/stats/reset_failed
    outreach.spin          -> spin_fields (spintax + [Name] per target)
    outreach.submitter_http-> attempt/_client/unlocker_stats
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
                             QFrame, QTextEdit, QProgressBar, QSpinBox, QComboBox,
                             QCheckBox)

from outreach import db, spin, submitter_http
from gui.dialogs import dark_warning

# status -> (flag, colour)
STATUS_STYLE = {
    "submitted":   ("OK",   "#3FB950"),
    "needs_manual": ("MAN",  "#D29922"),
    "blocked":     ("BLK",  "#FF7B72"),
    "no_form":     ("NONE", "#8B949E"),
    "failed":      ("ERR",  "#F85149"),
}
COUNTERS = ("submitted", "needs_manual", "blocked", "no_form", "failed")


class BlastWorker(QThread):
    log_signal = pyqtSignal(str, str)       # level, message
    progress_signal = pyqtSignal(int, int)  # done, total
    counter_signal = pyqtSignal(dict)       # live status counts
    finished_signal = pyqtSignal()
    status_signal = pyqtSignal(str)

    def __init__(self, limit=0, concurrency=8, dry_run=False, assume_sent=False):
        super().__init__()
        self.limit = int(limit or 0)
        self.concurrency = max(1, int(concurrency or 1))
        self.dry_run = bool(dry_run)
        self.assume_sent = bool(assume_sent)
        self.is_running = True
        self.is_paused = False
        self._stop_clean = False   # True when Stop was pressed, not a natural end
        self._lock = threading.Lock()
        self._n = 0
        self._total = 0
        self.counts = {k: 0 for k in COUNTERS}

    # ── pause / stop ───────────────────────────────────────────────────────

    def pause(self):
        self.is_paused = True
        self.log_signal.emit("INFO", "Paused")

    def resume(self):
        self.is_paused = False
        self.log_signal.emit("INFO", "Resumed")

    def stop(self):
        """Ask the run to wind down. Safe to call from the GUI thread."""
        self.request_stop()

    def request_stop(self):
        """Stop immediately AND unpause, so a paused pool can drain."""
        self.is_running = False
        self.is_paused = False

    def is_draining(self):
        """True once a stop cancelled work that was still queued or in flight."""
        return self._stop_clean

    def _gate(self):
        while self.is_paused and self.is_running:
            time.sleep(0.2)

    # ── worker body ────────────────────────────────────────────────────────

    def run(self):
        started = time.time()
        try:
            db.init_db()
            cfg = db.load_config()
            todo = db.pending(limit=self.limit or None)
            total = len(todo)

            if not todo:
                self.log_signal.emit("WARN", "No pending rows — import leads first.")
                self.status_signal.emit("Nothing to do")
                return

            self._total = total
            self.log_signal.emit(
                "INFO", f"{total} site(s) queued · concurrency {self.concurrency}")
            if self.dry_run:
                self.log_signal.emit("WARN", "Dry run — results detected but NOT saved.")
            self.status_signal.emit("Running")
            self.counter_signal.emit(dict(self.counts))
            self.progress_signal.emit(0, total)

            # ONE client per worker thread. httpx.Client is not thread-safe, so
            # every pooled thread lazily builds its own and registers it here —
            # the local.client pattern from the Form Blaster runner.
            local = threading.local()
            clients = {}
            clients_lock = threading.Lock()

            def client_for():
                c = getattr(local, "client", None)
                if c is None:
                    c = submitter_http._client(cfg)
                    local.client = c
                    with clients_lock:
                        clients[threading.get_ident()] = c
                return c

            def do_one(site):
                if not self.is_running:
                    return dict(ok=False, status="failed", tier="http",
                                error="stopped", _cancelled=True)
                self._gate()
                if not self.is_running:
                    return dict(ok=False, status="failed", tier="http",
                                error="stopped", _cancelled=True)
                client = client_for()
                site_cfg = spin.spin_fields(cfg, values=site)
                return submitter_http.attempt(site["url"], site_cfg, client)

            pool = ThreadPoolExecutor(max_workers=self.concurrency)
            try:
                futures = {pool.submit(do_one, s): s for s in todo}
                for fut in as_completed(futures):
                    site = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        # A cancelled future never ran; don't re-queue it.
                        res = dict(ok=False, status="failed", tier="http",
                                   error=f"{type(e).__name__}: {e}"[:300],
                                   _cancelled=fut.cancelled())

                    status = res.get("status") or "failed"
                    if status not in STATUS_STYLE:
                        status = "failed"

                    # One place counts every finished (or cancelled) future, so
                    # the running total can never drift from the bar.
                    with self._lock:
                        self._n += 1
                        n = self._n
                        self.counts[status] += 1

                    # Persist unless dry run. A cancelled future wrote nothing.
                    if not self.dry_run and not res.get("_cancelled"):
                        try:
                            db.mark(site["id"], status, res.get("tier"),
                                    res.get("http_code"), res.get("detail", ""),
                                    res.get("error", ""), res.get("form_action", ""),
                                    res.get("fields_hit", ""))
                        except Exception as e:
                            self.log_signal.emit("ERR", f"db.mark failed: {e}")

                    flag, _c = STATUS_STYLE[status]
                    note = res.get("error") or res.get("detail") or ""
                    self.log_signal.emit(
                        status,
                        f"[{n:>3}] {flag:<4} {status:<12} {site['domain'] or '?':<28} "
                        f"{note[:90].replace(chr(10), ' ')}")

                    self.progress_signal.emit(n, total)
                    self.counter_signal.emit(dict(self.counts))

                    if not self.is_running and not self._stop_clean:
                        # Stop pressed: cancel everything still queued, then let
                        # only the in-flight futures finish. Track the survivors
                        # so the surviving clients can be closed afterwards.
                        self._stop_clean = True
                        inflight = 0
                        for f in list(futures):
                            if not f.cancel() and not f.done():
                                inflight += 1
                        self.log_signal.emit(
                            "WARN", f"Stop requested — draining {inflight} "
                                    f"in-flight request(s)…")
            finally:
                try:
                    pool.shutdown(wait=True)
                except Exception:
                    pass
                for c in list(clients.values()):
                    try:
                        c.close()
                    except Exception:
                        pass
                clients.clear()

            elapsed = time.time() - started
            rate = (self._n / elapsed * 60) if elapsed > 0 else 0.0
            c = self.counts
            self.log_signal.emit("INFO", "─" * 66)
            self.log_signal.emit("INFO", f"Run {'stopped' if self._stop_clean else 'complete'} — "
                                         f"{self._n} processed in {elapsed:.1f}s "
                                         f"({rate:.0f} sites/min)")
            self.log_signal.emit("INFO", f"submitted {c['submitted']} · manual {c['needs_manual']} · "
                                         f"blocked {c['blocked']} · no form {c['no_form']} · "
                                         f"failed {c['failed']}")
            if self.assume_sent:
                self.log_signal.emit("INFO", f"Assumed sent (incl. needs_manual): "
                                             f"{c['submitted'] + c['needs_manual']}")
            self.log_signal.emit("INFO", submitter_http.unlocker_stats())
            self.status_signal.emit("Stopped" if self._stop_clean else "Completed")
        except Exception as e:
            self.log_signal.emit("ERR", f"Worker crash: {type(e).__name__}: {e}")
            self.status_signal.emit(f"Failed: {e}")
        finally:
            self.is_running = False
            self.finished_signal.emit()


class BlastPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.worker = None
        self._total = 0
        self._n_seen = 0
        self._counts = {k: 0 for k in COUNTERS}
        self._last_state = "Idle"
        self._init_ui()
        self.update_status("Idle")

    # ── UI ─────────────────────────────────────────────────────────────────

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        head = QHBoxLayout()
        title = QLabel("Form Blast")
        title.setObjectName("headerTitle")
        head.addWidget(title)
        self.lbl_run_state = QLabel("Idle")
        self.lbl_run_state.setStyleSheet("color: #8B949E; font-size: 12px; font-weight: 700;")
        head.addSpacing(10)
        head.addWidget(self.lbl_run_state)
        head.addStretch()
        self.lbl_unlocker = QLabel(submitter_http.unlocker_stats())
        self.lbl_unlocker.setStyleSheet("color: #8B949E; font-size: 11px;")
        head.addWidget(self.lbl_unlocker)
        root.addLayout(head)

        sub = QLabel("Finds and submits contact forms over plain HTTP — fast, "
                     "headless, and independent of the DM engine.")
        sub.setStyleSheet("color: #8B949E; font-size: 12px;")
        root.addWidget(sub)

        # ── controls ────────────────────────────────────────────────────────
        card = QFrame()
        card.setObjectName("card")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(10)

        self.btn_start = QPushButton("▶  Start Blast")
        self.btn_start.setObjectName("btnStart")
        self.btn_start.setMinimumHeight(36)
        self.btn_start.setMinimumWidth(120)
        self.btn_start.clicked.connect(self.start_blast)

        self.btn_pause = QPushButton("⏸  Pause")
        self.btn_pause.setObjectName("btnWarning")
        self.btn_pause.setMinimumHeight(36)
        self.btn_pause.setMinimumWidth(95)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.pause_blast)

        self.btn_stop = QPushButton("⏹  Stop")
        self.btn_stop.setObjectName("btnDanger")
        self.btn_stop.setMinimumHeight(36)
        self.btn_stop.setMinimumWidth(95)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_blast)

        cl.addWidget(self.btn_start)
        cl.addWidget(self.btn_pause)
        cl.addWidget(self.btn_stop)
        cl.addSpacing(16)

        cl.addWidget(QLabel("Limit:"))
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(0, 1000000)
        self.spin_limit.setValue(0)
        self.spin_limit.setFixedWidth(84)
        self.spin_limit.setToolTip("0 = every pending row")
        cl.addWidget(self.spin_limit)

        cl.addWidget(QLabel("Threads:"))
        self.cmb_conc = QComboBox()
        self.cmb_conc.addItems(["1", "2", "4", "8", "10"])
        self.cmb_conc.setCurrentText("8")
        self.cmb_conc.setFixedWidth(62)
        cl.addWidget(self.cmb_conc)

        cl.addSpacing(16)

        self.chk_dry = QCheckBox("Dry run")
        self.chk_dry.setToolTip("Detect and report, but leave the database untouched.")
        self.chk_assume = QCheckBox("Assume sent")
        self.chk_assume.setToolTip(
            "Count 'needs_manual' (posted, no confirmation) as sent in the summary.")
        cl.addWidget(self.chk_dry)
        cl.addWidget(self.chk_assume)

        cl.setStretch(0, 0)
        cl.addStretch()
        root.addWidget(card)

        # ── progress ────────────────────────────────────────────────────────
        prog_card = QFrame()
        prog_card.setObjectName("card")
        pl = QVBoxLayout(prog_card)
        pl.setContentsMargins(16, 12, 16, 12)
        pl.setSpacing(8)
        row = QHBoxLayout()
        self.status_label = QLabel("0 / 0 — idle")
        self.status_label.setStyleSheet("color: #C9D1D9; font-size: 12px;")
        row.addWidget(self.status_label)
        row.addStretch()
        self.lbl_assumed = QLabel("")
        self.lbl_assumed.setStyleSheet("color: #3FB950; font-size: 12px; font-weight: 700;")
        row.addWidget(self.lbl_assumed)
        pl.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(20)
        self.progress.setStyleSheet(
            "QProgressBar {"
            "  background: #0D1117; border: 1px solid #30363D;"
            "  border-radius: 4px; color: #C9D1D9; font-size: 11px;"
            "}"
            "QProgressBar::chunk {"
            "  background-color: #238636; border-radius: 3px;"
            "}"
        )
        pl.addWidget(self.progress)
        root.addWidget(prog_card)

        # ── log ─────────────────────────────────────────────────────────────
        log_card = QFrame()
        log_card.setObjectName("card")
        ll = QVBoxLayout(log_card)
        ll.setContentsMargins(16, 12, 16, 12)
        log_title = QLabel("Live Blast Log")
        log_title.setStyleSheet("font-size: 13px; font-weight: 700; letter-spacing: 0.5px;")
        ll.addWidget(log_title)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        ll.addWidget(self.log)
        root.addWidget(log_card, 1)

    # ── actions ────────────────────────────────────────────────────────────

    def start_blast(self):
        if self.worker is not None and self.worker.isRunning():
            dark_warning(self, "Already running", "A blast is already in progress.")
            return
        try:
            db.init_db()
            db.load_config()
        except Exception as e:
            dark_warning(self, "Config missing",
                         f"Could not load outreach/blaster_config.json:\n{e}")
            return
        if not db.pending(limit=1):
            dark_warning(self, "Nothing pending",
                         "No rows are marked 'pending'. Import leads or reset failed rows first.")
            return

        self.log.clear()
        self.progress.setValue(0)
        self._total = 0
        self._n_seen = 0
        self._counts = {k: 0 for k in COUNTERS}
        self._refresh_state_line()
        self.worker = BlastWorker(
            limit=self.spin_limit.value(),
            concurrency=int(self.cmb_conc.currentText()),
            dry_run=self.chk_dry.isChecked(),
            assume_sent=self.chk_assume.isChecked(),
        )
        self.worker.log_signal.connect(self._on_log)
        self.worker.progress_signal.connect(self._on_progress)
        self.worker.counter_signal.connect(self._on_counts)
        self.worker.status_signal.connect(self.update_status)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_pause.setText("⏸  Pause")
        self.update_status("Running")

    def pause_blast(self):
        if self.worker is None or not self.worker.isRunning():
            return
        if self.worker.is_paused:
            self.worker.resume()
            self.btn_pause.setText("⏸  Pause")
        else:
            self.worker.pause()
            self.btn_pause.setText("▶  Resume")

    def stop_blast(self):
        if self.worker is None:
            return
        self.worker.request_stop()
        self.update_status("Stopping…")
        # Wait, but do not block the GUI for the full in-flight request: if the
        # worker is still draining, finished_signal will settle the buttons.
        self.worker.wait(3000)

    # ── signals ────────────────────────────────────────────────────────────

    def _on_log(self, level, message):
        color = STATUS_STYLE.get(level, (None, None))[1] or {
            "INFO": "#58A6FF", "WARN": "#D29922", "ERR": "#F85149",
        }.get(level, "#C9D1D9")
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.insertHtml(
            f'<span style="color:{color}">[{stamp}] {message}</span><br>')
        cur = self.log.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.log.setTextCursor(cur)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _on_progress(self, done, total):
        self._total = max(int(total), 0)
        self._n_seen = int(done)
        self.progress.setMaximum(max(self._total, 1))
        self.progress.setValue(done)
        self._refresh_state_line()

    def _on_counts(self, counts):
        self._counts = counts
        self._refresh_state_line()

    def _refresh_state_line(self):
        """Render 'done / total — breakdown' under the progress bar."""
        c = self._counts
        breakdown = (f"{c.get('submitted', 0)} sent, {c.get('needs_manual', 0)} manual, "
                     f"{c.get('blocked', 0)} blocked, {c.get('no_form', 0)} no form, "
                     f"{c.get('failed', 0)} failed")
        self.status_label.setText(f"{self._n_seen} / {self._total} — {breakdown}")
        if self.chk_assume.isChecked():
            self.lbl_assumed.setText(
                f"·  assumed sent {c.get('submitted', 0) + c.get('needs_manual', 0)}")
        else:
            self.lbl_assumed.setText("")

    def _on_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_pause.setText("⏸  Pause")
        self.lbl_unlocker.setText(submitter_http.unlocker_stats())
        self._refresh_state_line()

    def update_status(self, text):
        """Single writer for the run-state pill, so it can never stick."""
        if text.startswith("Stopping"):
            self._pill("⏹  Stopping…", "#D29922")
        elif text == "Running":
            self._pill("▶  Running", "#3FB950")
        elif text.startswith("Completed"):
            self._pill("✔  Completed", "#3FB950")
        elif text.startswith("Stopped"):
            self._pill("⏹  Stopped", "#8B949E")
        else:
            self._pill(text, "#8B949E")
        self._last_state = text

    def _pill(self, text, color):
        self.lbl_run_state.setText(text)
        self.lbl_run_state.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 700;")

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()
            self.worker.wait(3000)
        super().closeEvent(event)
