"""
gui/leads.py — Leads panel.

The target list the form outreach engine works through. Import URLs, watch how
far the queue has got, and clean up anything that cannot be reached.
"""

from pathlib import Path

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QTableWidget, QTableWidgetItem, QHeaderView, QLabel,
                             QLineEdit, QComboBox, QFrame, QFileDialog, QDialog,
                             QPlainTextEdit)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush

from outreach import db
from gui.dialogs import dark_info, dark_warning, dark_question, _STYLE

REFRESH = "⟳"

# Rows pulled into the table at once. The card totals stay exact; a hit-list this
# long is well past what anyone scrolls, and 5k of QTableWidgetItems stays snappy.
ROW_CAP = 5000

BASE = Path(__file__).resolve().parent.parent
SAMPLE = BASE / "outreach" / "leads_independent.txt"

# combo label -> statuses it maps to. The engine uses exactly these statuses.
FILTERS = [
    ("All",           ()),
    ("Pending",       ("pending",)),
    ("Sent",          ("submitted",)),
    ("Failed",        ("failed",)),
    ("Needs manual",  ("needs_manual",)),
    ("Blocked",       ("blocked",)),
    ("No form",       ("no_form",)),
]

# status -> colour, mirrors tracker.py's status colouring
STATUS_COLOURS = {
    "submitted":    "#3FB950",
    "needs_manual": "#D29922",
    "failed":       "#F85149",
    "blocked":      "#8B949E",
    "no_form":      "#6E7681",
    "pending":      "#58A6FF",
}


