"""A dockable panel holding a vertical stack of plots.

A plot is either a Flight Review graph (drawn from a ``GraphDef``) or a custom
plot of raw signals; Flight Review graphs can also get extra signals dropped on
them. Every time-based plot shows the red cursor at the TimeBus instant. Plots
in a panel share their X range; panels with "Link X" on also share it through
the TimeBus. FFT plots have a frequency axis and stay independent.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import shiboken6
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QMimeData, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QDrag, QMouseEvent
from PySide6.QtWidgets import (QApplication, QComboBox, QGraphicsItem, QGraphicsRectItem, QHBoxLayout, QLabel, QMenu, QScrollArea,
                               QToolButton, QVBoxLayout, QWidget)

from ..log.fr_graphs import GraphDef
from ..log.ulog_loader import FlightLog, SignalKey
from ..timebus import TimeBus
from ..ui import icons, theme
from . import line_refs
from .plot_edit import apply_series_state, series_state
from .common import (CURSOR_COLOR, show_preview_at, MIME_GRAPHS, MIME_LINE, MIME_SIGNALS, PALETTE, TimeAxis, fmt_time,
                     message_color, mode_color)

_highlighted = None


def _highlight(plot) -> None:
    """Thin solid lavender frame around the whole last-clicked plot (shaped like pyqtgraph's export
    selection); one at a time, across panels."""
    global _highlighted
    if _highlighted is not None and _highlighted is not plot:
        _highlighted.set_frame(False)
    plot.set_frame(True)
    _highlighted = plot


def highlighted_plot():
    return _highlighted


def _clear_highlight(plot) -> None:
    global _highlighted
    try:
        plot.set_frame(False)
    except RuntimeError:
        pass
    if _highlighted is plot:
        _highlighted = None


PLOT_HEIGHTS = {"S": 130, "M": 210, "L": 320}
FOLLOW_LEAD = 0.1  # when paging, put the cursor 10% from the left edge
LINE_WIDTH = 1.5
_BIG = 1e12
_VIRIDIS = pg.colormap.get("viridis")


def _cache(item):
    """Pixmap-cache static layers so moving the cursor only blits them."""
    if isinstance(item, pg.PlotDataItem):
        item.curve.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        item.scatter.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
    else:
        item.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
    return item


class _XWheelViewBox(pg.ViewBox):
    """The wheel zooms X only, even when Y mouse interaction is on (box zoom)."""

    def wheelEvent(self, ev, axis=None):
        super().wheelEvent(ev, 0 if axis is None else axis)


def _pick(options: list, pos=None):
    """Small pop-up menu: options = [(text, value)]; the chosen value or None. Tests replace this."""
    menu = QMenu()
    acts = {menu.addAction(text): value for text, value in options}
    chosen = menu.exec(pos or QCursor.pos())
    return acts.get(chosen)


class _DragLegend(pg.LegendItem):
    """Legend whose entries can be dragged onto another plot (copies that line there). Dragging
    inside the plot still moves the legend; the entry leaves as a drag once the pointer is outside."""

    def __init__(self, plot, *a, **k):
        super().__init__(*a, **k)
        self._plot = plot
        self._entry = None
        self._pos0 = None

    def entry_at(self, scene_pos):
        for sample, label in self.items:
            if sample.sceneBoundingRect().united(label.sceneBoundingRect()).contains(scene_pos):
                return sample.item
        return None

    def mouseDragEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        ev.accept()
        if ev.isStart():
            self._entry = self.entry_at(ev.buttonDownScenePos())
            self._pos0 = self.pos()
        if self._entry is not None and not self._plot.vb.sceneBoundingRect().contains(ev.scenePos()):
            item, self._entry = self._entry, None
            self.autoAnchor(self._pos0)
            ref = self._plot.ref_of_item(item)
            view = self.getViewWidget()
            if ref is not None and view is not None:
                QTimer.singleShot(0, lambda: self._start_drag(view, ref))
            return
        if ev.isFinish():
            self._entry = None
        super().mouseDragEvent(ev)

    @staticmethod
    def _start_drag(view, ref):
        md = QMimeData()
        md.setData(MIME_LINE, json.dumps(ref).encode())
        drag = QDrag(view)
        drag.setMimeData(md)
        drag.exec(Qt.CopyAction)
        # Qt swallowed the button release: tell the scene the press is over
        local = QPointF(view.mapFromGlobal(QCursor.pos()))
        QApplication.sendEvent(view.viewport(), QMouseEvent(QEvent.MouseButtonRelease, local, Qt.LeftButton,
                                                            Qt.NoButton, Qt.NoModifier))


class GraphPlot:
    """One PlotItem: a Flight Review graph and/or custom signals, plus the cursor."""

    def __init__(self, panel: "GraphPanel", title: str = "", graph: GraphDef | None = None):
        self.panel = panel
        self.graph = graph
        self._frame = None
        self.title = graph.title if graph else title
        self.time_based = graph.time_based if graph else True
        axis = {"bottom": TimeAxis("bottom")} if self.time_based else {}
        self.item = pg.PlotItem(axisItems=axis, viewBox=_XWheelViewBox())
        self.item.showGrid(x=not self.time_based, y=True, alpha=0.2)
        self.item.setDownsampling(auto=True, mode="peak")
        self.item.setClipToView(True)
        self.item.getAxis("left").setWidth(60)
        for name in ("left", "bottom"):
            self.item.getAxis(name).enableAutoSIPrefix(False)
        self._title_opts = {"size": "9pt"}
        if theme.title_color():
            self._title_opts["color"] = theme.title_color()
        self.item.setTitle(self.title, **self._title_opts)
        self.vb: pg.ViewBox = self.item.getViewBox()
        self.vb.setMouseEnabled(x=True, y=False)
        self.vb.enableAutoRange(axis="y")
        self.vb.setAutoVisible(y=True)
        if self.time_based:
            self.vb.disableAutoRange(axis="x")
        self.legend = _DragLegend(self, offset=(-6, 4), labelTextSize="8pt", **theme.legend_style())
        self.legend.setParentItem(self.vb)
        self.item.legend = self.legend
        self.invert_y = False
        self.cursor = None
        if self.time_based:
            self.cursor = pg.InfiniteLine(angle=90, movable=True,
                                          pen=pg.mkPen(CURSOR_COLOR, width=2),
                                          hoverPen=pg.mkPen(CURSOR_COLOR, width=4))
            self.cursor.setZValue(1000)
            self.item.addItem(self.cursor, ignoreBounds=True)
        self.signals: list[str] = []          # custom signals (keys)
        self.overlays: list[dict] = []        # pasted lines: {"ref", "axis", "item", "name", "units"}
        self._unresolved: list = []           # (position, saved dict) of pasted lines this log lacks
        self.right = None                     # RightAxis, while a line sits on the right axis
        self.right_settings: dict = {"mode": "auto"}
        self.series_items: list = []          # (name, PlotDataItem) of a Flight Review graph
        self.styles: dict[str, dict] = {}     # user edits, by series name -> look
        self.curves: dict[str, pg.PlotDataItem] = {}
        self._full: dict = {}                 # time-based item -> (t, y), whole flight (for the trim)
        self._trim = None
        self.mode_items: list = []
        self.note_items: list = []
        self._note_map: dict = {}   # note id -> ((t, t_end, color), item)
        self.message_items: list = []
        self._n_legend = 0
        self.stale = True
        if graph is not None:
            self._draw_graph(graph)
        for a in self.item.axes.values():
            _cache(a["item"])
        self._build_menu()

    # --- Flight Review graph ---
    def _draw_graph(self, g: GraphDef):
        pi = self.item
        if g.y_label:
            pi.setLabel("left", g.y_label)
        if g.x_label:
            pi.setLabel("bottom", g.x_label)
        for s in g.series:
            color = theme.plot_color(s.color)
            if s.scatter:
                item = pg.PlotDataItem(s.t, s.y, pen=None, symbol="o", symbolSize=5,
                                       symbolPen=pg.mkPen(color, width=1.5), symbolBrush=None,
                                       name=s.label or None)
            else:
                item = pg.PlotDataItem(s.t, s.y, pen=pg.mkPen(color, width=LINE_WIDTH),
                                       stepMode="left" if s.step else None,
                                       name=s.label or None)
            pi.addItem(_cache(item))
            if self.time_based:
                self._full[item] = (s.t, s.y)
            self.series_items.append((s.label or f"Series {len(self.series_items) + 1}", item))
            self._n_legend += bool(s.label)
        if g.spectrogram is not None:
            sp = g.spectrogram
            img = pg.ImageItem(sp.image.T)
            img.setColorMap(_VIRIDIS)
            lo, hi = float(np.min(sp.image)), float(np.max(sp.image))
            img.setLevels((lo, hi))
            img.setRect(QRectF(sp.t0, sp.f0, sp.t1 - sp.t0, sp.f1 - sp.f0))
            img.setZValue(-200)
            pi.addItem(_cache(img))
            pi.setTitle(f"{self.title}  ({lo:.0f} … {hi:.0f} dB)", **self._title_opts)
        if g.y_range is not None:
            self.vb.disableAutoRange(axis="y")
            self.vb.setYRange(*g.y_range, padding=0)
        if g.y_start is not None and g.series:
            t = np.concatenate([s.t for s in g.series if len(s.t)])
            if len(t):
                xs = np.linspace(np.nanmin(t), np.nanmax(t), 400)
                pi.addItem(pg.PlotDataItem(xs, np.full_like(xs, g.y_start),
                                           pen=pg.mkPen(0, 0, 0, 0)))
        for bottom, top, color in g.hbands:
            c = pg.mkColor(color)
            c.setAlpha(23)
            r = pg.LinearRegionItem((-_BIG if bottom is None else bottom,
                                     _BIG if top is None else top),
                                    orientation="horizontal", movable=False, brush=c,
                                    pen=pg.mkPen(None))
            r.setZValue(-90)
            pi.addItem(_cache(r), ignoreBounds=True)
        for y, color, *faint in g.hlines:   # optional 3rd item: faint dotted style
            c = pg.mkColor(theme.plot_color(color))
            c.setAlpha(70 if faint else 128)
            pen = pg.mkPen(c, width=1, style=Qt.DotLine if faint else Qt.SolidLine)
            pi.addItem(pg.InfiniteLine(y, angle=0, pen=pen), ignoreBounds=True)
        for f, label in g.vmarks:
            ink = theme.plot_color("k")
            pi.addItem(pg.InfiniteLine(f, angle=90, pen=pg.mkPen(ink, width=1), label=" " + label,
                                       labelOpts={"position": 0.5, "color": ink,
                                                  "anchors": [(0, 0.5), (0, 0.5)]}),
                       ignoreBounds=True)
        for t in g.nan_times:
            ink = theme.plot_color("k")
            pi.addItem(pg.InfiniteLine(t, angle=90, pen=pg.mkPen(ink, width=2, style=Qt.DashLine),
                                       label=" NaN", labelOpts={"position": 0.1, "color": ink,
                                                                "anchors": [(0, 1), (0, 1)]}),
                       ignoreBounds=True)
        def shade(alpha):  # translucent black on white plots, white on dark ones
            return (255, 255, 255, int(alpha * 0.8)) if theme.is_dark() else (0, 0, 0, alpha)

        if g.dropouts:
            d = np.array(g.dropouts)
            pi.addItem(pg.BarGraphItem(x0=d[:, 0], x1=d[:, 1], y0=d[:, 2], y1=d[:, 3],
                                       brush=pg.mkBrush(*shade(38)), pen=pg.mkPen(*shade(76))),
                       ignoreBounds=True)
        self._update_legend()

    # --- custom signals ---
    def add_signal(self, key: str) -> bool:
        log = self.panel.log
        if not self.time_based or key in self.curves or log is None or not log.has_signal(key):
            return False
        t, y = log.get(key)
        color = theme.plot_color(PALETTE[len(self.signals) % len(PALETTE)])
        curve = self.item.plot(t, y, pen=pg.mkPen(color, width=LINE_WIDTH),
                               name=SignalKey.parse(key).label)
        _cache(curve)
        self.curves[key] = curve
        self._full[curve] = (t, y)
        if self._trim:
            self._slice(curve, *self._trim)
        self.signals.append(key)
        self._n_legend += 1
        self._update_legend()
        return True

    def remove_signal(self, key: str):
        curve = self.curves.pop(key, None)
        if curve is not None:
            self._full.pop(curve, None)
            self.item.removeItem(curve)
            self.signals.remove(key)
            self._n_legend -= 1
            self._update_legend()

    # --- hidden time range ---
    def _slice(self, item, t0: float, t1: float):
        t, y = self._full[item]
        i0 = int(np.searchsorted(t, t0, "left"))
        i1 = int(np.searchsorted(t, t1, "right"))
        item.setData(t[i0:i1], y[i0:i1])

    def set_trim(self, trim):
        """Show only data inside trim = (t0, t1) (None: the whole flight). Full arrays are kept;
        outside data is neither drawn nor used by the Y autoscale. Non-time plots are unchanged."""
        if not self.time_based or trim == self._trim:
            return
        self._trim = trim
        if trim:
            self.vb.setLimits(xMin=trim[0], xMax=trim[1])
            for item in self._full:
                self._slice(item, *trim)
        else:
            self.vb.setLimits(xMin=None, xMax=None)
            for item, (t, y) in self._full.items():
                item.setData(t, y)

    def _update_legend(self):
        self.legend.setColumnCount(2 if self._n_legend > 4 else 1)
        self.legend.setVisible(self._n_legend > 0)

    def set_notes(self, notes):
        """Timeline notes: dashed line (instant) or shaded band (span).

        Only markers whose time or color changed are redrawn: rebuilding every marker on every
        plot at each keystroke in a note made typing lag by about a second."""
        if not self.time_based:
            return
        wanted = {n.id: (n.t, n.t_end, n.color) for n in notes if not n.general}   # general: no time
        for nid, (sig, item) in list(self._note_map.items()):
            if wanted.get(nid) != sig:
                self.item.removeItem(item)
                del self._note_map[nid]
        for nid, sig in wanted.items():
            if nid in self._note_map:
                continue
            t, t_end, name = sig
            color = pg.mkColor(name)
            line_c = pg.mkColor(name)
            line_c.setAlpha(153)         # ~60%: markers must not hide the data
            if t_end is None:
                it = pg.InfiniteLine(t, angle=90, movable=False,
                                     pen=pg.mkPen(line_c, width=1.5, style=Qt.DashLine))
            else:
                fill = pg.mkColor(name)
                fill.setAlpha(30)
                it = pg.LinearRegionItem((t, t_end), movable=False, brush=fill,
                                         pen=pg.mkPen(line_c, width=1))
            it.setZValue(-50)
            self.item.addItem(it, ignoreBounds=True)
            self._note_map[nid] = (sig, it)
        self.note_items = [item for _, item in self._note_map.values()]

    def set_messages(self, messages):
        """PX4 logged messages (warnings/errors) as thin dotted lines."""
        for it in self.message_items:
            self.item.removeItem(it)
        self.message_items = []
        if not self.time_based:
            return
        for m in messages:
            it = pg.InfiniteLine(m.t, angle=90, movable=False,
                                 pen=pg.mkPen(theme.plot_color(message_color(m.level)), width=1,
                                              style=Qt.DotLine))
            it.setZValue(-60)
            self.item.addItem(it, ignoreBounds=True)
            self.message_items.append(it)

    def set_modes(self, segments):
        for it in self.mode_items:
            self.item.removeItem(it)
        self.mode_items = []
        if not self.time_based or (self.graph is not None and not self.graph.mode_background):
            return
        for seg in segments:
            color = mode_color(seg.nav_state, theme.mode_alpha(23))
            if color is None:
                continue
            r = pg.LinearRegionItem((seg.t0, seg.t1), movable=False, brush=pg.mkBrush(*color),
                                    pen=pg.mkPen(None))
            r.setZValue(-100)
            self.item.addItem(_cache(r), ignoreBounds=True)
            self.mode_items.append(r)

    def set_param_changes(self, changes):
        """Parameter changes during the flight as labels (Flight Review does this too)."""
        if not self.time_based:
            return
        for i, (t, name, value) in enumerate(changes):
            txt = f"• {name}: {value}" if isinstance(value, int) else f"• {name}: {value:.2f}"
            line = pg.InfiniteLine(t, angle=90, pen=pg.mkPen(120, 120, 120, 90, style=Qt.DotLine),
                                   label=txt, labelOpts={"position": 0.95 - (i % 4) * 0.07,
                                                         "color": (60, 60, 60)})
            self.item.addItem(line, ignoreBounds=True)

    def set_frame(self, on: bool):
        if self._frame is not None and not shiboken6.isValid(self._frame):
            self._frame = None   # its scene is gone (panel deleted)
            if not on:
                return
        if self._frame is None:
            if not on:
                return
            self._frame = QGraphicsRectItem(self.item)
            self._frame.setPen(pg.mkPen(theme.ACCENT, width=1))
            self._frame.setBrush(Qt.NoBrush)
            self._frame.setZValue(1000)
            self._frame.setAcceptedMouseButtons(Qt.NoButton)
            self.item.geometryChanged.connect(self._fit_frame)
        self._frame.setVisible(on)
        if on:
            self._fit_frame()

    def _fit_frame(self):
        if self._frame is not None:
            self._frame.setRect(self.item.boundingRect())

    # --- editing ---
    def editable_series(self):
        out = list(self.series_items)
        for key, curve in self.curves.items():
            out.append((SignalKey.parse(key).label, curve))
        out.extend((ov["name"], ov["item"]) for ov in self.overlays)
        return out

    # --- pasted lines (overlays), left or right axis ---
    def line_refs(self) -> list:
        """Every line of the plot as (name, reference, item): what "Copy line" offers."""
        if not self.time_based:
            return []
        out = []
        if self.graph is not None:
            for name, item in self.series_items:
                out.append((name, line_refs.make_ref(graph=self.graph.id, series=name), item))
        for key, curve in self.curves.items():
            out.append((SignalKey.parse(key).label, line_refs.make_ref(signal=key), curve))
        for ov in self.overlays:
            out.append((ov["name"], dict(ov["ref"]), ov["item"]))
        return out

    def ref_of_item(self, item):
        return next((ref for _n, ref, it in self.line_refs() if it is item), None)

    def _right_ids(self) -> set:
        return {id(ov["item"]) for ov in self.overlays if ov["axis"] == "right"}

    def left_line_names(self) -> list[str]:
        right = self._right_ids()
        return [n for n, it in self.editable_series() if id(it) not in right]

    def right_arrays(self) -> list:
        return [self._full[ov["item"]] for ov in self.overlays if ov["axis"] == "right"]

    def left_arrays(self, name) -> list:
        right = self._right_ids()
        return [self._full[it] for n, it in self.editable_series()
                if n == name and id(it) not in right and it in self._full]

    def _unique_name(self, base: str) -> str:
        names = {n for n, _ in self.editable_series()}
        name, i = base, 2
        while name in names:
            name, i = f"{base} #{i}", i + 1
        return name

    def add_overlay(self, ref: dict, axis: str = "left", style: dict | None = None,
                    keep: bool = False) -> bool:
        """Draw a line from a reference (see line_refs) on the left or right axis. False when it
        cannot be (no such line in this log, not a time plot, already there)."""
        axis = "right" if axis == "right" else "left"
        ref = line_refs.norm_ref(ref)
        if ref is None or not self.time_based:
            return False
        line = line_refs.resolve(ref, self.panel.log)
        if line is None:
            if keep:   # not in this log: not drawn, but saved again as it was (retried on the next log)
                self._unresolved.append((len(self.overlays) + len(self._unresolved),
                                         dict(ref, axis=axis, **({"style": style} if style else {}))))
            return False
        rid = line_refs.ref_id(ref)
        if any(line_refs.ref_id(ov["ref"]) == rid and ov["axis"] == axis for ov in self.overlays):
            return False
        pasted = {id(ov["item"]) for ov in self.overlays}
        if axis == "left" and any(line_refs.ref_id(r) == rid for _n, r, it in self.line_refs()
                                  if id(it) not in pasted):
            return False   # already one of this plot's own lines
        used = {pg.mkColor(series_state(it)["color"]).name() for _n, it in self.editable_series()}
        color = theme.plot_color(line.color) if line.color else None
        if color is None or pg.mkColor(color).name() in used:
            free = [c for c in PALETTE if pg.mkColor(theme.plot_color(c)).name() not in used]
            color = theme.plot_color(free[0] if free else PALETTE[len(self.overlays) % len(PALETTE)])
        name = self._unique_name(line.label + (" (R)" if axis == "right" else ""))
        if line.scatter:
            item = pg.PlotDataItem(line.t, line.y, pen=None, symbol="o", symbolSize=5,
                                   symbolPen=pg.mkPen(color, width=1.5), symbolBrush=None, name=name)
        else:
            item = pg.PlotDataItem(line.t, line.y, pen=pg.mkPen(color, width=LINE_WIDTH),
                                   stepMode="left" if line.step else None, name=name)
        _cache(item)
        if axis == "right":
            if self.right is None:
                from .dual_axis import RightAxis
                self.right = RightAxis(self)
                self.right.set_settings(self.right_settings)
            self.right.add_item(item)
            self.legend.addItem(item, name)
        else:
            self.item.addItem(item)
        self._full[item] = (line.t, line.y)
        if self._trim:
            self._slice(item, *self._trim)
        self.overlays.append({"ref": ref, "axis": axis, "item": item, "name": name, "units": line.units})
        if style:
            apply_series_state(item, style)
        self._n_legend += 1
        self._update_legend()
        self._update_right()
        return True

    def remove_overlay(self, ov: dict):
        if ov not in self.overlays:
            return
        self.overlays.remove(ov)
        item = ov["item"]
        self._full.pop(item, None)
        if ov["axis"] == "right":
            self.right.remove_item(item)
            self.legend.removeItem(item)
        else:
            self.item.removeItem(item)
        self._n_legend -= 1
        self._update_legend()
        self._update_right()

    def _update_right(self):
        """After lines came or went on the right: label, refresh, or drop the axis when empty."""
        rights = [ov for ov in self.overlays if ov["axis"] == "right"]
        if not rights:
            if self.right is not None:
                self.right.close()
                self.right = None
                self.right_settings = {"mode": "auto"}
            return
        units = {ov["units"] for ov in rights if ov["units"]}
        self.right.set_label(units.pop() if len(units) == 1 else "")
        self.right.refresh()

    def set_right_settings(self, s: dict, emit: bool = True):
        self.right_settings = line_refs.clean_settings(s)
        if self.right is not None:
            self.right.set_settings(self.right_settings)
        if emit:
            self.panel.changed.emit()

    def set_right_mode(self, mode: str):
        s = dict(self.right_settings, mode=mode)
        rng = self.right.range if self.right is not None else None
        if mode == "manual" and rng and s.get("min") is None:
            s["min"], s["max"] = rng
        if mode == "match" and not s.get("match"):
            names = self.left_line_names()
            s["match"] = names[0] if names else None
        self.set_right_settings(s)

    def paste_line(self, axis: str = "left", ref: dict | None = None) -> bool:
        ref = ref or line_refs.clipboard.get()
        if ref is None or not self.add_overlay(ref, axis):
            return False
        self.panel.changed.emit()
        return True

    def copy_line(self, ref: dict):
        line_refs.clipboard.set(ref)

    def copy_via_menu(self, pos=None):
        """Ctrl+C: the plot's line (a menu to pick when there are several) goes to the clipboard."""
        refs = self.line_refs()
        if not refs:
            return None
        ref = refs[0][1] if len(refs) == 1 else _pick([(n, r) for n, r, _ in refs], pos)
        if ref is not None:
            self.copy_line(ref)
        return ref

    def paste_via_menu(self, pos=None) -> bool:
        """Ctrl+V: paste the clipboard line on this plot, asking left or right axis."""
        if line_refs.clipboard.get() is None or not self.time_based:
            return False
        axis = _pick([("Left axis", "left"), ("Right axis", "right")], pos)
        return axis is not None and self.paste_line(axis)

    def set_box_zoom(self, on: bool):
        """On: dragging draws a rectangle that zooms X and Y (the wheel stays X-only)."""
        self.vb.setMouseMode(pg.ViewBox.RectMode if on else pg.ViewBox.PanMode)
        self.vb.setMouseEnabled(x=True, y=bool(on))

    def auto_y(self):
        """Back to automatic Y scaling (graphs with a fixed Y range get that range again)."""
        if self.graph is not None and self.graph.y_range is not None:
            self.vb.disableAutoRange(axis="y")
            self.vb.setYRange(*self.graph.y_range, padding=0)
        else:
            self.vb.enableAutoRange(axis="y")
        if self.right is not None:
            self.right.refresh()

    def set_invert_y(self, on: bool):
        self.invert_y = bool(on)
        self.vb.invertY(self.invert_y)
        if self.right is not None:
            self.right.set_invert_y(self.invert_y)
        self._flip_act.blockSignals(True)
        self._flip_act.setChecked(self.invert_y)
        self._flip_act.blockSignals(False)

    def _flip_toggled(self, on: bool):
        self.set_invert_y(on)
        self.panel.changed.emit()

    def set_title(self, title: str):
        self.title = title
        self.item.setTitle(title, **self._title_opts)

    def style_changed(self):
        self.styles = {name: series_state(it) for name, it in self.editable_series()}
        self.panel.changed.emit()

    def apply_styles(self, styles: dict):
        by_name = dict(self.editable_series())
        for name, st in styles.items():
            if name in by_name:
                apply_series_state(by_name[name], st)
        self.styles = dict(styles)

    def edit(self):
        from .plot_edit import PlotEditDialog
        PlotEditDialog(self, self.panel.window()).exec()

    def export_light(self):
        from PySide6.QtWidgets import QFileDialog
        from .plot_edit import export_light
        from ..project import LastDirs
        last = LastDirs()
        name = (self.title or "plot").replace(" ", "_").replace("/", "-")
        path, _ = QFileDialog.getSaveFileName(self.panel.window(), "Export plot (light mode)",
                                              str(Path(last.get("png_export", Path.cwd())) / f"{name}.png"), "PNG image (*.png)")
        if path:
            last.remember("png_export", path)
            export_light(self, path if path.lower().endswith(".png") else path + ".png")

    # --- context menu ---
    def _build_menu(self):
        menu: QMenu = self.vb.menu
        menu.addSeparator()
        self._rm_menu = menu.addMenu("Remove signal")
        self._rm_menu.aboutToShow.connect(self._fill_rm_menu)
        self._copy_menu = menu.addMenu("Copy line")
        self._copy_menu.aboutToShow.connect(self._fill_copy_menu)
        self._paste_menu = menu.addMenu("Paste line")
        self._paste_menu.aboutToShow.connect(self._fill_paste_menu)
        self._right_menu = menu.addMenu("Right axis")
        self._right_menu.aboutToShow.connect(self._fill_right_menu)
        menu.aboutToShow.connect(self._enable_line_menus)
        menu.addAction("Move plot up", lambda: self.panel.move_plot(self, -1))
        menu.addAction("Move plot down", lambda: self.panel.move_plot(self, +1))
        self._flip_act = menu.addAction("Flip Y axis")
        self._flip_act.setCheckable(True)
        self._flip_act.toggled.connect(self._flip_toggled)
        menu.addAction("Auto Y range", self.auto_y)
        menu.addAction("Edit plot (colors, lines)…", self.edit)
        menu.addAction("Export plot as light image…", self.export_light)
        menu.addAction("Isolate plot in a floating window",
                       lambda: self.panel.isolateRequested.emit(self))
        menu.addAction("Remove plot", lambda: self.panel.remove_plot(self))

    def _fill_rm_menu(self):
        self._rm_menu.clear()
        if not self.signals and not self.overlays:
            self._rm_menu.addAction("(no added signals)").setEnabled(False)
        for key in list(self.signals):
            self._rm_menu.addAction(SignalKey.parse(key).label,
                                    lambda k=key: (self.remove_signal(k), self.panel.changed.emit()))
        for ov in list(self.overlays):
            self._rm_menu.addAction(ov["name"] + ("" if ov["axis"] == "right" else " (pasted)"),
                                    lambda o=ov: (self.remove_overlay(o), self.panel.changed.emit()))

    def _enable_line_menus(self):
        self._copy_menu.menuAction().setEnabled(bool(self.line_refs()))
        self._paste_menu.menuAction().setEnabled(self.time_based and line_refs.clipboard.get() is not None)
        self._right_menu.menuAction().setEnabled(self.right is not None)

    def _fill_copy_menu(self):
        self._copy_menu.clear()
        for name, ref, _it in self.line_refs():
            self._copy_menu.addAction(name, lambda r=ref: self.copy_line(r))

    def _fill_paste_menu(self):
        m = self._paste_menu
        m.clear()
        ref = line_refs.clipboard.get()
        on = ref is not None
        m.addAction("on left axis", lambda: self.paste_line("left")).setEnabled(on and self.time_based)
        m.addAction("on right axis", lambda: self.paste_line("right")).setEnabled(on and self.time_based)
        m.addAction("as a new plot", lambda: self.panel.paste_as_new_plot(ref)).setEnabled(on)

    def _fill_right_menu(self):
        m = self._right_menu
        m.clear()
        cur = self.right_settings.get("mode", "auto")
        for mode in line_refs.MODES:
            if mode == "match":
                sub = m.addMenu(line_refs.MODE_NAMES[mode])
                sub.menuAction().setCheckable(True)
                sub.menuAction().setChecked(cur == "match")
                for n in self.left_line_names():
                    a = sub.addAction(n, lambda nn=n: self.set_right_settings(
                        dict(self.right_settings, mode="match", match=nn)))
                    a.setCheckable(True)
                    a.setChecked(cur == "match" and self.right_settings.get("match") == n)
                continue
            a = m.addAction(line_refs.MODE_NAMES[mode], lambda mm=mode: self.set_right_mode(mm))
            a.setCheckable(True)
            a.setChecked(cur == mode)
        m.addSeparator()
        m.addAction("Set range/gain\u2026", self.edit_right_axis)

    def edit_right_axis(self):
        from .dual_axis import RightAxisDialog
        RightAxisDialog(self, self.panel.window()).exec()

    def to_dict(self) -> dict:
        if self.graph is not None:
            d = {"graph": self.graph.id, "signals": list(self.signals)}
        else:
            d = {"title": self.title, "signals": list(self.signals)}
        if self.styles:
            d["styles"] = self.styles
        if self.graph is not None and self.title != self.graph.title:
            d["custom_title"] = self.title
        if self.invert_y:
            d["invert_y"] = True
        if self.overlays or self._unresolved:
            ovs = [dict(ov["ref"], axis=ov["axis"], style=series_state(ov["item"])) for ov in self.overlays]
            for pos, u in sorted(self._unresolved, key=lambda x: x[0]):
                ovs.insert(min(pos, len(ovs)), dict(u))
            d["overlay"] = ovs
            if self.right_settings.get("mode", "auto") != "auto":
                d["right_axis"] = dict(self.right_settings)
        return d


