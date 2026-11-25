"""Notes dock: list of timeline notes plus an editor for the selected one."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QColorDialog, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMenu, QToolButton, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..plots.common import fmt_time
from ..timebus import TimeBus
from ..ui import icons
from .model import CATEGORIES, DEFAULT_CATEGORY, Note, NotesModel

ID_ROLE = Qt.UserRole


class NotesPanel(QWidget):
    noteSelected = Signal(str)
    exportRequested = Signal()
    hiddenSetsChanged = Signal(object)     # set[str] of hidden set names ("" = no set)

    def __init__(self, bus: TimeBus, model: NotesModel, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.model = model
        self._current: str | None = None
        self._loading = False
        self.hidden_sets: set[str] = set()

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
        self.sets_btn = QToolButton()
        self.sets_btn.setText("Sets \u25be")
        self.sets_btn.setPopupMode(QToolButton.InstantPopup)
        self.sets_btn.setToolTip("Show or hide notes by set (hidden sets leave the plots, timeline, video and list)")
        self.sets_menu = QMenu(self.sets_btn)
        self.sets_menu.aboutToShow.connect(self._build_sets_menu)
        self.sets_btn.setMenu(self.sets_menu)
        top.addWidget(self.sets_btn)
        export_btn = QPushButton("Export")
        export_btn.setToolTip("Export all notes with timestamps as a text file")
        export_btn.clicked.connect(self.exportRequested)
        top.addWidget(export_btn)
        top.addWidget(self.del_btn)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "End", "Category", "Title", "Set"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        hh = self.table.horizontalHeader()
        for c in (0, 1, 2, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.cellClicked.connect(self._on_click)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        copy_act = QAction("Copy", self.table)
        copy_act.setShortcut(QKeySequence.Copy)
        copy_act.setShortcutContext(Qt.WidgetShortcut)
        copy_act.triggered.connect(self.copy_selected)
        self.table.addAction(copy_act)

        # editor
        self.title = QLineEdit(placeholderText="Title")
        self.category = QComboBox()
        self.category.setFocusPolicy(Qt.StrongFocus)
        for name in CATEGORIES:
            self.category.addItem(name)
        self.set_combo = QComboBox()
        self.set_combo.setEditable(True)
        self.set_combo.setInsertPolicy(QComboBox.NoInsert)
        self.set_combo.setCompleter(None)
        self.set_combo.lineEdit().setPlaceholderText("Set (none)")
        self.set_combo.setToolTip("Note set: type a new name to create one")
        self.set_combo.lineEdit().textEdited.connect(lambda s: self._edit(set=s.strip()))
        self.set_combo.activated.connect(lambda _i: self._edit(set=self.set_combo.currentText().strip()))
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
        r1.addWidget(self.set_combo)
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
        self._unhide_for_new()
        n = self.model.add(Note(t=self.bus.t, title=f"Note at {fmt_time(self.bus.t, 1)}",
                                category=category))
        self.select(n.id)
        self.title.setFocus()
        self.title.selectAll()
        return n

    def add_general(self) -> Note:
        self._unhide_for_new()
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
        notes = [n for n in self.model if n.set not in self.hidden_sets]
        self._sync_set_items()
        self.table.setRowCount(len(notes))
        for r, n in enumerate(notes):
            cells = ["General" if n.general else fmt_time(n.t, 1),
                     "" if n.t_end is None or n.general else fmt_time(n.t_end, 1),
                     n.category, n.title, n.set]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setData(ID_ROLE, n.id)
                if c == 2:
                    it.setForeground(QBrush(QColor(n.color)))
                self.table.setItem(r, c, it)
        visible = {n.id for n in notes}
        if self._current not in visible:
            # a stale selection would make the next click on the remaining row a no-op
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
        self._loading = False
        if self._current in visible:
            self.select(self._current)
            self._show(self.model.get(self._current))
        else:
            self._current = None
            self._show(None)

    def _sync_set_items(self):
        """Keep the combo's items equal to the existing sets without disturbing typing."""
        names = self.set_names()
        have = [self.set_combo.itemText(i) for i in range(self.set_combo.count())]
        if names != have:
            was = self._loading
            self._loading = True
            le = self.set_combo.lineEdit()
            txt, pos = le.text(), le.cursorPosition()
            self.set_combo.clear()
            self.set_combo.addItems(names)
            le.setText(txt)
            le.setCursorPosition(pos)
            self._loading = was

    def _show(self, n: Note | None):
        self._loading = True
        self.editor.setEnabled(n is not None)
        self.del_btn.setEnabled(n is not None)
        if n is not None:
            self.color_btn.setStyleSheet(f"border-left: 6px solid {n.color};")
        if n is None:
            self.title.clear()
            self.set_combo.setEditText("")
            self.text.clear()
            self.time_label.setStyleSheet("background: transparent;")
            self.time_label.setText("Select a note, or press N to add one")
        else:
            if self.title.text() != n.title:
                self.title.setText(n.title)
            self.category.setCurrentText(n.category)
            if self.set_combo.currentText() != n.set:
                self.set_combo.setEditText(n.set)
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
        rows = self._rows()
        cur = self.table.currentRow()
        row = cur if cur in rows else (rows[0] if rows else None)
        nid = self.table.item(row, 0).data(ID_ROLE) if row is not None else None
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

    def flush_edits(self):
        """Apply a comment typed in the last moments (it is committed after a short delay):
        call before saving, exporting or closing so nothing typed is left out."""
        if self._text_timer.isActive() and self._current:
            self._text_timer.stop()
            self._edit(text=self.text.toPlainText())

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

    # --- sets ---
    def set_names(self) -> list[str]:
        return sorted({n.set for n in self.model if n.set})

    def set_hidden_sets(self, names):
        names = set(names)
        if names != self.hidden_sets:
            self.hidden_sets = names
            self.refresh()

    def _build_sets_menu(self):
        m = self.sets_menu
        m.clear()
        counts: dict[str, int] = {}
        for n in self.model:
            counts[n.set] = counts.get(n.set, 0) + 1
        for name in [""] + self.set_names():
            label = f"{name or '(no set)'} ({counts.get(name, 0)})"
            a = m.addAction(label)
            a.setCheckable(True)
            a.setChecked(name not in self.hidden_sets)
            a.toggled.connect(lambda on, name=name: self._toggle_set(name, on))
        m.addSeparator()
        m.addAction("Show all", lambda: self._emit_hidden(set()))

    def _toggle_set(self, name: str, shown: bool):
        hidden = set(self.hidden_sets)
        (hidden.discard if shown else hidden.add)(name)
        self._emit_hidden(hidden)

    def _emit_hidden(self, hidden: set):
        self.set_hidden_sets(hidden)
        self.hiddenSetsChanged.emit(set(hidden))

    def _unhide_for_new(self):
        if "" in self.hidden_sets:      # a new note has no set: keep it visible
            self._emit_hidden(self.hidden_sets - {""})

    # --- copy ---
    def _rows(self) -> list[int]:
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def _note_at(self, row: int) -> Note | None:
        it = self.table.item(row, 0)
        return self.model.get(it.data(ID_ROLE)) if it is not None else None

    @staticmethod
    def _flat(s: str) -> str:
        return s.replace("\t", " ").replace("\r\n", " \u23ce ").replace("\n", " \u23ce ")

    def _note_line(self, n: Note) -> str:
        return "\t".join([("General" if n.general else fmt_time(n.t, 1)), n.category, n.set,
                          self._flat(n.title), self._flat(n.text)])

    def copy_selected(self):
        lines = [self._note_line(n) for n in map(self._note_at, self._rows()) if n is not None]
        if lines:
            QGuiApplication.clipboard().setText("\n".join(lines))

    def _table_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        if row not in self._rows():
            self.table.selectRow(row)
        n = self._note_at(row)
        if n is None:
            return
        clip = QGuiApplication.clipboard()
        menu = QMenu(self)
        menu.addAction("Copy", self.copy_selected)
        menu.addAction("Copy title", lambda: clip.setText(n.title))
        menu.addAction("Copy comment", lambda: clip.setText(n.text))
        menu.addAction("Copy note", lambda: clip.setText(self._note_line(n)))
        menu.exec(self.table.viewport().mapToGlobal(pos))