class _PasteDialog(QDialog):
    """Multi-line paste box — dark_input() is single-line only."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setStyleSheet(_STYLE)
        self.setFixedWidth(520)
        self.setWindowTitle("Paste URLs")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        title = QLabel("Paste URLs")
        title.setObjectName("dlg_title")
        root.addWidget(title)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background: #30363D; border: none; max-height: 1px;")
        root.addWidget(line)

        hint = QLabel("One per line — or just dump them in. Duplicates are skipped.")
        hint.setStyleSheet("color: #8B949E; font-size: 12px; background: transparent;")
        root.addWidget(hint)

        self.box = QPlainTextEdit()
        self.box.setPlaceholderText("example.com\nexample.org")
        self.box.setMinimumHeight(190)
        self.box.setStyleSheet(
            "QPlainTextEdit { background-color: #0D1117; border: 1px solid #30363D;"
            " border-radius: 6px; padding: 8px; color: #C9D1D9; font-size: 13px; }"
            "QPlainTextEdit:focus { border: 1px solid #58A6FF; }")
        root.addWidget(self.box)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Add URLs")
        ok.setObjectName("btn_primary")
        ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        root.addLayout(btns)

    def urls(self):
        return self.box.toPlainText().split()


class LeadsPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        try:
            db.init_db()
        except Exception:
            pass
        self._rows = []
        self._init_ui()
        self.refresh()

    # ── ui ──────────────────────────────────────────────────────────────────

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header = QLabel("Leads")
        header.setObjectName("headerTitle")
        layout.addWidget(header)

        sub = QLabel(
            "The target list for form outreach. Import URLs, then the engine works "
            "through them from the top and records what happened."
        )
        sub.setStyleSheet("color: #8B949E; font-size: 12px;")
        layout.addWidget(sub)

        # ── stat cards ──────────────────────────────────────────────────────
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.card_labels = {}
        for key, title, colour in [
            ("tracked", "TRACKED", "#8B949E"),
            ("pending", "PENDING", "#58A6FF"),
            ("sent",    "SENT",    "#3FB950"),
            ("failed",  "FAILED",  "#F85149"),
            ("blocked", "BLOCKED", "#8B949E"),
            ("no_form", "NO FORM", "#D29922"),
        ]:
            card = QFrame()
            card.setObjectName("card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 10, 14, 10)
            t = QLabel(title)
            t.setStyleSheet(f"color: {colour}; font-size: 10px; font-weight: 700; letter-spacing: 0.5px;")
            v = QLabel("0")
            v.setStyleSheet(f"color: {colour}; font-size: 22px; font-weight: 700;")
            cl.addWidget(t)
            cl.addWidget(v)
            self.card_labels[key] = v
            cards.addWidget(card)
        layout.addLayout(cards)

        # ── import row ──────────────────────────────────────────────────────
        imports = QHBoxLayout()
        btn_import = QPushButton("Import TXT/CSV")
        btn_import.setObjectName("btnSuccess")
        btn_import.clicked.connect(self.import_file)

        btn_paste = QPushButton("Paste URLs")
        btn_paste.clicked.connect(self.paste_urls)

        btn_sample = QPushButton("Load sample leads")
        btn_sample.setToolTip(f"Loads {SAMPLE.name} (~9,750 URLs). Can take a moment.")
        btn_sample.clicked.connect(self.load_sample)

        imports.addWidget(btn_import)
        imports.addWidget(btn_paste)
        imports.addWidget(btn_sample)
        imports.addStretch()
        layout.addLayout(imports)

        # ── tools row ───────────────────────────────────────────────────────
        tools = QHBoxLayout()

        self.cmb_filter = QComboBox()
        self.cmb_filter.addItems([f[0] for f in FILTERS])
        self.cmb_filter.currentIndexChanged.connect(self.refresh)

        btn_refresh = QPushButton(f"{REFRESH} Refresh")
        btn_refresh.clicked.connect(self.refresh)

        btn_requeue = QPushButton("Requeue Selected")
        btn_requeue.setObjectName("btnWarning")
        btn_requeue.clicked.connect(self.requeue_selected)

        btn_delete = QPushButton("Delete Selected")
        btn_delete.setObjectName("btnDanger")
        btn_delete.clicked.connect(self.delete_selected)

        btn_export = QPushButton("Export CSV")
        btn_export.clicked.connect(self.export_csv)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search url or domain...")
        self.txt_search.setFixedWidth(200)
        self.txt_search.textChanged.connect(self.apply_filter)

        tools.addWidget(self.cmb_filter)
        tools.addWidget(btn_refresh)
        tools.addWidget(btn_requeue)
        tools.addWidget(btn_delete)
        tools.addWidget(btn_export)
        tools.addStretch()
        tools.addWidget(self.txt_search)
        layout.addLayout(tools)

        # ── table ───────────────────────────────────────────────────────────
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["ID", "URL", "Domain", "Status", "HTTP", "Tried", "Last try"]
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        layout.addWidget(self.table)

        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet("color: #8B949E; font-size: 12px;")
        layout.addWidget(self.lbl_count)

        self.setLayout(layout)

    # ── data ────────────────────────────────────────────────────────────────

    def _fetch(self):
        """Return the rows matching the current filter (parameterised)."""
        statuses = FILTERS[self.cmb_filter.currentIndex()][1]
        cols = "id, url, domain, status, http_code, tried, last_try"
        try:
            c = db.get_conn()
            if statuses:
                marks = ",".join("?" * len(statuses))
                sql = (f"SELECT {cols} FROM sites WHERE status IN ({marks}) "
                       f"ORDER BY id ASC LIMIT {ROW_CAP}")
                cur = c.execute(sql, statuses)
            else:
                cur = c.execute(f"SELECT {cols} FROM sites ORDER BY id ASC LIMIT {ROW_CAP}")
            return [dict(r) for r in cur]
        except Exception:
            return []

    def refresh(self):
        try:
            s = db.stats()
            by = s.get("by_status", {})
        except Exception:
            s, by = {"total": 0}, {}

        counts = {
            "tracked": s.get("total", 0),
            "pending": by.get("pending", 0),
            "sent":    by.get("submitted", 0),
            "failed":  by.get("failed", 0),
            "blocked": by.get("blocked", 0),
            "no_form": by.get("no_form", 0),
        }
        for key, lbl in self.card_labels.items():
            lbl.setText(str(counts.get(key, 0)))

        self._rows = self._fetch()
        self.table.setRowCount(len(self._rows))

        for r, row in enumerate(self._rows):
            status = row.get("status") or "pending"
            cells = [str(row.get("id", "")), row.get("url") or "", row.get("domain") or "",
                     status, str(row.get("http_code") or ""), str(row.get("tried") or 0),
                     str(row.get("last_try") or "")[:19].replace("T", " ")]
            colour = STATUS_COLOURS.get(status, "#58A6FF")
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c in (0, 4, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 3:
                    item.setForeground(QBrush(QColor(colour)))
                if c == 1:
                    item.setToolTip(row.get("detail") or row.get("error") or text)
                self.table.setItem(r, c, item)

        self.apply_filter(self.txt_search.text())

    def apply_filter(self, text):
        term = (text or "").lower()
        shown = 0
        for row in range(self.table.rowCount()):
            hit = not term
            if term:
                url = self.table.item(row, 1)
                dom = self.table.item(row, 2)
                hit = term in ((url.text() if url else "") + " "
                               + (dom.text() if dom else "")).lower()
            self.table.setRowHidden(row, not hit)
            shown += 1 if hit else 0
        total = self.table.rowCount()
        parts = [f"{shown}"]
        if shown != total:
            parts.append(f"of {total}")
        parts.append("lead(s) shown")
        if total >= ROW_CAP:
            parts.append(f"— capped at {ROW_CAP:,}, narrow the filter to see the rest")
        self.lbl_count.setText(" ".join(parts))

    def _ids_for_rows(self, rows):
        out = []
        for row in rows:
            item = self.table.item(row, 0)
            if item:
                try:
                    out.append(int(item.text()))
                except ValueError:
                    pass
        return out

    def _visible_ids(self):
        """IDs of the rows currently visible after search filtering."""
        rows = [r for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]
        return self._ids_for_rows(rows)

    def _selected_ids(self):
        return self._ids_for_rows(sorted({i.row() for i in self.table.selectedIndexes()}))

    # ── imports ─────────────────────────────────────────────────────────────
    # One URL per line; blanks and # comments skipped; anything after a comma
    # dropped (the leads CSVs carry url as the first column).

    def _parse(self, raw):
        urls = []
        for line in raw:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                line = line.split(",", 1)[0].strip()
            if line:
                urls.append(line)
        return urls

    def _read(self, path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()

    def _ingest(self, urls):
        try:
            added, skipped = db.add_urls(urls)
        except Exception as e:
            dark_warning(self, "Import failed", str(e))
            return None
        self.refresh()
        dark_info(self, "Import complete",
                  f"Read {len(urls)} line(s).\n\n  Added:   {added}\n  Skipped: {skipped}")
        return added

    def _load_text(self, path, empty_msg, missing_msg=None):
        """Read + parse a leads file, warning and returning [] on any problem."""
        if missing_msg and not path.exists():
            dark_warning(self, "Not found", missing_msg)
            return []
        try:
            urls = self._parse(self._read(path))
        except Exception as e:
            dark_warning(self, "Import failed", str(e))
            return []
        if not urls:
            dark_warning(self, "Nothing to import", empty_msg)
        return urls

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import leads", "", "Text or CSV (*.txt *.csv);;All Files (*)")
        if not path:
            return
        urls = self._load_text(path, "No usable URLs were found in that file.")
        if urls:
            self._ingest(urls)

    def paste_urls(self):
        dlg = _PasteDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        urls = dlg.urls()
        if not urls:
            dark_warning(self, "Nothing pasted", "No URLs were entered.")
            return
        self._ingest(urls)

    def load_sample(self):
        # ~9,754 real leads — the progress dialog-free load can take a second.
        urls = self._load_text(
            SAMPLE, "The sample file has no usable URLs.",
            missing_msg=f"Could not find:\n{SAMPLE}")
        if urls:
            self._ingest(urls)

    # ── row actions ─────────────────────────────────────────────────────────

    def _run_on_selection(self, title, message, sql, done):
        ids = self._selected_ids()
        if not ids:
            dark_warning(self, "No selection", "Select one or more rows first.")
            return
        if not dark_question(self, title, message.format(n=len(ids))):
            return
        try:
            c = db.get_conn()
            c.executemany(sql, [(i,) for i in ids])
            c.commit()
        except Exception as e:
            dark_warning(self, "Action failed", str(e))
            return
        self.refresh()
        dark_info(self, title, done.format(n=len(ids)))

    def delete_selected(self):
        self._run_on_selection(
            "Delete leads",
            "Permanently remove {n} lead(s) from the database?\n"
            "They will need re-importing to come back.",
            "DELETE FROM sites WHERE id = ?",
            "Removed {n} lead(s).")

    def requeue_selected(self):
        self._run_on_selection(
            "Requeue leads",
            "Set {n} lead(s) back to 'pending'?\n"
            "The engine will try them again. Try counters are kept.",
            "UPDATE sites SET status = 'pending' WHERE id = ?",
            "{n} lead(s) are pending again.")

    # ── export ──────────────────────────────────────────────────────────────

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export leads CSV", "leads.csv",
                                              "CSV Files (*.csv)")
        if not path:
            return
        ids = self._visible_ids()
        if not ids:
            dark_warning(self, "Nothing to export",
                         "The current filtered view has no rows.")
            return
        import csv
        try:
            c = db.get_conn()
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["id", "url", "domain", "status", "http_code", "tier",
                            "detail", "error", "form_action", "fields_hit",
                            "tried", "last_try"])
                # chunk the IN () clause so huge views stay under SQLite's limits
                for i in range(0, len(ids), 900):
                    chunk = ids[i:i + 900]
                    marks = ",".join("?" * len(chunk))
                    cur = c.execute(
                        f"SELECT id, url, domain, status, http_code, tier, detail, error, "
                        f"form_action, fields_hit, tried, last_try FROM sites "
                        f"WHERE id IN ({marks}) ORDER BY id ASC", chunk)
                    for r in cur:
                        w.writerow(list(r))
            dark_info(self, "Exported", f"Wrote {len(ids)} row(s) to:\n{path}")
        except Exception as e:
            dark_warning(self, "Export failed", str(e))
