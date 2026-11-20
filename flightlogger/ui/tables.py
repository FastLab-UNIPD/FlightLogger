"""Logged messages and parameters tables (Flight Review shows both below the graphs)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLineEdit,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..log.ulog_loader import FlightLog
from ..plots.common import fmt_time
from ..timebus import TimeBus
from . import theme

LEVEL_COLORS = {"WARNING": QColor(255, 236, 200), "ERROR": QColor(255, 210, 210),
                "CRITICAL": QColor(255, 190, 190), "ALERT": QColor(255, 190, 190),
                "EMERGENCY": QColor(255, 170, 170)}
RED = QColor("#d55e00")  # Flight Review: non-airframe-default values in red


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
        self.table.cellClicked.connect(self._on_click)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.table)

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


class ParamsTable(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter = QLineEdit(placeholderText="Filter parameters", clearButtonEnabled=True)
        self.changed_only = QCheckBox("Changed from default only")
        self.filter.textChanged.connect(self._apply_filter)
        self.changed_only.toggled.connect(self._apply_filter)
        self.table = _table(["Name", "Value", "Default"])
        self.table.setSortingEnabled(True)
        row = QHBoxLayout()
        row.addWidget(self.filter, 1)
        row.addWidget(self.changed_only)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.table)

    def set_log(self, log: FlightLog | None):
        self.table.setSortingEnabled(False)
        params = log.params if log else {}
        defaults = log.default_params() if log else None
        self.changed_only.setEnabled(defaults is not None)
        self.changed_only.setToolTip("" if defaults is not None
                                     else "This log does not contain default values")
        self.table.setRowCount(len(params))
        for r, name in enumerate(sorted(params)):
            value = params[name]
            default = defaults.get(name) if defaults else None
            changed = default is not None and abs(float(default) - float(value)) > 1e-5
            cells = [QTableWidgetItem(name), QTableWidgetItem(_fmt(value)),
                     QTableWidgetItem("" if default is None else _fmt(default))]
            cells[0].setData(Qt.UserRole, changed)
            for c, it in enumerate(cells):
                if changed:
                    it.setForeground(QBrush(RED))
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.resizeColumnsToContents()
        self._apply_filter()

    def _apply_filter(self, *_):
        words = self.filter.text().upper().split()
        only = self.changed_only.isChecked() and self.changed_only.isEnabled()
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            name = it.text()
            hide = (words and not all(w in name for w in words)) or (only and not it.data(Qt.UserRole))
            self.table.setRowHidden(r, bool(hide))


def _fmt(v) -> str:
    return str(v) if isinstance(v, int) else f"{v:.6g}"
