"""
gui/form_tracker.py — Form Blast results tracker.

Every lead and what happened when we tried to submit. Filter by outcome,
see the evidence (which form, which fields, what the site returned), and
export. This is the record of what has actually been sent.
"""

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QTableWidget, QTableWidgetItem, QHeaderView, QLabel,
                             QLineEdit, QComboBox, QFrame, QFileDialog,
                             QTextEdit, QSplitter)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush

from outreach import db
from gui.dialogs import dark_info, dark_question


TICK = "OK"
CROSS = ""

# status -> (short label, colour)
STATUS_META = {
    "submitted":    ("SENT",   "#3FB950"),
    "needs_manual": ("MAYBE",  "#D29922"),
    "blocked":      ("BLOCK",  "#8B949E"),
    "no_form":      ("NONE",   "#6E7681"),
    "failed":       ("FAIL",   "#F85149"),
    "pending":      ("-",      "#58A6FF"),
}

FILTERS = [
    ("All leads",        None),
    ("Sent (confirmed)", ["submitted"]),
    ("Maybe sent",       ["needs_manual"]),
    ("Blocked",          ["blocked"]),
    ("No form found",    ["no_form"]),
    ("Failed",           ["failed"]),
    ("Pending",          ["pending"]),
]


class FormTrackerPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        db.init_db()
        self._init_ui()
        self.refresh()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header = QLabel("Form Blast Tracker")
        header.setObjectName("headerTitle")
        layout.addWidget(header)

        sub = QLabel(
            "Every lead and what happened when we submitted. "
            "SENT is confirmed. MAYBE means the form posted but the site "
            "gave no confirmation — most of those did arrive."
        )
        sub.setStyleSheet("color: #8B949E; font-size: 12px;")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        # ── stat cards ──────────────────────────────────────────────────────
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.card_labels = {}
        for key, title, colour in [
            ("total",       "TOTAL LEADS", "#8B949E"),
            ("submitted",   "SENT",        "#3FB950"),
            ("needs_manual", "MAYBE SENT", "#D29922"),
            ("no_form",     "NO FORM",     "#6E7681"),
            ("blocked",     "BLOCKED",     "#8B949E"),
            ("failed",      "FAILED",      "#F85149"),
        ]:
            card = QFrame()
            card.setObjectName("card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 10, 14, 10)
            t = QLabel(title)
            t.setStyleSheet(
                f"color: {colour}; font-size: 10px; font-weight: 700; "
                f"letter-spacing: 0.5px;")
            v = QLabel("0")
            v.setStyleSheet(f"color: {colour}; font-size: 22px; font-weight: 700;")
            cl.addWidget(t)
            cl.addWidget(v)
            self.card_labels[key] = v
            cards.addWidget(card)
        layout.addLayout(cards)

        # ── controls ────────────────────────────────────────────────────────
        tools = QHBoxLayout()

        self.cmb_filter = QComboBox()
        for label, _ in FILTERS:
            self.cmb_filter.addItem(label)
        self.cmb_filter.currentIndexChanged.connect(self.refresh)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search url or domain...")
        self.txt_search.setFixedWidth(220)
        self.txt_search.textChanged.connect(self.apply_filter)

        btn_refresh = QPushButton("⟳ Refresh")
        btn_refresh.clicked.connect(self.refresh)

        btn_export = QPushButton("Export CSV")
        btn_export.clicked.connect(self.export_csv)

        tools.addWidget(self.cmb_filter)
        tools.addWidget(btn_refresh)
        tools.addWidget(btn_export)
        tools.addStretch()
        tools.addWidget(self.txt_search)
        layout.addLayout(tools)

        # ── splitter: table on top, evidence below ──────────────────────────
        split = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["URL", "Status", "HTTP", "Tried", "Last try", "What happened"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.show_evidence)
        split.addWidget(self.table)

        self.evidence = QTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setPlaceholderText(
            "Select a row to see which form it used and what the site returned.")
        self.evidence.setStyleSheet(
            "background:#0D1117; color:#C9D1D9; font-family:Consolas,monospace; "
            "font-size:11px; border:1px solid #30363D;")
        split.addWidget(self.evidence)
        split.setSizes([420, 160])
        layout.addWidget(split)

        # ── row actions ─────────────────────────────────────────────────────
        actions = QHBoxLayout()
        btn_requeue = QPushButton("Requeue Selected")
        btn_requeue.setObjectName("btnWarning")
        btn_requeue.clicked.connect(self.requeue_selected)

        btn_requeue_failed = QPushButton("Requeue ALL failed / blocked / maybe")
        btn_requeue_failed.clicked.connect(self.requeue_all_retryable)

        btn_delete = QPushButton("Delete Selected")
        btn_delete.setObjectName("btnDanger")
        btn_delete.clicked.connect(self.delete_selected)

        actions.addWidget(btn_requeue)
        actions.addWidget(btn_requeue_failed)
        actions.addStretch()
        actions.addWidget(btn_delete)
        layout.addLayout(actions)

        self.setLayout(layout)
        self._rows = []

    # ── data ────────────────────────────────────────────────────────────────

    def refresh(self):
        try:
            s = db.stats()
        except Exception:
            s = {"total": 0, "by_status": {}}
        bs = s.get("by_status", {})
        for k, lbl in self.card_labels.items():
            lbl.setText(str(bs.get(k, 0) if k != "total" else s.get("total", 0)))

        idx = self.cmb_filter.currentIndex()
        wanted = FILTERS[idx][1]

        c = db.get_conn().cursor()
        if wanted:
            qmarks = ",".join("?" * len(wanted))
            rows = c.execute(
                f"SELECT * FROM sites WHERE status IN ({qmarks}) "
                f"ORDER BY COALESCE(last_try, first_seen) DESC LIMIT 5000",
                wanted).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM sites "
                "ORDER BY COALESCE(last_try, first_seen) DESC LIMIT 5000").fetchall()

        self._rows = [dict(r) for r in rows]
        self.table.setRowCount(len(self._rows))

        for i, r in enumerate(self._rows):
            st = r.get("status", "pending")
            label, colour = STATUS_META.get(st, (st, "#C9D1D9"))
            detail = (r.get("detail") or r.get("error") or "")[:70]

            cells = [
                r.get("url", ""),
                label,
                str(r.get("http_code") or ""),
                str(r.get("tried") or 0),
                str(r.get("last_try") or "")[:19].replace("T", " "),
                detail,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setForeground(QBrush(QColor(colour)))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col in (2, 3, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(i, col, item)

        self.apply_filter(self.txt_search.text())

    def apply_filter(self, text):
        term = (text or "").lower()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                self.table.setRowHidden(row, term not in item.text().lower())

    def show_evidence(self):
        sel = self.table.selectedItems()
        if not sel:
            return
        row = sel[0].row()
        if row >= len(self._rows):
            return
        r = self._rows[row]
        lines = [
            f"URL          : {r.get('url','')}",
            f"Status       : {r.get('status','')}",
            f"HTTP code    : {r.get('http_code') or '-'}",
            f"Tried        : {r.get('tried') or 0} time(s)",
            f"First seen   : {r.get('first_seen') or '-'}",
            f"Last try     : {r.get('last_try') or '-'}",
            "",
            f"Form posted  : {r.get('form_action') or '(none)'}",
            f"Fields used  : {r.get('fields_hit') or '(none)'}",
            "",
            f"Detail       : {r.get('detail') or '(none)'}",
            f"Error        : {r.get('error') or '(none)'}",
        ]
        self.evidence.setPlainText("\n".join(lines))

    # ── actions ─────────────────────────────────────────────────────────────

    def _selected_ids(self):
        ids = []
        for item in self.table.selectedItems():
            row = item.row()
            if row < len(self._rows):
                ids.append(self._rows[row]["id"])
        return sorted(set(ids))

    def requeue_selected(self):
        ids = self._selected_ids()
        if not ids:
            dark_info(self, "Nothing selected", "Select one or more rows first.")
            return
        c = db.get_conn().cursor()
        c.executemany("UPDATE sites SET status='pending' WHERE id=?",
                      [(i,) for i in ids])
        db.get_conn().commit()
        self.refresh()
        dark_info(self, "Requeued", f"{len(ids)} lead(s) set back to pending.")

    def requeue_all_retryable(self):
        c = db.get_conn().cursor()
        c.execute("UPDATE sites SET status='pending' "
                  "WHERE status IN ('failed','blocked','needs_manual')")
        n = c.rowcount
        db.get_conn().commit()
        self.refresh()
        dark_info(self, "Requeued", f"{n} lead(s) set back to pending. "
                                    f"Re-run the blast to retry them.")

    def delete_selected(self):
        ids = self._selected_ids()
        if not ids:
            dark_info(self, "Nothing selected", "Select one or more rows first.")
            return
        if not dark_question(self, "Delete",
                             f"Remove {len(ids)} lead(s) from the tracker?"):
            return
        c = db.get_conn().cursor()
        c.executemany("DELETE FROM sites WHERE id=?", [(i,) for i in ids])
        db.get_conn().commit()
        self.refresh()

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export results CSV", "form_blast_results.csv",
            "CSV Files (*.csv)")
        if not path:
            return
        try:
            n = db.export_csv(path)
            dark_info(self, "Exported", f"Wrote {n} row(s) to:\n{path}")
        except Exception as e:
            dark_info(self, "Export failed", str(e))
