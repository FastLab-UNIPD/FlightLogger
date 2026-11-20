"""Whole-flight overview strip: cursor, visible-window region, modes and events."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal

from ..log.ulog_loader import FlightLog
from ..timebus import TimeBus
from ..ui import theme
from .common import MODE_ALPHA_TIMELINE, show_preview_at, CURSOR_COLOR, TimeAxis, message_color, mode_color

EVENT_COLORS = {"armed": "#b06000", "disarmed": "#b06000", "takeoff": "#007a50", "landed": "#007a50"}


class TimelineWidget(pg.PlotWidget):
    contextRequested = Signal(object)   # global QPoint of a right click

    def __init__(self, bus: TimeBus, parent=None):
        super().__init__(parent, axisItems={"bottom": TimeAxis("bottom")})
        self.bus = bus
        self.setMinimumHeight(70)
        self.setMaximumHeight(160)
        pi = self.getPlotItem()
        pi.hideAxis("left")
        pi.setMenuEnabled(False)
        pi.hideButtons()
        self.vb = pi.getViewBox()
        self.vb.setMouseEnabled(x=False, y=False)
        self.vb.setYRange(0, 1, padding=0.02)

        self.region = pg.LinearRegionItem((0, 1), brush=pg.mkBrush(80, 120, 255, 45),
                                          pen=pg.mkPen(80, 120, 255, 160))
        self.region.setZValue(10)
        self.region.sigRegionChanged.connect(self._on_region)
        pi.addItem(self.region, ignoreBounds=True)
        self.cursor = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(CURSOR_COLOR, width=2),
                                      hoverPen=pg.mkPen(CURSOR_COLOR, width=4))
        self.cursor.setZValue(20)
        self.cursor.sigDragged.connect(lambda l: self.bus.seek(l.value()))
        pi.addItem(self.cursor, ignoreBounds=True)
        self._static: list = []
        self._note_items: list = []
        self._message_items: list = []
        self._updating = False
        self.layer_visible = {"modes": True, "events": True, "notes": True}
        self._layers: dict[str, list] = {"modes": [], "events": []}

        self.scene().sigMouseClicked.connect(self._on_click)
        self.previews = True
        self._notes: list = []
        self._messages: list = []
        self._modes: list = []
        self.scene().sigMouseMoved.connect(self._on_hover)
        bus.timeChanged.connect(self.cursor.setValue)
        bus.xRangeChanged.connect(self._on_bus_range)

    def set_log(self, log: FlightLog | None):
        pi = self.getPlotItem()
        for it in self._static:
            pi.removeItem(it)
        self._static = []
        self._layers = {"modes": [], "events": []}
        self._modes = []
        if log is None:
            return
        dur = max(log.duration, 1e-3)
        self.vb.setLimits(xMin=0, xMax=dur)
        self.vb.setXRange(0, dur, padding=0)

        for seg in log.mode_segments():
            color = mode_color(seg.nav_state, theme.mode_alpha(MODE_ALPHA_TIMELINE))
            if color is None:
                continue
            r = pg.LinearRegionItem((seg.t0, seg.t1), movable=False, brush=pg.mkBrush(*color),
                                    pen=pg.mkPen(None))
            r.setZValue(-10)
            self._modes.append(seg)
            self._add(r, "modes")
            if seg.t1 - seg.t0 > 1.0:  # Flight Review also hides labels of short modes
                txt = pg.TextItem(seg.name, color=color[:3], anchor=(0, 0))
                txt.setPos(seg.t0, 1.0)
                self._add(txt, "modes")
        key = "vehicle_local_position:0/z"
        if log.has_signal(key):
            t, z = log.get(key)
            alt = -z
            ok = np.isfinite(alt)
            rng = np.ptp(alt[ok]) if ok.any() else 0
            if rng > 0:
                y = 0.08 + 0.7 * (alt - np.nanmin(alt[ok])) / rng
                c = pg.PlotDataItem(t, y, pen=pg.mkPen(theme.plot_color("#555"), width=1))
                c.setDownsampling(auto=True, method="peak")
                self._add(c)
        for ev in log.events():
            color = EVENT_COLORS.get(ev.kind, "#888")
            self._add(pg.InfiniteLine(ev.t, angle=90, movable=False,
                                      pen=pg.mkPen(color, width=1, style=Qt.DashLine),
                                      label=ev.label, labelOpts={"position": 0.1, "color": color}),
                      "events")
        self._set_region(0, dur)

    def set_messages(self, messages):
        self._messages = list(messages)
        pi = self.getPlotItem()
        for it in self._message_items:
            pi.removeItem(it)
        self._message_items = []
        for m in messages:
            it = pg.InfiniteLine(m.t, angle=90, movable=False,
                                 pen=pg.mkPen(theme.plot_color(message_color(m.level)), width=1,
                                              style=Qt.DotLine))
            it.setZValue(3)
            pi.addItem(it, ignoreBounds=True)
            self._message_items.append(it)

    def _on_hover(self, pos):
        if not self.vb.sceneBoundingRect().contains(pos):
            return
        x = self.vb.mapSceneToView(pos).x()
        gp = self.mapToGlobal(self.mapFromScene(pos))
        if self.previews:   # the only hover on the timeline: note / message previews
            show_preview_at(self._notes, self._messages, x, 6 * self.vb.viewPixelSize()[0], gp)

    def set_notes(self, notes):
        self._notes = list(notes)
        pi = self.getPlotItem()
        for it in self._note_items:
            pi.removeItem(it)
        self._note_items = []
        for n in (n for n in notes if not n.general):   # general notes have no time
            color = pg.mkColor(n.color)
            if n.t_end is None:
                it = pg.InfiniteLine(n.t, angle=90, movable=False, pen=pg.mkPen(color, width=2),
                                     label=n.title[:24], labelOpts={
                                         "position": 0.55, "color": color,
                                         "fill": (30, 30, 30, 200) if theme.is_dark()
                                         else (255, 255, 255, 190)})
            else:
                fill = pg.mkColor(n.color)
                fill.setAlpha(70)
                it = pg.LinearRegionItem((n.t, n.t_end), movable=False, brush=fill,
                                         pen=pg.mkPen(color, width=1))
            it.setZValue(5)
            pi.addItem(it, ignoreBounds=True)
            self._note_items.append(it)
            it.setVisible(self.layer_visible["notes"])
            if n.t_end is not None and n.title:   # spans get the same label as instants
                lbl = pg.TextItem(n.title[:24], color=color, anchor=(0, 0.5),
                                  fill=(30, 30, 30, 200) if theme.is_dark() else (255, 255, 255, 190))
                lbl.setPos(n.t, 0.55)
                lbl.setZValue(6)
                pi.addItem(lbl, ignoreBounds=True)
                self._note_items.append(lbl)
                lbl.setVisible(self.layer_visible["notes"])

    def _add(self, item, layer: str | None = None):
        self.getPlotItem().addItem(item, ignoreBounds=True)
        self._static.append(item)
        if layer:
            self._layers[layer].append(item)
            item.setVisible(self.layer_visible[layer])

    def set_layer_visible(self, layer: str, on: bool):
        self.layer_visible[layer] = on
        if layer == "notes":
            for it in self._note_items:
                it.setVisible(on)
        else:
            for it in self._layers.get(layer, []):
                it.setVisible(on)

    def _set_region(self, x0, x1):
        self._updating = True
        try:
            self.region.setRegion((x0, x1))
        finally:
            self._updating = False

    def _on_region(self):
        if not self._updating:
            x0, x1 = self.region.getRegion()
            self.bus.set_x_range(x0, x1, self)

    def _on_bus_range(self, x0, x1, source):
        if source is not self:
            self._set_region(x0, x1)

    def _on_click(self, ev):
        if ev.button() == Qt.RightButton:
            self.contextRequested.emit(ev.screenPos().toPoint())
            ev.accept()
            return
        if ev.button() == Qt.LeftButton and not ev.double():
            self.bus.seek(self.vb.mapSceneToView(ev.scenePos()).x())
