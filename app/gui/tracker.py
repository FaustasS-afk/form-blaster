"""
gui/tracker.py — Outreach Tracker panel.

Shows every target and which actions have been performed, so you can engage
on one day and DM on another without double-following or forgetting anyone.
"""

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QTableWidget, QTableWidgetItem, QHeaderView, QLabel,
                             QLineEdit, QComboBox, QFrame, QFileDialog)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush

from core.tracker import Tracker
from gui.dialogs import dark_info, dark_question


TICK = "✓"
CROSS = ""


class TrackerPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.tracker = Tracker()
        self._init_ui()
        self.refresh()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header = QLabel("Outreach Tracker")
        header.setObjectName("headerTitle")
        layout.addWidget(header)

        sub = QLabel(
            "Every target and what has been done to them. Use this to engage one day "
            "and DM another — nothing gets double-followed."
        )
        sub.setStyleSheet("color: #8B949E; font-size: 12px;")
        layout.addWidget(sub)

        # ── stat cards ──────────────────────────────────────────────────────
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.card_labels = {}
        for key, title, colour in [
            ("total",     "TRACKED",        "#8B949E"),
            ("followed",  "FOLLOWED",       "#3FB950"),
            ("liked",     "LIKED",          "#F778BA"),
            ("commented", "COMMENTED",      "#D29922"),
            ("dmed",      "DM'D",           "#58A6FF"),
            ("engaged_not_dmed", "OWES A DM", "#FF7B72"),
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

        # ── controls ────────────────────────────────────────────────────────
        tools = QHBoxLayout()

        self.cmb_filter = QComboBox()
        self.cmb_filter.addItems([
            "All tracked",
            "Owes a DM (engaged, no DM)",
            "DM'd but not followed",
            "Followed",
            "Liked",
            "Commented",
            "DM'd",
            "Never engaged",
        ])
        self.cmb_filter.currentIndexChanged.connect(self.refresh)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search username...")
        self.txt_search.setFixedWidth(190)
        self.txt_search.textChanged.connect(self.apply_filter)

        btn_refresh = QPushButton("⟳ Refresh")
        btn_refresh.clicked.connect(self.refresh)

        btn_backfill = QPushButton("Backfill from history")
        btn_backfill.setToolTip(
            "Seed the tracker from your existing sent log and queue.\n"
            "Safe to run more than once."
        )
        btn_backfill.clicked.connect(self.backfill)

        btn_export = QPushButton("Export CSV")
        btn_export.clicked.connect(self.export_csv)

        tools.addWidget(self.cmb_filter)
        tools.addWidget(btn_refresh)
        tools.addWidget(btn_backfill)
        tools.addWidget(btn_export)
        tools.addStretch()
        tools.addWidget(self.txt_search)
        layout.addLayout(tools)

        # ── table ───────────────────────────────────────────────────────────
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["Username", "Followed", "Liked", "Liked #", "Commented", "DM'd", "Last action"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        self.setLayout(layout)

    # ── data ────────────────────────────────────────────────────────────────

    def refresh(self):
        s = self.tracker.stats()
        for k, lbl in self.card_labels.items():
            lbl.setText(str(s.get(k, 0)))

        idx = self.cmb_filter.currentIndex()
        if idx == 1:
            rows = self.tracker.get_engaged_not_dmed()
            rows = self._pad(rows)
        elif idx == 2:
            rows = self._pad(self.tracker.get_unfollowed_dmed())
        elif idx == 3:
            rows = [r for r in self.tracker.get_all() if r.get("followed")]
        elif idx == 4:
            rows = [r for r in self.tracker.get_all() if r.get("liked")]
        elif idx == 5:
            rows = [r for r in self.tracker.get_all() if r.get("commented")]
        elif idx == 6:
            rows = [r for r in self.tracker.get_all() if r.get("dmed")]
        elif idx == 7:
            rows = [r for r in self.tracker.get_all()
                    if not any(r.get(k) for k in ("followed", "liked", "commented", "dmed"))]
        else:
            rows = self.tracker.get_all()

        self._rows = rows
        self.table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            last = (row.get("dmed_at") or row.get("commented_at")
                    or row.get("liked_at") or row.get("followed_at")
                    or row.get("last_engage") or row.get("first_seen") or "")
            cells = [
                row.get("username", ""),
                TICK if row.get("followed") else CROSS,
                TICK if row.get("liked") else CROSS,
                str(row.get("liked_count") or "") or "",
                TICK if row.get("commented") else CROSS,
                TICK if row.get("dmed") else CROSS,
                str(last)[:19].replace("T", " "),
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c in (1, 2, 4, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if text == TICK:
                        item.setForeground(QBrush(QColor("#3FB950")))
                if c == 0:
                    item.setToolTip(row.get("comment_text") or "")
                self.table.setItem(r, c, item)

        self.apply_filter(self.txt_search.text())

    def _pad(self, rows):
        """Ensure rows from partial queries carry the full column set."""
        out = []
        full = {r["username"]: r for r in self.tracker.get_all()}
        for r in rows:
            u = r.get("username")
            merged = dict(full.get(u, {}))
            merged.update({k: v for k, v in r.items() if v is not None})
            out.append(merged)
        return out

    def apply_filter(self, text):
        term = (text or "").lower()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                self.table.setRowHidden(row, term not in item.text().lower())

    # ── actions ─────────────────────────────────────────────────────────────

    def backfill(self):
        n = self.tracker.backfill_from_history()
        self.refresh()
        dark_info(self, "Backfill complete",
                  f"Imported {n} new record(s) from your existing history.\n\n"
                  "Past DMs are now marked as sent so the tracker reflects reality.")

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export tracker CSV", "tracker.csv",
                                              "CSV Files (*.csv)")
        if not path:
            return
        import csv
        rows = self.tracker.get_all(limit=100000)
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["username", "followed", "followed_at", "liked", "liked_count",
                            "liked_at", "commented", "comment_text", "commented_at",
                            "dmed", "dmed_at"])
                for r in rows:
                    w.writerow([r.get("username"), r.get("followed"), r.get("followed_at"),
                                r.get("liked"), r.get("liked_count"), r.get("liked_at"),
                                r.get("commented"), r.get("comment_text"),
                                r.get("commented_at"), r.get("dmed"), r.get("dmed_at")])
            dark_info(self, "Exported", f"Wrote {len(rows)} row(s) to:\n{path}")
        except Exception as e:
            dark_info(self, "Export failed", str(e))
