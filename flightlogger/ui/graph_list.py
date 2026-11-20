"""Checklist of Flight Review graphs available in the log.

A tick means "this graph is in the active panel". Ticking adds it there (in
Flight Review's order), unticking removes it. Graphs can also be dragged onto
any panel, at a chosen position.
"""
from __future__ import annotations

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from ..plots.common import MIME_GRAPHS

ID_ROLE = Qt.UserRole
KIND_HINT = {"spectrogram": "  ▦", "fft": "  ∿ Hz"}


class _List(QListWidget):
    def __init__(self):
        super().__init__()
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)

    def mimeTypes(self):
        return [MIME_GRAPHS]

    def mimeData(self, items):
        md = QMimeData()
        md.setData(MIME_GRAPHS, "\n".join(it.data(ID_ROLE) for it in items).encode())
        return md


class GraphList(QWidget):
    toggled = Signal(str, bool)      # graph_id, wanted in active panel
    allRequested = Signal(bool)      # True = add all, False = remove all

    def __init__(self, parent=None):
        super().__init__(parent)
        self.target_label = QLabel()
        self.target_label.setStyleSheet("color: gray;")
        self.list = _List()
        self.list.itemChanged.connect(self._on_item_changed)
        all_btn = QPushButton("All")
        none_btn = QPushButton("None")
        all_btn.clicked.connect(lambda: self.allRequested.emit(True))
        none_btn.clicked.connect(lambda: self.allRequested.emit(False))
        row = QHBoxLayout()
        row.addWidget(self.target_label, 1)
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(row)
        lay.addWidget(self.list)
        self._updating = False

    def set_graphs(self, graphs: dict):
        self._updating = True
        self.list.clear()
        for gid, g in graphs.items():
            it = QListWidgetItem(g.title + KIND_HINT.get(g.kind, ""))
            it.setData(ID_ROLE, gid)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            it.setCheckState(Qt.Unchecked)
            tip = "Frequency plot over the whole flight (no time cursor)" if g.kind == "fft" else ""
            it.setToolTip(tip or "Tick to add to the active panel, or drag onto any panel")
            self.list.addItem(it)
        self._updating = False

    def sync(self, panel):
        """Reflect which graphs the active panel contains."""
        self._updating = True
        self.target_label.setText(f"Active panel: {panel.title}" if panel else "No panel")
        for i in range(self.list.count()):
            it = self.list.item(i)
            on = bool(panel and panel.has_graph(it.data(ID_ROLE)))
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
        self._updating = False

    def _on_item_changed(self, it: QListWidgetItem):
        if not self._updating:
            self.toggled.emit(it.data(ID_ROLE), it.checkState() == Qt.Checked)
