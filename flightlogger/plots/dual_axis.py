"""The second (right) Y axis of a plot.

The standard pyqtgraph two-ViewBox pattern: a second ViewBox is X-linked to the plot's ViewBox, sits
under it (a child of the PlotItem, ignoring the mouse so the main ViewBox keeps the X-only wheel,
panning and box zoom) and draws the lines pasted "on the right axis" against the right AxisItem.

The right Y range is set by us from the plot's ``right`` settings (see ``line_refs.right_range``):
it is recomputed, throttled, when the X view changes (visible data changed), and applied at once when
the left Y range changes (zero / gain / match follow the left axis).
"""
from __future__ import annotations

import pyqtgraph as pg
import shiboken6
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel)

from .line_refs import MODE_NAMES, MODES, clean_settings, right_range, union_span, visible_span

THROTTLE_MS = 60


class _PassiveViewBox(pg.ViewBox):
    """Draws, never takes the mouse."""

    def __init__(self):
        super().__init__(enableMenu=False)
        self.setMouseEnabled(x=False, y=False)
        self.setAcceptedMouseButtons(Qt.NoButton)
        self.setAcceptHoverEvents(False)

    def wheelEvent(self, ev, axis=None):
        ev.ignore()


class RightAxis(QObject):
    """Owns the right ViewBox + AxisItem of one GraphPlot. `plot` supplies the data through
    ``right_arrays()`` (list of (t, y) of the right-axis lines) and ``left_arrays(label)``."""

    def __init__(self, plot):
        super().__init__()
        self.plot = plot
        self.pi: pg.PlotItem = plot.item
        self.vb1: pg.ViewBox = plot.vb
        self.settings: dict = {"mode": "auto"}
        self.range: tuple[float, float] | None = None     # what the right axis shows now
        self._data = None       # visible (min, max) of the right lines
        self._target = None     # visible (min, max) of the match line
        self._items: list = []
        self._open = True

        self.vb = _PassiveViewBox()
        self.vb.setZValue(-1)                   # under the main ViewBox (curves, cursor, legend)
        self.vb.setParentItem(self.pi)
        self.vb.disableAutoRange()
        self.vb.setXRange(*self.vb1.viewRange()[0], padding=0)   # followed by hand (_on_x), not setXLink:
        # a linked ViewBox calls into its partner while both are being destroyed, which crashed at exit
        self.vb.invertY(plot.invert_y)
        self.pi.showAxis("right")
        self.axis = self.pi.getAxis("right")
        self.axis.linkToView(self.vb)
        self.axis.setWidth(60)
        self.axis.enableAutoSIPrefix(False)
        self.axis.setZValue(-1000)
        from .graph_panel import _cache
        _cache(self.axis)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(THROTTLE_MS)
        self._timer.timeout.connect(self.refresh)
        self.vb1.sigResized.connect(self._sync_geometry)
        self.vb1.sigXRangeChanged.connect(self._on_x)
        self.vb1.sigYRangeChanged.connect(self._on_left_y)
        self._sync_geometry()

    # --- items ---
    def add_item(self, item):
        self.vb.addItem(item)
        item.setDownsampling(auto=True, method="peak")
        item.setClipToView(True)
        self._items.append(item)

    def remove_item(self, item):
        if item in self._items:
            self._items.remove(item)
            self.vb.removeItem(item)

    @property
    def n_items(self) -> int:
        return len(self._items)

    def set_label(self, units: str):
        self.axis.setLabel(f"[{units}]" if units else None)

    def set_invert_y(self, on: bool):
        self.vb.invertY(bool(on))

    def close(self):
        """Last right-axis line gone: remove the ViewBox and hide the axis."""
        if not self._open:
            return
        self._open = False
        self._timer.stop()
        for sig, slot in ((self.vb1.sigResized, self._sync_geometry), (self.vb1.sigXRangeChanged, self._on_x),
                          (self.vb1.sigYRangeChanged, self._on_left_y)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.pi.hideAxis("right")
        sc = self.vb.scene()
        if sc is not None:
            sc.removeItem(self.vb)
        self.vb.setParentItem(None)
        self.range = None

    # --- geometry ---
    def _sync_geometry(self, *_):
        if self._open and shiboken6.isValid(self.vb1):
            self.vb.setGeometry(self.vb1.geometry())

    # --- settings ---
    def set_settings(self, s: dict):
        self.settings = clean_settings(s) if s else {"mode": "auto"}
        self.refresh()

    # --- range ---
    def _on_x(self, vb, rng):
        if not self._open:
            return
        try:
            self.vb.setXRange(rng[0], rng[1], padding=0)
        except RuntimeError:
            return
        if self.settings.get("mode") != "manual":
            self._timer.start()

    def _on_left_y(self, *_):
        if self._open and self.settings.get("mode") in ("zero", "gain", "match"):
            self.apply()

    def _x_view(self):
        return self.vb1.viewRange()[0]

    def refresh(self):
        """Recompute the visible data of the right lines (and the match target), then apply."""
        if not self._open or not shiboken6.isValid(self.vb1):
            return
        x0, x1 = self._x_view()
        self._data = union_span(visible_span(t, y, x0, x1) for t, y in self.plot.right_arrays())
        self._target = None
        if self.settings.get("mode") == "match":
            self._target = union_span(visible_span(t, y, x0, x1)
                                      for t, y in self.plot.left_arrays(self.settings.get("match")))
        self.apply()

    def apply(self):
        if not self._open:
            return
        rng = right_range(self.settings, tuple(self.vb1.viewRange()[1]), self._data, self._target)
        if rng is not None and rng[1] > rng[0]:
            self.range = (float(rng[0]), float(rng[1]))
            self.vb.setYRange(*self.range, padding=0)


class RightAxisDialog(QDialog):
    """Set the right axis mode and its numbers; changes show live, Cancel restores."""

    def __init__(self, plot, parent=None):
        super().__init__(parent)
        self.plot = plot
        self.setWindowTitle("Right axis range / gain")
        self._before = dict(plot.right_settings)
        cur = plot.right.range if plot.right is not None and plot.right.range else (0.0, 1.0)
        s = dict(self._before)
        form = QFormLayout(self)
        self.mode = QComboBox()
        for m in MODES:
            self.mode.addItem(MODE_NAMES[m], m)
        self.mode.setCurrentIndex(MODES.index(s.get("mode", "auto")))

        def spin(v, lo=-1e9, hi=1e9):
            w = QDoubleSpinBox(minimum=lo, maximum=hi, decimals=4, singleStep=0.1)
            w.setValue(float(v))
            return w

        self.min = spin(s.get("min", cur[0]))
        self.max = spin(s.get("max", cur[1]))
        self.gain = spin(s.get("gain", 1.0), 1e-6)
        self.offset = spin(s.get("offset", 0.0))
        self.match = QComboBox()
        self.match.addItems(plot.left_line_names())
        if s.get("match") in plot.left_line_names():
            self.match.setCurrentText(s["match"])
        form.addRow("Mode", self.mode)
        form.addRow("Manual: min", self.min)
        form.addRow("Manual: max", self.max)
        form.addRow("Gain: scale (drawn at gain x value + offset)", self.gain)
        form.addRow("Gain: offset (left axis units)", self.offset)
        form.addRow("Match: left line", self.match)
        form.addRow(QLabel("Gain mode keeps the right axis in the lines' own units."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        for w in (self.min, self.max, self.gain, self.offset):
            w.valueChanged.connect(self._push)
        self.mode.currentIndexChanged.connect(self._push)
        self.match.currentIndexChanged.connect(self._push)

    def settings(self) -> dict:
        m = self.mode.currentData()
        return {"mode": m, "min": self.min.value(), "max": self.max.value(), "gain": self.gain.value(),
                "offset": self.offset.value(), "match": self.match.currentText() or None}

    def _push(self, *_):
        self.plot.set_right_settings(self.settings(), emit=False)

    def accept(self):
        self.plot.set_right_settings(self.settings(), emit=True)
        super().accept()

    def reject(self):
        self.plot.set_right_settings(self._before, emit=False)
        super().reject()
