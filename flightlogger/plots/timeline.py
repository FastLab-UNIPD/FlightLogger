"""Whole-flight overview strip: cursor, visible-window region, modes and events."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QToolButton

from ..log.ulog_loader import FlightLog
from ..timebus import TimeBus
from ..ui import icons, theme
from .common import MODE_ALPHA_TIMELINE, show_preview_at, CURSOR_COLOR, TimeAxis, message_color, mode_color

EVENT_COLORS = {"armed": "#b06000", "disarmed": "#b06000", "takeoff": "#007a50", "landed": "#007a50"}


class TimelineWidget(pg.PlotWidget):
    contextRequested = Signal(object)   # global QPoint of a right click
    lockChanged = Signal(bool)          # True = locked (whole flight), False = zoom/pan

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
        self._note_map: dict = {}   # note id -> mark, label, signature, title
        self._message_items: list = []
        self._updating = False
        self.layer_visible = {"modes": True, "events": True, "notes": True}
        self._layers: dict[str, list] = {"modes": [], "events": []}

        self.scene().sigMouseClicked.connect(self._on_click)
        self.vb.sigResized.connect(self._stagger)
        self.vb.sigXRangeChanged.connect(self._stagger)
        self.previews = True
        self._notes: list = []
        self._messages: list = []
        self._modes: list = []
        self.scene().sigMouseMoved.connect(self._on_hover)
        bus.timeChanged.connect(self.cursor.setValue)
        bus.xRangeChanged.connect(self._on_bus_range)
        bus.trimChanged.connect(self._on_trim)
        self._trim_items: list = []

        self.locked = True
        self.lock_btn = QToolButton(self)
        self.lock_btn.setCheckable(True)
        self.lock_btn.setChecked(True)
        self.lock_btn.setAutoRaise(True)
        self.lock_btn.setFixedSize(26, 26)
        self.lock_btn.setIconSize(QSize(19, 19))
        self.lock_btn.setCursor(Qt.ArrowCursor)
        self.lock_btn.setToolTip("Lock: timeline shows the whole flight / "
                                 "Unlocked: wheel to zoom, drag to pan")
        self.lock_btn.setIcon(icons.icon("lock"))
        self.style_lock_btn()
        self.lock_btn.toggled.connect(self.set_locked)
        self._place_lock_btn()

    # --- lock / unlock (zoom and pan) ---
    def style_lock_btn(self):
        """Semi-opaque backing so the icon reads over plot content (call again after a theme switch)."""
        from ..ui import theme
        dark = theme.is_dark()
        bg, hov, edge = (("rgba(30,30,30,200)", "rgba(70,70,70,230)", "rgba(200,188,242,150)") if dark else
                         ("rgba(255,255,255,215)", "rgba(235,232,248,240)", "rgba(80,60,160,150)"))
        self.lock_btn.setStyleSheet(
            f"QToolButton{{background:{bg};border:1px solid {edge};border-radius:5px;padding:0;}}"
            f"QToolButton:hover{{background:{hov};}}")
        self.lock_btn.setIcon(icons.icon("lock" if self.locked else "unlock"))

    def _place_lock_btn(self):
        self.lock_btn.move(max(0, self.width() - self.lock_btn.width() - 4), 3)
        self.lock_btn.raise_()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if "lock_btn" in self.__dict__:      # resizeEvent(None) runs during pyqtgraph's init
            self._place_lock_btn()

    def _limits(self) -> tuple[float, float]:
        dur = max(self.bus.duration, 1e-3)
        t = self.bus.trim
        return (t[0], t[1]) if t else (0.0, dur)

    def set_locked(self, on: bool):
        on = bool(on)
        if self.lock_btn.isChecked() != on:
            self.lock_btn.setChecked(on)   # re-enters through toggled
            return
        changed = on != self.locked
        self.locked = on
        self.style_lock_btn()
        self.vb.setMouseEnabled(x=not on, y=False)
        dur = max(self.bus.duration, 1e-3)
        if on:
            self.vb.setLimits(xMin=0, xMax=dur)
            self.vb.setXRange(0, dur, padding=0)
        else:
            lo, hi = self._limits()
            self.vb.setLimits(xMin=lo, xMax=hi)
            self.vb.setXRange(lo, hi, padding=0)
        if changed:
            self.lockChanged.emit(on)

    def _on_trim(self, trim):
        """Hidden parts: grey shading; the blue region cannot leave the visible part."""
        pi = self.getPlotItem()
        for it in self._trim_items:
            pi.removeItem(it)
        self._trim_items = []
        self.region.setBounds(trim if trim else ((0.0, self.bus.duration) if self.bus.duration > 0
                                                 else (None, None)))
        if not self.locked and self.bus.duration > 0:
            lo, hi = self._limits()
            self.vb.setLimits(xMin=lo, xMax=hi)
            self.vb.setXRange(lo, hi, padding=0)
        if not trim:
            return
        dur = max(self.bus.duration, trim[1])
        grey = pg.mkBrush(200, 200, 200, 80) if theme.is_dark() else pg.mkBrush(90, 90, 90, 100)
        for a, b in ((0.0, trim[0]), (trim[1], dur)):
            if b - a > 0:
                r = pg.LinearRegionItem((a, b), movable=False, brush=grey, pen=pg.mkPen(None))
                r.setZValue(8)
                pi.addItem(r, ignoreBounds=True)
                self._trim_items.append(r)

    def set_log(self, log: FlightLog | None):
        pi = self.getPlotItem()
        for it in self._static:
            pi.removeItem(it)
        self._static = []
        self._layers = {"modes": [], "events": []}
        self._modes = []
        self._on_trim(self.bus.trim if log is not None else None)
        if log is None:
            return
        dur = max(log.duration, 1e-3)
        lo, hi = (0.0, dur) if self.locked else self._limits()
        self.vb.setLimits(xMin=lo, xMax=hi)
        self.vb.setXRange(lo, hi, padding=0)

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

    MAX_NOTE_ROWS = 6

    def set_notes(self, notes):
        """Span bands / instant lines with a title label each. Items are updated in place (a
        title edit only changes its label text) and labels are staggered into rows."""
        self._notes = list(notes)
        pi = self.getPlotItem()
        wanted = {n.id: n for n in notes if not n.general}   # general notes have no time
        for nid, entry in list(self._note_map.items()):
            n = wanted.get(nid)
            if n is None or entry["sig"] != (n.t, n.t_end, n.color):
                pi.removeItem(entry["mark"])
                pi.removeItem(entry["label"])
                del self._note_map[nid]
        for nid, n in wanted.items():
            entry = self._note_map.get(nid)
            if entry is None:
                color = pg.mkColor(n.color)
                if n.t_end is None:
                    mark = pg.InfiniteLine(n.t, angle=90, movable=False, pen=pg.mkPen(color, width=2))
                else:
                    fill = pg.mkColor(n.color)
                    fill.setAlpha(70)
                    mark = pg.LinearRegionItem((n.t, n.t_end), movable=False, brush=fill,
                                               pen=pg.mkPen(color, width=1))
                mark.setZValue(5)
                pi.addItem(mark, ignoreBounds=True)
                label = pg.TextItem("", color=color, anchor=(0, 0.5),
                                    fill=(30, 30, 30, 200) if theme.is_dark() else (255, 255, 255, 190))
                font = label.textItem.font()          # compact, so more rows fit the strip
                font.setPointSizeF(8)
                label.setFont(font)
                label.textItem.document().setDocumentMargin(1)
                label.setZValue(6)
                pi.addItem(label, ignoreBounds=True)
                entry = self._note_map[nid] = {"sig": (n.t, n.t_end, n.color), "mark": mark,
                                               "label": label, "title": None}
            if entry["title"] != n.title[:24]:
                entry["title"] = n.title[:24]
                entry["label"].setText(entry["title"])
        self._note_items = []
        for entry in self._note_map.values():
            self._note_items += [entry["mark"], entry["label"]]
        self._show_notes()
        self._stagger()

    def _show_notes(self):
        on = self.layer_visible["notes"]
        for entry in self._note_map.values():
            entry["mark"].setVisible(on)
            entry["label"].setVisible(on and bool(entry["title"]))   # no empty label boxes

    def _stagger(self, *_):
        """Give each note label a row (one label height apart) where it does not cover an earlier
        label; the taller the timeline, the more rows."""
        if not self._note_map:
            return
        px, py = self.vb.viewPixelSize()          # data units per pixel
        if not (px and py and np.isfinite(px) and np.isfinite(py)):
            return
        entries = sorted(self._note_map.values(), key=lambda e: e["sig"][0])
        label_h = max(e["label"].textItem.boundingRect().height() for e in entries) or 16
        n = max(1, min(self.MAX_NOTE_ROWS, int(self.vb.height() / label_h)))
        low, high = self.vb.viewRange()[1]
        top = (low + high) / 2 + n * label_h * py / 2          # rows centered in the strip
        rows = [top - (i + 0.5) * label_h * py for i in range(n)]
        busy_until = [-np.inf] * n
        for e in entries:
            t = e["sig"][0]
            end = t + (e["label"].textItem.boundingRect().width() + 3) * px
            free = [i for i, u in enumerate(busy_until) if u <= t]
            i = free[0] if free else int(np.argmin(busy_until))   # all taken: the one ending first
            busy_until[i] = end
            e["label"].setPos(t, rows[i])

    def _add(self, item, layer: str | None = None):
        self.getPlotItem().addItem(item, ignoreBounds=True)
        self._static.append(item)
        if layer:
            self._layers[layer].append(item)
            item.setVisible(self.layer_visible[layer])

    def set_layer_visible(self, layer: str, on: bool):
        self.layer_visible[layer] = on
        if layer == "notes":
            self._show_notes()
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
            if self.bus.x_range and (abs(self.bus.x_range[0] - x0) > 1e-9
                                     or abs(self.bus.x_range[1] - x1) > 1e-9):
                self._set_region(*self.bus.x_range)   # the bus clamped it to the trim

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
