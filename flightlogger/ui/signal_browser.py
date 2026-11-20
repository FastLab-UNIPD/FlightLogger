"""Searchable tree of every topic/field in the log. Drag fields onto graph panels."""
from __future__ import annotations

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QLineEdit, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..log.ulog_loader import FlightLog, SignalKey
from ..plots.common import MIME_SIGNALS

KEY_ROLE = Qt.UserRole


class _Tree(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.setHeaderLabels(["Signal", "Hz"])
        self.setColumnWidth(0, 260)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setUniformRowHeights(True)

    def mimeTypes(self):
        return [MIME_SIGNALS]

    def mimeData(self, items):
        keys = selected_keys(items)
        md = QMimeData()
        md.setData(MIME_SIGNALS, "\n".join(keys).encode())
        md.setText("\n".join(keys))
        return md


def selected_keys(items) -> list[str]:
    return [it.data(0, KEY_ROLE) for it in items if it.data(0, KEY_ROLE)]


class SignalBrowser(QWidget):
    signalsActivated = Signal(list)  # double-click / Enter -> add to active panel

    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter = QLineEdit(placeholderText="Filter, e.g. 'attitude roll'", clearButtonEnabled=True)
        self.filter.textChanged.connect(self._apply_filter)
        self.tree = _Tree()
        self.tree.itemDoubleClicked.connect(self._on_double)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.filter)
        lay.addWidget(self.tree)

    def set_log(self, log: FlightLog | None):
        self.tree.clear()
        if log is None:
            return
        italic = QFont()
        italic.setItalic(True)
        multi = {t.name for t in log.topics() if t.instance > 0}
        for ti in log.topics():
            name = f"{ti.name} [{ti.instance}]" if ti.name in multi else ti.name
            top = QTreeWidgetItem([name, f"{ti.rate_hz:.0f}"])
            top.setToolTip(0, f"{ti.n_samples} samples")
            top.setFlags(top.flags() & ~Qt.ItemIsDragEnabled)
            for f in ti.derived:
                ch = QTreeWidgetItem([f, ""])
                ch.setFont(0, italic)
                ch.setToolTip(0, "Derived (computed by FlightLogger)")
                ch.setData(0, KEY_ROLE, str(SignalKey(ti.name, ti.instance, f)))
                top.addChild(ch)
            for f in ti.fields:
                ch = QTreeWidgetItem([f, ""])
                ch.setData(0, KEY_ROLE, str(SignalKey(ti.name, ti.instance, f)))
                top.addChild(ch)
            self.tree.addTopLevelItem(top)
        self._apply_filter(self.filter.text())

    def _apply_filter(self, text: str):
        words = text.lower().split()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            tname = top.text(0).lower()
            any_vis = False
            for j in range(top.childCount()):
                ch = top.child(j)
                hay = f"{tname} {ch.text(0).lower()}"
                vis = all(w in hay for w in words)
                ch.setHidden(not vis)
                any_vis |= vis
            top.setHidden(not any_vis)
            top.setExpanded(bool(words) and any_vis)

    def _on_double(self, item, _col):
        keys = selected_keys(self.tree.selectedItems()) or selected_keys([item])
        if keys:
            self.signalsActivated.emit(keys)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            keys = selected_keys(self.tree.selectedItems())
            if keys:
                self.signalsActivated.emit(keys)
                return
        super().keyPressEvent(ev)
