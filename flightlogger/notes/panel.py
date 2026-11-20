"""Notes dock: list of timeline notes plus an editor for the selected one."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QColorDialog, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..plots.common import fmt_time
from ..timebus import TimeBus
from ..ui import icons
from .model import CATEGORIES, DEFAULT_CATEGORY, Note, NotesModel

ID_ROLE = Qt.UserRole


class NotesPanel(QWidget):
    noteSelected = Signal(str)
    exportRequested = Signal()

    def __init__(self, bus: TimeBus, model: NotesModel, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.model = model
        self._current: str | None = None
        self._loading = False

        add_btn = icons.decorate(QPushButton("Note"), "note", "primary")
        add_btn.setToolTip("Add a note at the red cursor (N)")
        add_btn.clicked.connect(lambda: self.add_at_cursor())
        self.del_btn = icons.decorate(QPushButton("Delete"), "trash")
        self.del_btn.clicked.connect(self.delete_current)
        top = QHBoxLayout()
        top.addWidget(add_btn)
        gen_btn = icons.decorate(QPushButton("General"), "note")
        gen_btn.setToolTip("Add an untimed note about the whole flight")
        gen_btn.clicked.connect(self.add_general)
        top.addWidget(gen_btn)
        top.addStretch(1)
        export_btn = QPushButton("Export")
        export_btn.setToolTip("Export all notes with timestamps as a text file")
        export_btn.clicked.connect(self.exportRequested)
        top.addWidget(export_btn)
        top.addWidget(self.del_btn)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Time", "End", "Category", "Title"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        for c in range(3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.cellClicked.connect(self._on_click)

        # editor
        self.title = QLineEdit(placeholderText="Title")
        self.category = QComboBox()
        self.category.setFocusPolicy(Qt.StrongFocus)
        for name in CATEGORIES:
            self.category.addItem(name)
        self.time_label = QLabel()
        self.time_label.setStyleSheet("background: transparent;")   # monospace only for times
        self.time_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.time_label.setWordWrap(True)
        self.set_t_btn = QPushButton("Set start")
        self.set_t_btn.setToolTip("Move the note to the red cursor")
        self.set_end_btn = QPushButton("Set end")
        self.set_end_btn.setToolTip("Make the note a span ending at the red cursor (Ctrl+N)")
        self.clear_end_btn = QPushButton("No end")
        self.clear_end_btn.setToolTip("Make the note a single instant again")
        self.color_btn = QPushButton("Color…")
        self.color_btn.setToolTip("Pick a custom color for this note (right-click: back to the category color)")
        self.color_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.color_btn.clicked.connect(self._pick_color)
        self.color_btn.customContextMenuRequested.connect(lambda _p: self._edit(custom_color=None))
        self.text = QPlainTextEdit(placeholderText="Comment…")
        self.title.textEdited.connect(lambda s: self._edit(title=s))
        self.category.activated.connect(lambda _i: self._edit(category=self.category.currentText()))
        self.text.textChanged.connect(self._on_text)
        self.set_t_btn.clicked.connect(lambda: self._edit(t=self.bus.t, general=False))
        self.set_end_btn.clicked.connect(lambda: self._edit(t_end=self.bus.t))
        self.clear_end_btn.clicked.connect(lambda: self._edit(t_end=None))

        editor = QWidget()
        el = QVBoxLayout(editor)
        el.setContentsMargins(0, 0, 0, 0)
        r1 = QHBoxLayout()
        r1.addWidget(self.category)
        r1.addWidget(self.color_btn)
        r1.addWidget(self.time_label, 1)
        r2 = QHBoxLayout()
        r2.addWidget(self.set_t_btn)
        r2.addWidget(self.set_end_btn)
        r2.addWidget(self.clear_end_btn)
        r2.addStretch(1)
        el.addWidget(self.title)
        el.addLayout(r1)
        el.addLayout(r2)
        el.addWidget(self.text, 1)
        self.editor = editor

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(editor)
        split.setSizes([200, 160])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(top)
        lay.addWidget(split, 1)

        self._text_timer = QTimer(self, singleShot=True, interval=300)
        self._text_timer.timeout.connect(lambda: self._edit(text=self.text.toPlainText()))
        model.changed.connect(self.refresh)
        self.refresh()

    # --- actions ---
    def add_at_cursor(self, category: str = DEFAULT_CATEGORY) -> Note:
        n = self.model.add(Note(t=self.bus.t, title=f"Note at {fmt_time(self.bus.t, 1)}",
                                category=category))
        self.select(n.id)
        self.title.setFocus()
        self.title.selectAll()
        return n

    def add_general(self) -> Note:
        n = self.model.add(Note(t=0.0, title="General note", general=True))
        self.select(n.id)
        self.title.setFocus()
        self.title.selectAll()
        return n

    def delete_current(self):
        if self._current:
            nid, self._current = self._current, None
            self.model.remove(nid)

    def _pick_color(self):
        n = self.model.get(self._current) if self._current else None
        if n is None:
            return
        c = QColorDialog.getColor(QColor(n.color), self, "Note color")
        if c.isValid():
            self._edit(custom_color=c.name())

    def set_end_at_cursor(self):
        """Ctrl+N: end the selected note at the red cursor (makes it a span)."""
        n = self.model.get(self._current) if self._current else None
        if n is not None and not n.general:
            self._edit(t_end=self.bus.t)

    def has_selection(self) -> bool:
        return self._current is not None

    def select(self, note_id: str):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(ID_ROLE) == note_id:
                self.table.selectRow(r)
                return

    # --- view ---
    def refresh(self):
        self._loading = True
        notes = self.model.notes
        self.table.setRowCount(len(notes))
        for r, n in enumerate(notes):
            cells = ["General" if n.general else fmt_time(n.t, 1),
                     "" if n.t_end is None or n.general else fmt_time(n.t_end, 1),
                     n.category, n.title]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setData(ID_ROLE, n.id)
                if c == 2:
                    it.setForeground(QBrush(QColor(n.color)))
                self.table.setItem(r, c, it)
        if not (self._current and self.model.get(self._current)):
            # a stale selection would make the next click on the remaining row a no-op
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
        self._loading = False
        if self._current and self.model.get(self._current):
            self.select(self._current)
            self._show(self.model.get(self._current))
        else:
            self._current = None
            self._show(None)

    def _show(self, n: Note | None):
        self._loading = True
        self.editor.setEnabled(n is not None)
        self.del_btn.setEnabled(n is not None)
        if n is not None:
            self.color_btn.setStyleSheet(f"border-left: 6px solid {n.color};")
        if n is None:
            self.title.clear()
            self.text.clear()
            self.time_label.setStyleSheet("background: transparent;")
            self.time_label.setText("Select a note, or press N to add one")
        else:
            if self.title.text() != n.title:
                self.title.setText(n.title)
            self.category.setCurrentText(n.category)
            if self.text.toPlainText() != n.text:
                self.text.setPlainText(n.text)
            span = "" if n.t_end is None else f" → {fmt_time(n.t_end)}"
            self.time_label.setStyleSheet("font-family: monospace; background: transparent;")
            self.time_label.setText("General (untimed)" if n.general else f"{fmt_time(n.t)}{span}")
            for b in (self.set_end_btn, self.clear_end_btn):
                b.setEnabled(not n.general)
            self.set_t_btn.setText("Set time" if n.general else "Set start")
        self._loading = False

    def _on_selection(self):
        if self._loading:
            return
        rows = self.table.selectionModel().selectedRows()
        nid = self.table.item(rows[0].row(), 0).data(ID_ROLE) if rows else None
        if nid != self._current:
            self._text_timer.stop()
            self._current = nid
            self._show(self.model.get(nid) if nid else None)
            if nid:
                self.noteSelected.emit(nid)

    def _on_click(self, row, _col):
        nid = self.table.item(row, 0).data(ID_ROLE)
        n = self.model.get(nid)
        if n is not None:
            if nid != self._current:   # selection did not change (e.g. only row left)
                self._text_timer.stop()
                self._current = nid
                self._show(n)
                self.noteSelected.emit(nid)
            if not n.general:
                self.bus.seek(n.t)

    def _on_text(self):
        if not self._loading and self._current:
            self._text_timer.start()

    def _edit(self, **fields):
        if self._loading or not self._current:
            return
        if "text" not in fields:
            self._text_timer.stop()
            fields.setdefault("text", self.text.toPlainText())
        self.model.update(self._current, **fields)
