"""Logged messages and parameters tables (Flight Review shows both below the graphs)."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QMenu, QCheckBox, QHBoxLayout, QHeaderView, QLineEdit,
                               QListWidget, QListWidgetItem, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..log.ulog_loader import FlightLog
from ..plots.common import fmt_time
from ..timebus import TimeBus
from . import theme

LEVEL_COLORS = {"WARNING": QColor(255, 236, 200), "ERROR": QColor(255, 210, 210),
                "CRITICAL": QColor(255, 190, 190), "ALERT": QColor(255, 190, 190),
                "EMERGENCY": QColor(255, 170, 170)}
RED = QColor("#d55e00")  # Flight Review: non-airframe-default values in red


class CopyKeyFilter(QObject):
    """Ctrl+C in a table: a key filter, not a QAction (every shortcut action is made app-wide, and
    two of them with the same keys cancel each other)."""

    def __init__(self, table, callback):
        super().__init__(table)
        self.callback = callback
        table.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.KeyPress and ev.matches(QKeySequence.Copy):
            self.callback()
            return True
        return False


def _table(headers) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setWordWrap(False)
    t.horizontalHeader().setStretchLastSection(True)
    return t


class MessagesTable(QWidget):
    """Click a message to jump there."""

    def __init__(self, bus: TimeBus, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.table = _table(["Time", "Level", "Message"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.cellClicked.connect(self._on_click)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self._copy_filter = CopyKeyFilter(self.table, self.copy_selected)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.table)

    def copy_selected(self):
        """Selected rows as tab-separated text: time, level, message."""
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        lines = ["\t".join(self.table.item(r, c).text() for c in range(3)) for r in rows
                 if self.table.item(r, 0) is not None]
        if lines:
            QGuiApplication.clipboard().setText("\n".join(lines))

    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        if row not in {i.row() for i in self.table.selectedIndexes()}:
            self.table.selectRow(row)
        menu = QMenu(self)
        menu.addAction("Copy", self.copy_selected)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def set_log(self, log: FlightLog | None):
        msgs = log.messages() if log else []
        self.table.setRowCount(len(msgs))
        for r, m in enumerate(msgs):
            cells = [QTableWidgetItem(fmt_time(m.t, 1)), QTableWidgetItem(m.level),
                     QTableWidgetItem(m.text)]
            cells[0].setData(Qt.UserRole, m.t)
            bg = LEVEL_COLORS.get(m.level)
            bg = theme.row_tint(bg) if bg is not None else None
            for c, it in enumerate(cells):
                if bg is not None:
                    it.setBackground(QBrush(bg))
                self.table.setItem(r, c, it)

    def _on_click(self, row, _col):
        it = self.table.item(row, 0)
        if it is not None:
            self.bus.seek(float(it.data(Qt.UserRole)))


_ALL, _CHANGED = "\0all", "\0changed"
_COL_NAME, _COL_GROUP, _COL_VALUE, _COL_DEFAULT = 0, 1, 2, 3


def _param_group(name: str) -> str:
    from ..log import param_meta
    meta = param_meta.lookup(name)
    if meta is None:
        return "Other"
    g = meta.get("group")
    return g or (name.split("_", 1)[0] if "_" in name else "Other")


class ParamsTable(QWidget):
    _splitter_sizes: list | None = None     # remembered in-session only

    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter = QLineEdit(placeholderText="Filter parameters", clearButtonEnabled=True)
        self.changed_only = QCheckBox("Changed from default only")
        self.filter.textChanged.connect(self._apply_filter)
        self.changed_only.toggled.connect(self._apply_filter)
        self.table = _table(["Name", "Group", "Value", "Default"])
        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.installEventFilter(self)
        self._defaults: dict | None = None
        self.group_list = QListWidget()
        self.group_list.setMinimumWidth(80)
        self.group_list.currentItemChanged.connect(lambda *_: self._apply_filter())
        row = QHBoxLayout()
        row.addWidget(self.filter, 1)
        row.addWidget(self.changed_only)
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
            lambda *_: setattr(ParamsTable, "_splitter_sizes", self.splitter.sizes()))
        if ParamsTable._splitter_sizes:
            self.splitter.setSizes(ParamsTable._splitter_sizes)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.splitter)

    def set_log(self, log: FlightLog | None):
        self.table.setSortingEnabled(False)
        params = log.params if log else {}
        defaults = log.default_params() if log else None
        self._defaults = defaults
        self.changed_only.setEnabled(defaults is not None)
        self.changed_only.setToolTip("" if defaults is not None
                                     else "This log does not contain default values")
        self.table.setRowCount(len(params))
        counts: dict[str, int] = {}
        n_changed = 0
        for r, name in enumerate(sorted(params)):
            value = params[name]
            default = defaults.get(name) if defaults else None
            changed = default is not None and abs(float(default) - float(value)) > 1e-5
            group = _param_group(name)
            counts[group] = counts.get(group, 0) + 1
            n_changed += bool(changed)
            cells = [QTableWidgetItem(name), QTableWidgetItem(group), QTableWidgetItem(_fmt(value)),
                     QTableWidgetItem("" if default is None else _fmt(default))]
            cells[0].setData(Qt.UserRole, changed)
            cells[2].setData(Qt.UserRole + 1, value)      # raw values for parameter help
            cells[3].setData(Qt.UserRole + 1, default)
            for c, it in enumerate(cells):
                if changed:
                    it.setForeground(QBrush(RED))
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.resizeColumnsToContents()
        self._fill_groups(len(params), counts, n_changed if defaults is not None else None)

    def _fill_groups(self, total: int, counts: dict, n_changed: int | None):
        """Group list: All, Changed from default (if defaults exist), then groups A-Z."""
        prev = self.group_list.currentItem()
        prev_key = prev.data(Qt.UserRole) if prev else _ALL
        lst = self.group_list
        lst.blockSignals(True)
        lst.clear()
        entries = [(f"All ({total})", _ALL)]
        if n_changed is not None:
            entries.append((f"Changed from default ({n_changed})", _CHANGED))
        entries += [(f"{g} ({n})", g) for g, n in sorted(counts.items(), key=lambda kv: kv[0].lower())]
        sel = 0
        for i, (label, key) in enumerate(entries):
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, key)
            lst.addItem(it)
            if key == prev_key:
                sel = i
        lst.setCurrentRow(sel)
        lst.blockSignals(False)
        self._apply_filter()

    def select_group(self, key: str) -> bool:
        """Select a group by name (or "All" / "Changed"); False when not in the list."""
        key = {"All": _ALL, "Changed": _CHANGED}.get(key, key)
        for i in range(self.group_list.count()):
            if self.group_list.item(i).data(Qt.UserRole) == key:
                self.group_list.setCurrentRow(i)
                return True
        return False

    def group_names(self) -> list[str]:
        """The list's labels, e.g. "All (1250)", "Battery (12)"."""
        return [self.group_list.item(i).text() for i in range(self.group_list.count())]

    # --- parameter help (H), copy ---
    def eventFilter(self, obj, ev):
        if (obj is self.table and ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_H
                and not ev.modifiers() and not ev.isAutoRepeat()):
            self.show_help()
            return True
        return super().eventFilter(obj, ev)

    def _current(self, row: int | None = None):
        r = self.table.currentRow() if row is None else row
        if r < 0 or self.table.item(r, 0) is None:
            return None
        it = self.table.item
        return (it(r, 0).text(), it(r, _COL_VALUE).data(Qt.UserRole + 1),
                it(r, _COL_DEFAULT).data(Qt.UserRole + 1), it(r, _COL_VALUE).text())

    def show_help(self, row: int | None = None):
        cur = self._current(row)
        if cur:
            from .param_help import ParamHelpPopup
            ParamHelpPopup.show_for(cur[0], cur[1], cur[2], self.window())

    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        cur = self._current(row)
        if not cur:
            return
        self.table.selectRow(row)
        menu = QMenu(self)
        menu.addAction("Parameter help (H)", lambda: self.show_help(row))
        menu.addAction("Copy name", lambda: QGuiApplication.clipboard().setText(cur[0]))
        menu.addAction("Copy value", lambda: QGuiApplication.clipboard().setText(cur[3]))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _apply_filter(self, *_):
        words = self.filter.text().upper().split()
        only = self.changed_only.isChecked() and self.changed_only.isEnabled()
        cur = self.group_list.currentItem()
        sel = cur.data(Qt.UserRole) if cur else _ALL
        if sel == _CHANGED and not self.changed_only.isEnabled():
            sel = _ALL
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            name = it.text()
            changed = it.data(Qt.UserRole)
            hide = ((words and not all(w in name for w in words)) or (only and not changed)
                    or (sel == _CHANGED and not changed)
                    or (sel not in (_ALL, _CHANGED) and self.table.item(r, _COL_GROUP).text() != sel))
            self.table.setRowHidden(r, bool(hide))


def _fmt(v) -> str:
    return str(v) if isinstance(v, int) else f"{v:.6g}"
