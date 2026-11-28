"""Parameter comparison: one row per parameter, one column per imported log, differences tinted."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMenu, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..ui import theme
from ..ui.tables import _ALL, _CHANGED, CopyKeyFilter, RED, _fmt, _param_group, fill_group_list
from .logs import LogSet

_FIXED = 2   # Name, Group; then one column per log; then Default (optional)


def diff_tint() -> QColor:
    c = QColor(RED)
    c.setAlpha(95 if theme.is_dark() else 70)
    return c


def _same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-5
    except (TypeError, ValueError):
        return a == b


class ParamsCompare(QWidget):
    _splitter_sizes: list | None = None

    def __init__(self, logset: LogSet, parent=None):
        super().__init__(parent)
        self.logset = logset
        self.filter = QLineEdit(placeholderText="Filter parameters", clearButtonEnabled=True)
        self.only_diff = QCheckBox("Only differences")
        self.only_diff.setChecked(True)
        self.count_label = QLabel()
        theme.style_role(self.count_label, "dim")
        self.table = QTableWidget(0, _FIXED)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.installEventFilter(self)
        self._copy_filter = CopyKeyFilter(self.table, self.copy_selected)
        self.group_list = QListWidget()
        self.group_list.setMinimumWidth(80)
        self.group_list.currentItemChanged.connect(lambda *_: self._apply_filter())
        self.filter.textChanged.connect(self._apply_filter)
        self.only_diff.toggled.connect(self._apply_filter)

        row = QHBoxLayout()
        row.addWidget(self.filter, 1)
        row.addWidget(self.only_diff)
        row.addWidget(self.count_label)
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.addLayout(row)
        rlay.addWidget(self.table)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.group_list)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([170, 700])
        self.splitter.splitterMoved.connect(
            lambda *_: setattr(ParamsCompare, "_splitter_sizes", self.splitter.sizes()))
        if ParamsCompare._splitter_sizes:
            self.splitter.setSizes(ParamsCompare._splitter_sizes)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.splitter)

        self._sig = None
        self._has_default = False
        self.n_diff = 0
        self.rows: dict[str, int] = {}
        logset.changed.connect(self.refresh)
        self.refresh()

    # --- data ---
    def _signature(self):
        return tuple((id(e), e.name, e.log is not None) for e in self.logset.entries)

    def refresh(self):
        """Rebuild when logs were added/removed/renamed/loaded (not on offset or active changes)."""
        sig = self._signature()
        if sig != self._sig:
            self._sig = sig
            self._rebuild()

    def _rebuild(self):
        entries = [e for e in self.logset.entries if e.log is not None]
        defaults = next((d for d in (e.log.default_params() for e in entries) if d), None)
        self._has_default = defaults is not None
        names = sorted({n for e in entries for n in e.log.params})
        ncol = _FIXED + len(entries) + (1 if self._has_default else 0)
        t = self.table
        t.setSortingEnabled(False)
        t.clear()
        t.setColumnCount(ncol)
        t.setHorizontalHeaderLabels(["Name", "Group"] + [e.name for e in self.logset.entries
                                                         if e.log is not None]
                                    + (["Default"] if self._has_default else []))
        t.setRowCount(len(names))
        tint = QBrush(diff_tint())
        counts: dict[str, int] = {}
        self.n_diff = 0
        self.rows = {}
        for r, name in enumerate(names):
            vals = [e.log.params.get(name) for e in entries]
            present = [v for v in vals if v is not None]
            differs = len(present) < len(vals) or any(not _same(present[0], v) for v in present[1:])
            group = _param_group(name)
            counts[group] = counts.get(group, 0) + 1
            self.n_diff += differs
            self.rows[name] = r
            default = defaults.get(name) if defaults else None
            cells = [QTableWidgetItem(name), QTableWidgetItem(group)]
            for v in vals:
                it = QTableWidgetItem("—" if v is None else _fmt(v))
                it.setData(Qt.UserRole + 1, v)
                if differs:
                    it.setBackground(tint)
                cells.append(it)
            if self._has_default:
                it = QTableWidgetItem("" if default is None else _fmt(default))
                it.setData(Qt.UserRole + 1, default)
                cells.append(it)
            cells[0].setData(Qt.UserRole, differs)
            for c, it in enumerate(cells):
                t.setItem(r, c, it)
        t.setSortingEnabled(True)
        t.sortByColumn(0, Qt.AscendingOrder)
        t.resizeColumnsToContents()
        fill_group_list(self.group_list, len(names), counts,
                        self.n_diff if len(entries) > 1 else None, "Differences")
        self._apply_filter()

    def difference_count(self) -> int:
        return self.n_diff

    def is_highlighted(self, name: str, col: int) -> bool:
        """Whether the cell of parameter `name` in table column `col` carries the difference tint."""
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).text() == name:
                return self.table.item(r, col).background().style() != Qt.NoBrush
        return False

    def visible_names(self) -> list[str]:
        return [self.table.item(r, 0).text() for r in range(self.table.rowCount())
                if not self.table.isRowHidden(r)]

    def _apply_filter(self, *_):
        words = self.filter.text().upper().split()
        only = self.only_diff.isChecked()
        cur = self.group_list.currentItem()
        sel = cur.data(Qt.UserRole) if cur else _ALL
        shown = 0
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            diff = it.data(Qt.UserRole)
            hide = ((words and not all(w in it.text() for w in words)) or (only and not diff)
                    or (sel == _CHANGED and not diff)
                    or (sel not in (_ALL, _CHANGED) and self.table.item(r, 1).text() != sel))
            self.table.setRowHidden(r, bool(hide))
            shown += not hide
        self.count_label.setText(f"{shown} shown, {self.n_diff} differ")

    def select_group(self, key: str) -> bool:
        key = {"All": _ALL, "Changed": _CHANGED, "Differences": _CHANGED}.get(key, key)
        for i in range(self.group_list.count()):
            if self.group_list.item(i).data(Qt.UserRole) == key:
                self.group_list.setCurrentRow(i)
                return True
        return False

    # --- help, copy ---
    def eventFilter(self, obj, ev):
        if (obj is self.table and ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_H
                and not ev.modifiers() and not ev.isAutoRepeat()):
            self.show_help()
            return True
        return super().eventFilter(obj, ev)

    def show_help(self, row: int | None = None):
        r = self.table.currentRow() if row is None else row
        if r < 0 or self.table.item(r, 0) is None:
            return
        value = next((self.table.item(r, c).data(Qt.UserRole + 1)
                      for c in range(_FIXED, self.table.columnCount())
                      if self.table.item(r, c).data(Qt.UserRole + 1) is not None), None)
        default = (self.table.item(r, self.table.columnCount() - 1).data(Qt.UserRole + 1)
                   if self._has_default else None)
        from ..ui.param_help import ParamHelpPopup
        ParamHelpPopup.show_for(self.table.item(r, 0).text(), value, default, self.window())

    def copy_selected(self):
        """Selected rows as tab-separated text, with a header line."""
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        cols = range(self.table.columnCount())
        if not rows:
            return
        head = "\t".join(self.table.horizontalHeaderItem(c).text() for c in cols)
        lines = [head] + ["\t".join(self.table.item(r, c).text() for c in cols) for r in rows]
        QGuiApplication.clipboard().setText("\n".join(lines))

    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        if row not in {i.row() for i in self.table.selectedIndexes()}:
            self.table.selectRow(row)
        menu = QMenu(self)
        menu.addAction("Parameter help (H)", lambda: self.show_help(row))
        menu.addAction("Copy", self.copy_selected)
        menu.exec(self.table.viewport().mapToGlobal(pos))