class _PlotView(pg.GraphicsLayoutWidget):
    """GraphicsLayoutWidget that accepts signal and graph drops."""

    def __init__(self, panel: "GraphPanel"):
        super().__init__()
        self.panel = panel
        self.setAcceptDrops(True)

    def mousePressEvent(self, ev):
        self.panel.activated.emit(self.panel)
        super().mousePressEvent(ev)

    @staticmethod
    def _ours(md) -> bool:
        return md.hasFormat(MIME_SIGNALS) or md.hasFormat(MIME_GRAPHS) or md.hasFormat(MIME_LINE)

    def dragEnterEvent(self, ev):
        if self._ours(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        if self._ours(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev):
        md = ev.mimeData()
        if not self._ours(md):
            return super().dropEvent(ev)
        pos = self.mapToScene(ev.position().toPoint())
        target = next((p for p in self.panel.plots if p.item.sceneBoundingRect().contains(pos)), None)
        if md.hasFormat(MIME_LINE):
            try:
                ref = json.loads(bytes(md.data(MIME_LINE)).decode())
            except ValueError:
                ref = None
            if target is not None and target.time_based and ref is not None:
                at = self.mapToGlobal(ev.position().toPoint())
                QTimer.singleShot(0, lambda: self.panel.drop_line(ref, target, at))
        elif md.hasFormat(MIME_GRAPHS):
            ids = bytes(md.data(MIME_GRAPHS)).decode().split("\n")
            idx = self.panel.plots.index(target) + 1 if target else None
            for gid in ids:
                self.panel.add_graph(gid, idx)
                idx = None if idx is None else idx + 1
        else:
            keys = bytes(md.data(MIME_SIGNALS)).decode().split("\n")
            self.panel.drop_signals(keys, target)
        self.panel.activated.emit(self.panel)
        ev.acceptProposedAction()


class GraphPanel(QWidget):
    activated = Signal(object)
    changed = Signal()
    isolateRequested = Signal(object)
    renameRequested = Signal(object)
    presetRequested = Signal(object)

    def __init__(self, panel_id: str, bus: TimeBus, title: str = "Graphs", parent=None):
        super().__init__(parent)
        self.panel_id = panel_id
        self.title = title
        self.bus = bus
        self.log: FlightLog | None = None
        self.plots: list[GraphPlot] = []
        self._syncing = False
        self._range: tuple[float, float] | None = None
        self._notes: list = []
        self._note_markers = True
        self._messages: list = []
        # plot specs waiting for a log (a layout applied before a log is open)
        self._pending: list[dict] | None = None

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        add_btn = QToolButton(text="Plot", toolTip="Add an empty plot (or drop signals below)")
        add_btn.clicked.connect(lambda: self.add_plot())
        self.link_btn = QToolButton(text="Link X", checkable=True, checked=True,
                                    toolTip="On: all plots here, other linked panels and the timeline zoom together.\n"
                                            "Off: each plot zooms on its own; switching on realigns them.")
        self.link_btn.toggled.connect(self._on_link_toggled)
        self.follow_btn = QToolButton(text="Follow", checkable=True, checked=True,
                                      toolTip="Scroll the view to keep the cursor visible")
        fit_btn = QToolButton(text="Fit", toolTip="Show the whole flight and re-enable automatic Y scaling")
        self.box_btn = QToolButton(checkable=True,
                                   toolTip="On: drag a rectangle to zoom both axes (wheel stays X-only).\n"
                                           "Fit or a plot's \u201cAuto Y range\u201d restores automatic Y.")
        self.box_btn.toggled.connect(self._apply_box_zoom)
        for b, name in ((add_btn, "plus"), (self.link_btn, "link"), (self.follow_btn, "follow"),
                        (fit_btn, "fit"), (self.box_btn, "box_zoom")):
            icons.decorate(b, name)
            b.setToolButtonStyle(Qt.ToolButtonIconOnly if b is self.box_btn
                                 else Qt.ToolButtonTextBesideIcon)
        fit_btn.clicked.connect(self.fit_all)
        self.height_box = QComboBox(toolTip="Plot height")
        self.height_box.setFocusPolicy(Qt.NoFocus)
        for k in PLOT_HEIGHTS:
            self.height_box.addItem(k)
        self.height_box.setCurrentText("M")
        self.height_box.setMinimumWidth(64)   # letter + padding + drop-down arrow
        self.height_box.setMinimumHeight(26)
        self.height_box.currentTextChanged.connect(lambda _: self._relayout())
        for w in (add_btn, self.link_btn, self.follow_btn, fit_btn, self.box_btn, QLabel("  Height "),
                  self.height_box):
            bar.addWidget(w)
        bar.addStretch(1)
        more = QToolButton(text="\u22ee", toolTip="Panel: rename, save as preset", popupMode=QToolButton.InstantPopup)
        more.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        mm = QMenu(more)
        mm.addAction("Rename panel\u2026", lambda: self.renameRequested.emit(self))
        mm.addAction("Save panel as preset\u2026", lambda: self.presetRequested.emit(self))
        more.setMenu(mm)
        bar.addWidget(more)

        self.view = _PlotView(self)
        self.view.ci.setSpacing(4)
        self.view.scene().sigMouseClicked.connect(self._on_click)
        self.previews = True
        self.view.scene().sigMouseMoved.connect(self._on_hover)
        self.scroll = QScrollArea(widgetResizable=True)
        self.scroll.setWidget(self.view)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.verticalScrollBar().valueChanged.connect(self._refresh_stale)
        self.hint = QLabel("Drag graphs or signals here", alignment=Qt.AlignCenter)
        theme.style_role(self.hint, "dim", "color: gray;")
        self.hint.setAcceptDrops(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(self.hint)
        lay.addWidget(self.scroll, 1)
        self.setAcceptDrops(True)
        self._update_hint()

        bus.timeChanged.connect(self._on_time)
        bus.xRangeChanged.connect(self._on_bus_range)
        bus.trimChanged.connect(self._on_trim)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        QTimer.singleShot(0, self._refresh_stale)  # newly revealed plots

    # empty panel: accept drops on the hint area too
    def dragEnterEvent(self, ev):
        self.view.dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        self.view.dragMoveEvent(ev)

    def dropEvent(self, ev):
        self.view.dropEvent(ev)

    @property
    def plot_height(self) -> int:
        return PLOT_HEIGHTS.get(self.height_box.currentText(), 210)

    def _time_plots(self):
        return [p for p in self.plots if p.time_based]

    # --- log ---
    def set_log(self, log: FlightLog | None):
        specs = self._pending if self._pending is not None else [p.to_dict() for p in self.plots]
        self.log = log
        self.clear()
        if log is not None:
            self._pending = None
            self.load_plots(specs)
        else:
            self._pending = specs

    @property
    def has_content(self) -> bool:
        return bool(self.plots or self._pending)

    def set_notes(self, notes):
        self._notes = list(notes)
        self._feed_notes()

    def set_note_markers(self, on: bool):
        """Show or hide the note markers on the plots (the notes themselves are kept)."""
        if on != self._note_markers:
            self._note_markers = on
            self._feed_notes()

    def _feed_notes(self):
        shown = self._notes if self._note_markers else []
        for p in self._time_plots():
            p.set_notes(shown)

    def set_messages(self, messages):
        self._messages = list(messages)
        for p in self._time_plots():
            p.set_messages(self._messages)

    def clear(self):
        for p in self.plots:
            _clear_highlight(p)
            self.view.ci.removeItem(p.item)
        self.plots = []
        self._relayout()

    # --- plots ---
    def _new_plot(self, p: GraphPlot, index: int | None, relayout: bool = True) -> GraphPlot:
        if self.box_btn.isChecked():
            p.set_box_zoom(True)
        if p.cursor is not None:
            p.cursor.setValue(self.bus.t)
            p.cursor.sigDragged.connect(lambda line: self.bus.seek(line.value()))
            p.vb.sigXRangeChanged.connect(self._on_plot_range)
        if self.bus.trim:
            p.set_trim(self.bus.trim)
        if self.log is not None:
            p.set_modes(self.log.mode_segments())
            if "replay" not in self.log.info:
                p.set_param_changes(self.log.param_changes())
        p.set_notes(self._notes if self._note_markers else [])
        p.set_messages(self._messages)
        self.plots.insert(len(self.plots) if index is None else min(index, len(self.plots)), p)
        if relayout:
            self._relayout()
            if p.time_based:
                self._apply_range(*self._current_range(exclude=p))
            self.changed.emit()
        return p

    def add_graphs(self, graph_ids) -> int:
        """Add several Flight Review graphs in their order, with one relayout."""
        if self.log is None:
            return 0
        rng = self._current_range()
        n = 0
        for gid in graph_ids:
            if gid in self.log.graphs and not self.has_graph(gid):
                self._new_plot(GraphPlot(self, graph=self.log.graphs[gid]),
                               self._fr_insert_index(gid), relayout=False)
                n += 1
        if n:
            self._relayout()
            self._apply_range(*rng)
            self.changed.emit()
        return n

    def add_plot(self, signals=None, title: str = "", index: int | None = None) -> GraphPlot:
        p = GraphPlot(self, title)
        for k in signals or []:
            p.add_signal(k)
        return self._new_plot(p, index)

    def add_graph(self, graph_id: str, index: int | None = None) -> GraphPlot | None:
        """Add a Flight Review graph. index=None keeps Flight Review's order."""
        if self.log is None or graph_id not in self.log.graphs or self.has_graph(graph_id):
            return None
        if index is None:
            index = self._fr_insert_index(graph_id)
        return self._new_plot(GraphPlot(self, graph=self.log.graphs[graph_id]), index)

    def _fr_insert_index(self, graph_id: str) -> int:
        order = list(self.log.graphs)
        rank = order.index(graph_id)
        for i, p in enumerate(self.plots):
            if p.graph is not None and order.index(p.graph.id) > rank:
                return i
        return len(self.plots)

    def has_graph(self, graph_id: str) -> bool:
        return any(p.graph is not None and p.graph.id == graph_id for p in self.plots)

    def remove_graph(self, graph_id: str):
        for p in list(self.plots):
            if p.graph is not None and p.graph.id == graph_id:
                self.remove_plot(p)

    def remove_plot(self, plot: GraphPlot):
        if plot in self.plots:
            _clear_highlight(plot)
            self.view.ci.removeItem(plot.item)
            self.plots.remove(plot)
            self._relayout()
            self.changed.emit()

    def move_plot(self, plot: GraphPlot, delta: int):
        i = self.plots.index(plot)
        j = min(max(i + delta, 0), len(self.plots) - 1)
        if i != j:
            self.plots.insert(j, self.plots.pop(i))
            self._relayout()
            self.changed.emit()

    def drop_signals(self, keys: list[str], target: GraphPlot | None):
        if target is None or not target.time_based:
            self.add_plot(keys)
        else:
            for k in keys:
                target.add_signal(k)
            self.changed.emit()

    def drop_line(self, ref: dict, target: GraphPlot, pos=None) -> bool:
        """A legend entry was dropped on a plot: ask left / right axis, then paste it there."""
        if target not in self.plots:
            return False
        axis = _pick([("Left axis", "left"), ("Right axis", "right")], pos)
        return axis is not None and target.paste_line(axis, ref)

    def paste_as_new_plot(self, ref: dict | None = None) -> GraphPlot | None:
        ref = ref or line_refs.clipboard.get()
        if ref is None or self.log is None or line_refs.resolve(ref, self.log) is None:
            return None
        p = GraphPlot(self, line_refs.ref_label(ref))
        p.add_overlay(ref, "left")
        return self._new_plot(p, None)

    def _relayout(self):
        self.view.ci.clear()
        for i, p in enumerate(self.plots):
            self.view.ci.addItem(p.item, row=i, col=0)
        self.view.setMinimumHeight(self.plot_height * len(self.plots))
        self._update_hint()
        for p in self.plots:
            p.stale = True
        self._refresh_stale()

    def _update_hint(self):
        self.hint.setVisible(not self.plots)
        self.scroll.setVisible(bool(self.plots))

    # --- time / range ---
    # Only plots visible in the scroll area get their X range updated right away;
    # the others are marked stale and catch up when scrolled into view. With
    # dozens of plots this keeps panning and follow-paging fast.
    def _current_range(self, exclude=None) -> tuple[float, float]:
        if self.link_btn.isChecked() and self.bus.x_range:
            return self.bus.x_range
        if self._range is not None:
            return self._range
        return self._full_range()

    def _full_range(self) -> tuple[float, float]:
        """The time span plots may show: the trim, else the whole flight."""
        if self.bus.trim:
            return self.bus.trim
        return (0.0, self.log.duration if self.log else 1.0)

    def _on_trim(self, trim):
        """Re-slice every time-based curve once (never per frame), then realign the view."""
        tp = self._time_plots()
        if not tp:
            return
        self._syncing = True
        try:
            for p in tp:
                p.set_trim(trim)
        finally:
            self._syncing = False
        if trim:
            x0, x1 = self._range or (trim[0], trim[1])
            x0, x1 = max(x0, trim[0]), min(x1, trim[1])
            if x1 <= x0:
                x0, x1 = trim
        else:
            x0, x1 = self._current_range()
        self._apply_range(x0, x1)

    def _visible_plots(self) -> list[GraphPlot]:
        top = self.scroll.verticalScrollBar().value()
        bottom = top + self.scroll.viewport().height()
        out = []
        for p in self._time_plots():
            r = p.item.sceneBoundingRect()
            if r.bottom() >= top - 50 and r.top() <= bottom + 50:
                out.append(p)
        return out

    def _apply_range(self, x0: float, x1: float, skip_vb=None):
        self._range = (x0, x1)
        visible = self._visible_plots()
        self._syncing = True
        try:
            for p in self._time_plots():
                if p.vb is skip_vb:
                    p.stale = False
                elif p in visible:
                    p.vb.setXRange(x0, x1, padding=0)
                    p.stale = False
                else:
                    p.stale = True
        finally:
            self._syncing = False

    def _refresh_stale(self, *_):
        if self._range is None:
            return
        self._syncing = True
        try:
            for p in self._visible_plots():
                if p.stale:
                    p.vb.setXRange(*self._range, padding=0)
                    p.stale = False
        finally:
            self._syncing = False

    def _on_plot_range(self, vb, rng):
        if self._syncing:
            return
        if not self.link_btn.isChecked():
            return   # unlinked: this plot zooms/pans on its own
        self._apply_range(rng[0], rng[1], skip_vb=vb)
        self.bus.set_x_range(rng[0], rng[1], self)

    def _on_bus_range(self, x0, x1, source):
        if source is not self and self.link_btn.isChecked():
            self._apply_range(x0, x1)

    def _on_link_toggled(self, on: bool):
        if on:   # realign every plot (and rejoin the other panels)
            rng = self.bus.x_range or self._range or (
                (0.0, self.log.duration) if self.log else None)
            if rng:
                self._apply_range(*rng)
                if not self.bus.x_range:
                    self.bus.set_x_range(*rng, self)

    def set_plot_height(self, key: str):
        self.height_box.setCurrentText(key)

    def fit_all(self):
        if self.log:
            lo, hi = self._full_range()
            self._apply_range(lo, hi)
            if self.link_btn.isChecked():
                self.bus.set_x_range(lo, hi, self)
        for p in self.plots:
            if p.time_based:
                p.auto_y()
            else:
                p.vb.autoRange()

    def set_box_zoom(self, on: bool):
        """The button is the source of truth: setting it applies the mode once (via toggled)."""
        if self.box_btn.isChecked() != bool(on):
            self.box_btn.setChecked(bool(on))
            return
        self._apply_box_zoom(bool(on))

    def _apply_box_zoom(self, on: bool):
        for p in self.plots:
            p.set_box_zoom(on)
        self.changed.emit()

    def visible_range(self) -> tuple[float, float] | None:
        return self._range if self._time_plots() else None

    def _on_time(self, t: float):
        tp = self._time_plots()
        for p in tp:
            p.cursor.setValue(t)
        if not (self.follow_btn.isChecked() and tp and self._range):
            return
        x0, x1 = self._range
        if t < x0 or t > x1:
            w = x1 - x0
            n0 = t - FOLLOW_LEAD * w
            if self.bus.trim:   # page inside the visible part of the flight
                n0 = min(max(n0, self.bus.trim[0]), max(self.bus.trim[0], self.bus.trim[1] - w))
            self._apply_range(n0, n0 + w)
            if self.link_btn.isChecked():
                self.bus.set_x_range(n0, n0 + w, self)

    def _on_hover(self, pos):
        if not self.previews:
            return
        for p in self._time_plots():
            if p.vb.sceneBoundingRect().contains(pos):
                x = p.vb.mapSceneToView(pos).x()
                show_preview_at(self._notes if self._note_markers else [], self._messages, x, 6 * p.vb.viewPixelSize()[0],
                                self.view.mapToGlobal(self.view.mapFromScene(pos)))
                return

    def _on_click(self, ev):
        if ev.button() != Qt.LeftButton or ev.double():
            return
        pos = ev.scenePos()
        for p in self.plots:
            if p.vb.sceneBoundingRect().contains(pos) or p.legend.sceneBoundingRect().contains(pos):
                if p.legend.isVisible() and p.legend.sceneBoundingRect().contains(pos):
                    # a click on the legend only shows / hides a series (pyqtgraph does that; save it
                    # once it has): not a cursor move
                    QTimer.singleShot(0, p.style_changed)
                    self.activated.emit(self)
                    _highlight(p)
                    return
                if not p.time_based:
                    return
                self.bus.seek(p.vb.mapSceneToView(pos).x())
                self.activated.emit(self)
                _highlight(p)
                return

    # --- persistence ---
    def load_plots(self, specs: list[dict]):
        rng = self._current_range()
        for s in specs:
            if "graph" in s:
                g = self.log.graphs.get(s["graph"]) if self.log else None
                if g is None or self.has_graph(g.id):
                    continue
                p = self._new_plot(GraphPlot(self, graph=g), None, relayout=False)
            else:
                p = self._new_plot(GraphPlot(self, s.get("title", "")), None, relayout=False)
            for k in s.get("signals", []):
                p.add_signal(k)
            if s.get("right_axis"):
                p.right_settings = line_refs.clean_settings(s["right_axis"])
            for o in s.get("overlay", []):
                p.add_overlay(o, o.get("axis", "left"), o.get("style"), keep=True)
            if s.get("custom_title"):
                p.set_title(s["custom_title"])
            if s.get("styles"):
                p.apply_styles(s["styles"])
            if s.get("invert_y"):
                p.set_invert_y(True)
        self._relayout()
        self._apply_range(*rng)
        self.changed.emit()

    def set_plots(self, specs: list[dict]):
        """Replace all plots (used by undo / redo)."""
        self.clear()
        self.load_plots(specs)

    def to_dict(self) -> dict:
        plots = (list(self._pending) if self.log is None and self._pending is not None
                 else [p.to_dict() for p in self.plots])
        return {"id": self.panel_id, "title": self.title,
                "link_x": self.link_btn.isChecked(), "follow": self.follow_btn.isChecked(),
                "height": self.height_box.currentText(), "plots": plots,
                **({"box_zoom": True} if self.box_btn.isChecked() else {})}

    def load_dict(self, d: dict):
        self.clear()
        self.title = d.get("title", self.title)
        self.link_btn.setChecked(d.get("link_x", True))
        self.follow_btn.setChecked(d.get("follow", True))
        self.height_box.setCurrentText(d.get("height", "M"))
        self.box_btn.setChecked(bool(d.get("box_zoom", False)))
        if self.log is None:
            self._pending = list(d.get("plots", []))
        else:
            self._pending = None
            self.load_plots(d.get("plots", []))


__all__ = ["GraphPanel", "GraphPlot", "fmt_time"]
