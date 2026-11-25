"""Flight Review's position plot (local frame, East vs North) with a live marker."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QStackedLayout, QWidget

from ..log.ulog_loader import FlightLog
from ..plots.common import CURSOR_COLOR
from ..timebus import TimeBus
from . import theme


class TrackView(QWidget):
    def __init__(self, bus: TimeBus, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.plot = pg.PlotWidget()
        pi = self.plot.getPlotItem()
        pi.setAspectLocked(True)
        pi.showGrid(x=True, y=True, alpha=0.2)
        pi.setLabel("bottom", "East [m]")
        pi.setLabel("left", "North [m]")
        for name in ("left", "bottom"):
            pi.getAxis(name).enableAutoSIPrefix(False)
        title = {"size": "9pt"}
        if theme.title_color():
            title["color"] = theme.title_color()
        pi.setTitle("Position (local frame)", **title)
        self.legend = pi.addLegend(offset=(-6, 4), labelTextSize="8pt", **theme.legend_style())
        self.marker = pg.ScatterPlotItem(size=11, brush=pg.mkBrush(CURSOR_COLOR),
                                         pen=pg.mkPen("w", width=1.5))
        self.marker.setZValue(100)
        self.empty = QLabel("No local position in this log", alignment=Qt.AlignCenter)
        theme.style_role(self.empty, "dim", "color: gray;")
        self.stack = QStackedLayout(self)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.plot)
        self._track = None
        self._items: list = []
        bus.timeChanged.connect(self._on_time)
        bus.trimChanged.connect(self._on_trim)

    def set_log(self, log: FlightLog | None):
        pi = self.plot.getPlotItem()
        for it in self._items:
            pi.removeItem(it)
        self._items = []
        self._track = log.track if log else None
        if self._track is None:
            self.stack.setCurrentWidget(self.empty)
            return
        for s in self._track.series:
            item = pg.PlotDataItem(s.t, s.y, pen=pg.mkPen(theme.plot_color(s.color), width=2),
                                   name=s.label)
            pi.addItem(item)
            self._items.append(item)
        pi.addItem(self.marker)
        self._items.append(self.marker)
        self._on_trim(self.bus.trim)
        pi.autoRange()
        self.stack.setCurrentWidget(self.plot)
        self._on_time(self.bus.t)

    def _on_trim(self, trim):
        """The estimated track (the series sampled at tr.t) shows only the visible part."""
        tr = self._track
        if tr is None or not self._items:
            return
        s = tr.series[0]
        item = self._items[0]
        if trim and len(tr.t) == len(s.t):
            i0 = int(np.searchsorted(tr.t, trim[0], "left"))
            i1 = int(np.searchsorted(tr.t, trim[1], "right"))
            item.setData(s.t[i0:i1], s.y[i0:i1])
        else:
            item.setData(s.t, s.y)
        self.plot.getPlotItem().autoRange()

    def _on_time(self, t: float):
        tr = self._track
        if tr is None or not len(tr.t):
            return
        i = int(np.clip(np.searchsorted(tr.t, t), 0, len(tr.t) - 1))
        self.marker.setData([tr.east[i]], [tr.north[i]])
