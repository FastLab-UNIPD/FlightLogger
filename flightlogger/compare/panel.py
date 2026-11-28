"""A comparison plot panel: the same graphs drawn once per active log, linked.

A panel holds a list of *specs* (the plot dicts GraphPanel uses). Each spec is drawn once per active
log of the LogSet (at most three), on a time axis aligned at takeoff (``LogSet.aligned_time``), in one
of two layouts: "columns" (side by side: logs are columns, graphs rows) or "rows" (stacked: for each
graph its per-log plots one under the other).

Linking
* Y: all plots of one spec share one Y range. Automatic = union of the visible data of every log; a Y
  zoom (box zoom) on one plot applies to all; "Auto Y" / Fit return to automatic.
* X width: one width for the whole panel; zooming a plot changes the width of every plot around that
  plot's own centre.
* X position: one per log, shared by that log's plots. Panning moves only that log, unless
  ``pan_together`` is on (every log moves by the same amount).

Performance: only plots in view get their ranges applied at once, the others are marked stale and
catch up when scrolled into view; plots are never rebuilt for a pan, zoom, name or colour change.
"""
from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Mapping
from html import escape

import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QInputDialog, QLabel, QMenu, QProgressBar, QScrollArea, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

from ..log.fr_graphs import GraphDef
from ..log.ulog_loader import ModeSegment
from ..plots import line_refs
from ..plots.common import MIME_GRAPHS, MIME_SIGNALS
from ..plots.graph_panel import PLOT_HEIGHTS, GraphPlot
from ..ui import icons, theme
from .logs import LogEntry, LogSet

MODES = ("columns", "rows")
MODE_LABELS = {"columns": "Side by side", "rows": "Stacked"}
ROWS_HEIGHT_FACTOR = 0.7          # stacked plots are lower: three per graph
HEADER_H = 26
Y_THROTTLE_MS = 40
SHIFT_DEBOUNCE_MS = 120
_QWIDGETSIZE_MAX = 16777215
KIND_HINT = {"spectrogram": "  ▦", "fft": "  ∿ Hz"}


# --- a log whose times are shifted to the comparison time ----------------------------------------
def shift_graph(g: GraphDef, d: float) -> GraphDef:
    """The graph with its time axis moved by d seconds (frequency graphs are unchanged)."""
    if d == 0 or not g.time_based:
        return g
    rep = dict(series=[dataclasses.replace(s, t=s.t + d) for s in g.series],
               nan_times=[t + d for t in g.nan_times],
               dropouts=[(a + d, b + d, c, e) for a, b, c, e in g.dropouts])
    if g.spectrogram is not None:
        rep["spectrogram"] = dataclasses.replace(g.spectrogram, t0=g.spectrogram.t0 + d,
                                                 t1=g.spectrogram.t1 + d)
    return dataclasses.replace(g, **rep)


class _ShiftedGraphs(Mapping):
    def __init__(self, owner: "ShiftedLog"):
        self.owner = owner
        self.cache: dict[str, GraphDef] = {}

    def __getitem__(self, gid):
        g = self.cache.get(gid)
        if g is None:
            g = self.cache[gid] = shift_graph(self.owner.log.graphs[gid], self.owner.delta)
        return g

    def __iter__(self):
        return iter(self.owner.log.graphs)

    def __len__(self):
        return len(self.owner.log.graphs)

    def __contains__(self, gid):
        return gid in self.owner.log.graphs


class ShiftedLog:
    """What a GraphPlot sees as "the log": the same data with times t + delta (delta = -takeoff + offset)."""

    def __init__(self, log, delta: float):
        self.log = log
        self.delta = float(delta)
        self.graphs = _ShiftedGraphs(self)
        self._modes = None

    def set_delta(self, delta: float):
        self.delta = float(delta)
        self.graphs.cache.clear()
        self._modes = None

    def has_signal(self, key) -> bool:
        return self.log.has_signal(key)

    def get(self, key):
        t, y = self.log.get(key)
        return t + self.delta, y

    def mode_segments(self):
        if self._modes is None:
            self._modes = [ModeSegment(s.t0 + self.delta, s.t1 + self.delta, s.nav_state)
                           for s in self.log.mode_segments()]
        return self._modes

    def __getattr__(self, name):          # duration, info, ...
        return getattr(self.log, name)


# --- one plot (one spec of one log) ---------------------------------------------------------------
class _Host(QObject):
    """Plays the role GraphPanel has for a GraphPlot: log, changed signal, menu callbacks."""
    changed = Signal()
    isolateRequested = Signal(object)

    def __init__(self, panel: "ComparePanel", spec: "Spec", entry: LogEntry, slog: ShiftedLog):
        super().__init__(panel)
        self.panel, self.spec, self.entry, self.log = panel, spec, entry, slog
        self.plot = None

    def move_plot(self, _plot, delta: int):
        self.panel.move_spec(self.spec, delta)

    def remove_plot(self, _plot):
        self.panel.remove_spec(self.spec)

    def paste_as_new_plot(self, ref=None):
        return self.panel.paste_as_new_spec(ref)

    def window(self):
        return self.panel.window()


class ComparePlot(GraphPlot):
    """GraphPlot without cursor, with a log tag in the title and the Y range managed by the panel."""

    def __init__(self, host: _Host, title: str = "", graph: GraphDef | None = None):
        super().__init__(host, title=title, graph=graph)
        self.host, self.entry, self.spec = host, host.entry, host.spec
        self.dead = False
        if self.cursor is not None:
            self.item.removeItem(self.cursor)
            self.cursor = None
        if self.time_based:
            self.vb.disableAutoRange(axis="y")      # the panel sets the (shared) Y range
        self._base_title = self.item.titleLabel.text or self.title
        self._tag = ("", "")
        self.show_tag = True
        self.legend_on = True
        for a in list(self.vb.menu.actions()):
            if a.text() == "Isolate plot in a floating window":
                self.vb.menu.removeAction(a)
            elif a.text() == "Remove plot":
                a.setText("Remove graph")
            elif a.text() in ("Move plot up", "Move plot down"):
                a.setText(a.text().replace("plot", "graph"))

    # title with the colored log tag
    def set_tag(self, name: str, color: str):
        self._tag = (name, color)
        self._retitle()

    def _retitle(self):
        name, color = self._tag
        txt = escape(self._base_title)
        if name and self.show_tag:
            txt += f'  <span style="color:{escape(color)}">● {escape(name)}</span>'
        self.item.setTitle(txt, **self._title_opts)
        tl = self.item.titleLabel            # a long title must not stop narrow columns from shrinking
        tl.updateMin = lambda: None
        tl.setMinimumWidth(0)

    def set_tag_visible(self, on: bool):
        if on != self.show_tag:
            self.show_tag = on
            self._retitle()

    def set_title(self, title: str):
        self.title = self._base_title = title
        self._retitle()

    def auto_y(self):
        self.host.panel.auto_y(self.spec)

    def _update_legend(self):
        self.legend.setColumnCount(1)
        self.legend.setVisible(self._n_legend > 0 and getattr(self, "legend_on", True))

    def set_legend_visible(self, on: bool):
        self.legend_on = bool(on)
        self._update_legend()

    def _update_right(self):
        super()._update_right()
        r = self.right
        if r is not None and not getattr(r, "_cmp_linked", False):
            r._cmp_linked = True            # the panel sets the shared right range, not the axis's own timer
            try:
                self.vb.sigXRangeChanged.disconnect(r._on_x)
            except (RuntimeError, TypeError):
                pass

    def left_data(self) -> list:
        right = self._right_ids()
        return [self._full[it] for _n, it in self.editable_series() if id(it) not in right and it in self._full]

    def paste_line(self, axis: str = "left", ref: dict | None = None) -> bool:
        """Pasting resolves per log: a line this log lacks is kept in the spec but not drawn here."""
        ref = line_refs.norm_ref(ref or line_refs.clipboard.get())
        if ref is None:
            return False
        if line_refs.resolve(ref, self.panel.log) is None:
            if not self.host.panel.ref_known(ref):
                return False
            self.add_overlay(ref, axis, keep=True)
            self.panel.changed.emit()
            return True
        return super().paste_line(axis, ref)

    def dispose(self):
        self.dead = True
        if self.right is not None:
            self.right.close()
            self.right = None


class Spec:
    """One graph (or custom plot) of the panel and its per-log plots."""

    def __init__(self, d: dict):
        self.d = d
        self.plots: dict[LogEntry, ComparePlot] = {}
        self.y_auto = True
        self.y_range: tuple[float, float] | None = None
        self.right_link = None      # (data span, match-target span) shared by the right axes

    @property
    def graph_id(self) -> str | None:
        return self.d.get("graph")

    @property
    def title(self) -> str:
        if self.graph_id is not None:
            for p in self.plots.values():
                return p.title
            return self.d.get("custom_title") or self.graph_id
        return self.d.get("title", "")

    @property
    def linked(self) -> bool:
        return bool(self.plots) and all(p.time_based for p in self.plots.values())

    def fixed_range(self):
        rs = [p.graph.y_range for p in self.plots.values() if p.graph is not None and p.graph.y_range]
        return (min(r[0] for r in rs), max(r[1] for r in rs)) if rs else None


class _View(pg.GraphicsLayoutWidget):
    def __init__(self, panel: "ComparePanel"):
        super().__init__()
        self.panel = panel
        self.setAcceptDrops(True)

    @staticmethod
    def _ours(md) -> bool:
        return md.hasFormat(MIME_GRAPHS) or md.hasFormat(MIME_SIGNALS)

    def dragEnterEvent(self, ev):
        ev.acceptProposedAction() if self._ours(ev.mimeData()) else super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        ev.acceptProposedAction() if self._ours(ev.mimeData()) else super().dragMoveEvent(ev)

    def dropEvent(self, ev):
        md = ev.mimeData()
        if not self._ours(md):
            return super().dropEvent(ev)
        self.panel.handle_drop(md)
        ev.acceptProposedAction()


class _Ctx:
    """Per active log: the shifted log the plots draw from."""

    def __init__(self, entry: LogEntry, delta: float):
        self.slog = ShiftedLog(entry.log, delta)
        self.log = entry.log
        self.sig = None


# --- the panel ----------------------------------------------------------------------------------------
class ComparePanel(QWidget):
    changed = Signal()
    renameRequested = Signal(object)
    removeRequested = Signal(object)
    panTogetherToggled = Signal(bool)

    def __init__(self, logset: LogSet, panel_id: str = "cmp_1", title: str = "Comparison",
                 pan_together: bool = False, parent=None):
        super().__init__(parent)
        self.logset = logset
        self.panel_id = panel_id
        self.title = title
        self.mode = "columns"
        self.specs: list[Spec] = []
        self.pan_together = bool(pan_together)
        self.build_seconds = 0.0            # last (re)build of plots (information)
        self._ctx: dict[LogEntry, _Ctx] = {}
        self._shown: list[LogEntry] = []    # entries drawn now, in order
        self._x0: dict[LogEntry, float] = {}
        self._w: float | None = None
        self._syncing = 0
        self._headers: dict[LogEntry, pg.LabelItem] = {}
        self._holes: list = []
        self._shift_pending: set[LogEntry] = set()
        self._box = False
        self._legends = True
        self._disposed = False

        self._ytimer = QTimer(self)
        self._ytimer.setSingleShot(True)
        self._ytimer.setInterval(Y_THROTTLE_MS)
        self._ytimer.timeout.connect(self.recompute_y)
        self._shift_timer = QTimer(self)
        self._shift_timer.setSingleShot(True)
        self._shift_timer.setInterval(SHIFT_DEBOUNCE_MS)
        self._shift_timer.timeout.connect(self._apply_shifts)

        self._build_ui()
        logset.changed.connect(self.sync)
        logset.graphs_ready.connect(self.sync)
        self.sync()

    # --- UI ---
    def _build_ui(self):
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        self.graph_btn = QToolButton(text="Graph", toolTip="Add a Flight Review graph (drawn for every log)",
                                     popupMode=QToolButton.InstantPopup)
        self.graph_menu = QMenu(self.graph_btn)
        self.graph_menu.aboutToShow.connect(self._fill_graph_menu)
        self.graph_btn.setMenu(self.graph_menu)
        self.preset_btn = QToolButton(text="Presets", toolTip="Add the graphs of a preset",
                                      popupMode=QToolButton.InstantPopup)
        self.preset_menu = QMenu(self.preset_btn)
        self.preset_menu.aboutToShow.connect(self._fill_preset_menu)
        self.preset_btn.setMenu(self.preset_menu)
        self.mode_box = QComboBox(toolTip="Side by side: a column per log.\n"
                                          "Stacked: for each graph, the logs one under the other.")
        for m in MODES:
            self.mode_box.addItem(MODE_LABELS[m], m)
        self.mode_box.setFocusPolicy(Qt.NoFocus)
        self.mode_box.setMinimumHeight(26)
        self.mode_box.currentIndexChanged.connect(lambda _i: self.set_mode(self.mode_box.currentData()))
        self.fit_btn = QToolButton(text="Fit", toolTip="Each log from its start, all with the width of the "
                                                         "longest flight; automatic Y")
        self.box_btn = QToolButton(checkable=True, toolTip="On: drag a rectangle to zoom X and Y "
                                                           "(Y applies to the same graph of every log)")
        self.legend_btn = QToolButton(text="Legends", checkable=True, checked=True,
                                      toolTip="Show or hide the line legends of the plots.\nSide by side, legends hide by themselves when the plots get narrow.")
        self.legend_btn.toggled.connect(self.set_legends)
        self.pan_btn = QToolButton(text="Pan together", checkable=True, checked=self.pan_together,
                                   toolTip="On: panning one log moves all logs by the same amount.\n"
                                           "Off: each log pans on its own (zoom always changes every width).")
        for b, name in ((self.graph_btn, "plus"), (self.fit_btn, "fit"), (self.box_btn, "box_zoom"),
                        (self.pan_btn, "link")):
            icons.decorate(b, name)
            b.setToolButtonStyle(Qt.ToolButtonIconOnly if b is self.box_btn else Qt.ToolButtonTextBesideIcon)
        self.fit_btn.clicked.connect(self.fit)
        self.box_btn.toggled.connect(self.set_box_zoom)
        self.pan_btn.toggled.connect(self._pan_toggled)
        self.height_box = QComboBox(toolTip="Plot height")
        self.height_box.setFocusPolicy(Qt.NoFocus)
        for k in PLOT_HEIGHTS:
            self.height_box.addItem(k)
        self.height_box.setCurrentText("M")
        self.height_box.setMinimumWidth(64)
        self.height_box.setMinimumHeight(26)
        self.height_box.currentTextChanged.connect(lambda _t: self._relayout())
        for w in (self.graph_btn, self.preset_btn, self.mode_box, self.fit_btn, self.box_btn, self.pan_btn, self.legend_btn,
                  QLabel(" Height "), self.height_box):
            bar.addWidget(w)
        self.busy_bar = QProgressBar(maximum=0, minimum=0, textVisible=False)
        self.busy_bar.setFixedSize(70, 10)
        self.busy_label = QLabel("")
        bar.addWidget(self.busy_bar)
        bar.addWidget(self.busy_label)
        self.legend = QLabel("")
        self.legend.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.legend.setTextFormat(Qt.RichText)
        self.legend.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # never widens the panel
        bar.addWidget(self.legend, 1)
        more = QToolButton(text="⋮", toolTip="Panel: rename, remove", popupMode=QToolButton.InstantPopup)
        more.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        mm = QMenu(more)
        mm.addAction("Rename panel…", lambda: self.renameRequested.emit(self))
        mm.addAction("Remove panel", lambda: self.removeRequested.emit(self))
        more.setMenu(mm)
        bar.addWidget(more)

        self.view = _View(self)
        self.view.ci.setSpacing(4)
        self.scroll = QScrollArea(widgetResizable=True)
        self.scroll.setWidget(self.view)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.verticalScrollBar().valueChanged.connect(self._refresh_stale)
        self.hint = QLabel("", alignment=Qt.AlignCenter)
        self.hint.setWordWrap(True)
        theme.style_role(self.hint, "dim", "color: gray;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(self.hint)
        lay.addWidget(self.scroll, 1)
        self.setAcceptDrops(True)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._update_legends()
        QTimer.singleShot(0, self._refresh_stale)

    def dragEnterEvent(self, ev):
        self.view.dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        self.view.dragMoveEvent(ev)

    def dropEvent(self, ev):
        self.view.dropEvent(ev)

    def handle_drop(self, md):
        if md.hasFormat(MIME_GRAPHS):
            for gid in bytes(md.data(MIME_GRAPHS)).decode().split("\n"):
                self.add_graph(gid)
        else:
            keys = [k for k in bytes(md.data(MIME_SIGNALS)).decode().split("\n") if k]
            if keys:
                self.add_spec({"title": "Signals", "signals": keys})

    # --- menus ---
    def available_graphs(self) -> dict:
        """{graph id: GraphDef} of the shown logs (union, first log's order first)."""
        out: dict = {}
        for e in self._shown:
            for gid, g in e.log.graphs.items():
                out.setdefault(gid, g)
        return out

    def _fill_graph_menu(self):
        m = self.graph_menu
        m.clear()
        avail = self.available_graphs()
        if not avail:
            m.addAction("(import logs first)").setEnabled(False)
        for gid, g in avail.items():
            a = m.addAction(g.title + KIND_HINT.get(g.kind, ""))
            a.setCheckable(True)
            a.setChecked(self.has_graph(gid))
            a.triggered.connect(lambda on, gid=gid: self.add_graph(gid) if on else self.remove_graph(gid))
        m.addSeparator()
        m.addAction("Empty plot (add lines by pasting)", lambda: self.add_spec({"title": "Plot", "signals": []}))

    def preset_names(self) -> tuple[list[str], list[str]]:
        from ..project import PresetStore
        return list(_builtin_presets()), PresetStore().names()

    def _fill_preset_menu(self):
        m = self.preset_menu
        m.clear()
        builtin, user = self.preset_names()
        for n in builtin:
            m.addAction(n, lambda n=n: self.apply_preset(n))
        if user:
            m.addSeparator()
            for n in user:
                m.addAction(n, lambda n=n: self.apply_preset(n, user=True))

    # --- specs ---
    def has_graph(self, gid: str) -> bool:
        return any(s.graph_id == gid for s in self.specs)

    def _rank(self, gid: str) -> int:
        order = list(self.available_graphs())
        return order.index(gid) if gid in order else len(order)

    def add_graph(self, gid: str) -> Spec | None:
        if self.has_graph(gid):
            return None
        return self.add_spec({"graph": gid})

    def add_spec(self, d: dict, index: int | None = None, relayout: bool = True) -> Spec:
        spec = Spec(dict(d))
        if index is None:
            index = len(self.specs)
            if spec.graph_id is not None:           # Flight Review order among the graphs
                r = self._rank(spec.graph_id)
                for i, s in enumerate(self.specs):
                    if s.graph_id is not None and self._rank(s.graph_id) > r:
                        index = i
                        break
        self.specs.insert(index, spec)
        for e in self._shown:
            self._make_into(spec, e)
        self._init_y(spec)
        if relayout:
            self._relayout()
            self._after_structure()
            self.changed.emit()
        return spec

    def add_specs(self, dicts) -> int:
        n = 0
        for d in dicts:
            if d.get("graph") is not None and self.has_graph(d["graph"]):
                continue
            self.add_spec(d, relayout=False)
            n += 1
        if n:
            self._relayout()
            self._after_structure()
            self.changed.emit()
        return n

    def remove_graph(self, gid: str):
        for s in list(self.specs):
            if s.graph_id == gid:
                self.remove_spec(s)

    def remove_spec(self, spec: Spec):
        if spec not in self.specs:
            return
        self.specs.remove(spec)
        for p in spec.plots.values():
            p.dispose()
        spec.plots.clear()
        self._relayout()
        self.changed.emit()

    def move_spec(self, spec: Spec, delta: int):
        i = self.specs.index(spec)
        j = min(max(i + delta, 0), len(self.specs) - 1)
        if i != j:
            self.specs.insert(j, self.specs.pop(i))
            self._relayout()
            self.changed.emit()

    def paste_as_new_spec(self, ref: dict | None = None) -> Spec | None:
        ref = line_refs.norm_ref(ref or line_refs.clipboard.get() or {})
        if ref is None or not self.ref_known(ref):
            return None
        return self.add_spec({"title": line_refs.ref_label(ref), "signals": [],
                              "overlay": [dict(ref, axis="left")]})

    def ref_known(self, ref: dict) -> bool:
        return any(line_refs.resolve(ref, e.log) is not None for e in self._shown)

    def apply_preset(self, name: str, user: bool = False) -> int:
        """Add the preset's graphs (those already here are skipped; graphs no log has are left out)."""
        if user:
            from ..project import PresetStore
            try:
                dicts = [dict(p) for p in PresetStore().load(name).get("plots", [])]
            except (OSError, ValueError, KeyError):
                return 0
        else:
            dicts = [{"graph": g} for g in _builtin_presets().get(name, [])]
        avail = self.available_graphs()
        if avail:
            dicts = [d for d in dicts if "graph" not in d or d["graph"] in avail]
        return self.add_specs(dicts)

    def plots_for(self, index: int) -> list[ComparePlot]:
        """The plots of spec ``index``, in the order of the shown logs."""
        s = self.specs[index]
        return [s.plots[e] for e in self._shown if e in s.plots]

    def plot_of(self, index: int, entry: LogEntry) -> ComparePlot | None:
        return self.specs[index].plots.get(entry)

    # --- building plots ---
    def _make_into(self, spec: Spec, e: LogEntry) -> ComparePlot | None:
        old = spec.plots.pop(e, None)
        if old is not None:
            old.dispose()
        ctx = self._ctx[e]
        d = spec.d
        host = _Host(self, spec, e, ctx.slog)
        if d.get("graph") is not None:
            g = ctx.slog.graphs.get(d["graph"]) if d["graph"] in ctx.slog.graphs else None
            if g is None:
                return None
            p = ComparePlot(host, graph=g)
        else:
            p = ComparePlot(host, title=d.get("title", ""))
        host.plot = p
        for k in d.get("signals", []):
            p.add_signal(k)
        if d.get("right_axis"):
            p.right_settings = line_refs.clean_settings(d["right_axis"])
        for o in d.get("overlay", []):
            p.add_overlay(o, o.get("axis", "left"), o.get("style"), keep=True)
        if d.get("custom_title"):
            p.set_title(d["custom_title"])
        if d.get("styles"):
            p.apply_styles(d["styles"])
        if d.get("invert_y"):
            p.set_invert_y(True)
        p.set_modes(ctx.slog.mode_segments())
        if self._box:
            p.set_box_zoom(True)
        p.set_legend_visible(self._legend_effective())
        p.show_tag = self.mode == "rows"     # columns have their header; a long title squeezes them
        p.set_tag(e.name, e.color)
        p.stale = True
        host.changed.connect(lambda h=host: self._plot_changed(h))
        p.vb.sigXRangeChanged.connect(lambda vb, rng, p=p: self._on_plot_x(p, rng))
        p.vb.sigYRangeChanged.connect(lambda vb, rng, p=p: self._on_plot_y(p, rng))
        spec.plots[e] = p
        return p

    def _init_y(self, spec: Spec):
        if spec.y_range is None and spec.linked:
            fx = spec.fixed_range()
            spec.y_auto = fx is None
            spec.y_range = fx

    def _plot_changed(self, host: _Host):
        """A plot's lines / styles / axis changed by the user: the other logs' plots follow."""
        spec, plot = host.spec, host.plot
        if plot is None or plot.dead or spec not in self.specs:
            return
        d = plot.to_dict()
        spec.d = d
        key = json.dumps(d, sort_keys=True, default=str)
        rebuilt = False
        for e, p in list(spec.plots.items()):
            if p is not plot and json.dumps(p.to_dict(), sort_keys=True, default=str) != key:
                self._make_into(spec, e)
                rebuilt = True
        if rebuilt:
            self._relayout()
        self._schedule_y()
        self.changed.emit()

    # --- logset reactions ---
    def _ready(self, e: LogEntry) -> bool:
        return e.active and e.log is not None and not e.error and self.logset.has_graphs(e)

    def sync(self, *_):
        """Bring the plots in line with the LogSet: columns, names / colors, offsets."""
        if self._disposed:
            return
        active = self.logset.active_entries()
        for e in active:
            self.logset.ensure_graphs(e)
        ready = [e for e in active if self._ready(e)]
        # forget logs that are gone
        for e in [e for e in self._ctx if e not in self.logset.entries or e.log is not self._ctx[e].log]:
            self._drop_entry(e)
        structural = ready != self._shown
        t0 = time.perf_counter()
        for e in ready:
            delta = float(self.logset.aligned_time(e, 0.0))
            ctx = self._ctx.get(e)
            if ctx is None:
                self._ctx[e] = ctx = _Ctx(e, delta)
                self._x0.setdefault(e, delta)
            elif abs(ctx.slog.delta - delta) > 1e-12:
                self._shift_pending.add(e)
                self._shift_timer.start()
        gone = [e for e in self._shown if e not in ready]
        for s in self.specs:
            for e in gone:
                p = s.plots.pop(e, None)
                if p is not None:
                    p.dispose()
        new = [e for e in ready if e not in self._shown]
        self._shown = ready
        for s in self.specs:
            for e in new:
                self._make_into(s, e)
            self._init_y(s)
        if structural:
            if self._w is None and ready:
                self._fit_x()
            self._relayout()
            self._after_structure()
        else:
            self._retag()
        if new:
            self.build_seconds = time.perf_counter() - t0
        self._update_status()

    def _drop_entry(self, e: LogEntry):
        for s in self.specs:
            p = s.plots.pop(e, None)
            if p is not None:
                p.dispose()
        self._ctx.pop(e, None)
        self._x0.pop(e, None)
        self._shift_pending.discard(e)
        if e in self._shown:
            self._shown.remove(e)

    def _apply_shifts(self):
        pend, self._shift_pending = [e for e in self._shift_pending if e in self._shown], set()
        for e in pend:
            ctx = self._ctx[e]
            ctx.slog.set_delta(float(self.logset.aligned_time(e, 0.0)))
            for s in self.specs:
                if e in s.plots:
                    self._make_into(s, e)
        if pend:
            self._relayout()
            self._after_structure()

    def flush_shifts(self):
        self._shift_timer.stop()
        self._apply_shifts()

    def _retag(self):
        for e in self._shown:
            hdr = self._headers.get(e)
            if hdr is not None:
                _set_header(hdr, e)
            for s in self.specs:
                p = s.plots.get(e)
                if p is not None and p._tag != (e.name, e.color):
                    p.set_tag(e.name, e.color)
        self._update_legend()

    def _update_status(self):
        loading = [e for e in self.logset.active_entries() if e.loading or e.graphs_loading]
        self.busy_bar.setVisible(bool(loading))
        self.busy_label.setVisible(bool(loading))
        if loading:
            e = loading[0]
            self.busy_label.setText(f"{'Loading' if e.loading else 'Computing graphs of'} {e.name}…")
        if self._shown:
            self.hint.setVisible(False)
        else:
            self.hint.setText("Computing graphs…" if loading else
                              "Import logs (Ctrl+I) and tick them in the Logs list to compare them here")
            self.hint.setVisible(True)
        self.scroll.setVisible(bool(self._shown))
        self._update_legend()

    def _update_legend(self):
        self.legend.setText("<b>Logs:</b> " + "  ".join(
            f'<span style="color:{escape(e.color)}">●</span> {escape(e.name)}' for e in self._shown))

    # --- layout ---
    @property
    def plot_height(self) -> int:
        h = PLOT_HEIGHTS.get(self.height_box.currentText(), 210)
        return int(h * (ROWS_HEIGHT_FACTOR if self.mode == "rows" else 1))

    def set_mode(self, mode: str):
        if mode not in MODES or mode == self.mode:
            return
        self.mode = mode
        i = self.mode_box.findData(mode)
        if self.mode_box.currentIndex() != i:
            self.mode_box.blockSignals(True)
            self.mode_box.setCurrentIndex(i)
            self.mode_box.blockSignals(False)
        self._relayout()
        self._after_structure()
        self.changed.emit()

    def _relayout(self):
        ci = self.view.ci
        ci.clear()
        self._headers = {}
        self._holes = []
        L = ci.layout
        n_rows = 0

        def place(spec, e, row, col):
            p = spec.plots.get(e)
            if p is not None:
                ci.addItem(p.item, row=row, col=col)
            else:
                lab = pg.LabelItem(f"{spec.title or 'This graph'}: not in {e.name}", justify="center")
                self._holes.append(lab)
                ci.addItem(lab, row=row, col=col)

        if self.mode == "columns":
            for c, e in enumerate(self._shown):
                hdr = pg.LabelItem(justify="center")
                _set_header(hdr, e)
                self._headers[e] = hdr
                ci.addItem(hdr, row=0, col=c)
                L.setColumnStretchFactor(c, 1)
            L.setRowFixedHeight(0, HEADER_H)
            for r, s in enumerate(self.specs):
                L.setRowMinimumHeight(r + 1, 0)
                L.setRowMaximumHeight(r + 1, _QWIDGETSIZE_MAX)
                for c, e in enumerate(self._shown):
                    place(s, e, r + 1, c)
            n_rows = len(self.specs)
            total = HEADER_H + self.plot_height * n_rows + 4 * n_rows
        else:
            L.setRowMinimumHeight(0, 0)
            L.setRowMaximumHeight(0, _QWIDGETSIZE_MAX)
            L.setColumnStretchFactor(0, 1)
            row = 0
            for s in self.specs:
                for e in self._shown:
                    place(s, e, row, 0)
                    row += 1
            total = (self.plot_height + 4) * row
        self.view.setMinimumHeight(total)
        for s in self.specs:
            for p in s.plots.values():
                p.set_tag_visible(self.mode == "rows")
                p.stale = True
        self._update_legend()
        self._update_legends()
        self._refresh_stale()

    def _after_structure(self):
        """Ranges for freshly laid-out plots: X from the per-log state, Y recomputed."""
        self.recompute_y()

    # --- ranges: X ---
    def _all_plots(self):
        for s in self.specs:
            for p in s.plots.values():
                yield s, p

    def _visible(self, p: ComparePlot, top: float, bottom: float) -> bool:
        r = p.item.sceneBoundingRect()
        return r.bottom() >= top - 50 and r.top() <= bottom + 50

    def _window(self):
        top = self.scroll.verticalScrollBar().value()
        return top, top + self.scroll.viewport().height()

    def _apply_view(self, spec: Spec, p: ComparePlot):
        self._syncing += 1
        try:
            if p.time_based:
                x0 = self._x0.get(p.entry)
                if x0 is not None and self._w:
                    p.vb.setXRange(x0, x0 + self._w, padding=0)
                if spec.linked and spec.y_range is not None:
                    p.vb.setYRange(*spec.y_range, padding=0)
        finally:
            self._syncing -= 1
        p.stale = False
        if p.right is not None and spec.right_link is not None:
            p.right._data, p.right._target = spec.right_link
            p.right.apply()

    def _refresh_stale(self, *_):
        if self._disposed:
            return
        top, bottom = self._window()
        for s, p in self._all_plots():
            if p.stale and self._visible(p, top, bottom):
                self._apply_view(s, p)

    def flush(self):
        """Apply the ranges to every plot now, also those out of view, and recompute pending Y."""
        self._ytimer.stop()
        self.recompute_y(everything=True)
        for s, p in self._all_plots():
            if p.stale:
                self._apply_view(s, p)

    def _apply_x(self, skip_vb=None):
        top, bottom = self._window()
        self._syncing += 1
        try:
            for s, p in self._all_plots():
                if not p.time_based:
                    continue
                x0 = self._x0.get(p.entry)
                if p.vb is skip_vb:
                    p.stale = False
                elif x0 is not None and self._w and self._visible(p, top, bottom):
                    p.vb.setXRange(x0, x0 + self._w, padding=0)
                    p.stale = False
                else:
                    p.stale = True
        finally:
            self._syncing -= 1

    def _on_plot_x(self, plot: ComparePlot, rng):
        if self._syncing or plot.dead or not plot.time_based or plot.entry not in self._x0:
            return
        a, b = float(rng[0]), float(rng[1])
        w, W = b - a, self._w
        e = plot.entry
        if W is None or abs(w - W) > 1e-7 * max(W, 1e-9):        # zoom: every width follows
            old = W if W is not None else w
            for j in self._x0:
                self._x0[j] += old / 2 - w / 2                   # keep each log's own centre
            self._x0[e] = a
            self._w = w
        else:                                                    # pan
            d = a - self._x0[e]
            for j in (self._x0 if self.pan_together else [e]):
                self._x0[j] += d
        self._apply_x(skip_vb=plot.vb)
        self._schedule_y()

    def set_x(self, entry: LogEntry, x0: float | None = None, width: float | None = None, others: bool = False):
        """Set a log's X start and / or the common width as if the user did (for scripts and tests).
        A width change keeps every log's centre; ``others``: apply the x0 shift to all logs."""
        if width is not None and self._w is not None and abs(width - self._w) > 1e-12:
            for j in self._x0:
                self._x0[j] += self._w / 2 - width / 2
            self._w = width
        elif width is not None:
            self._w = width
        if x0 is not None:
            d = x0 - self._x0[entry]
            for j in (self._x0 if others else [entry]):
                self._x0[j] += d
        self._apply_x()
        self._schedule_y()

    @property
    def x_width(self) -> float | None:
        return self._w

    def x_start(self, entry: LogEntry) -> float | None:
        return self._x0.get(entry)

    def x_range(self, entry: LogEntry) -> tuple[float, float] | None:
        x0 = self._x0.get(entry)
        return None if x0 is None or self._w is None else (x0, x0 + self._w)

    def _pan_toggled(self, on: bool):
        self.pan_together = bool(on)
        self.panTogetherToggled.emit(self.pan_together)

    def set_pan_together(self, on: bool):
        self.pan_together = bool(on)
        if self.pan_btn.isChecked() != self.pan_together:
            self.pan_btn.blockSignals(True)
            self.pan_btn.setChecked(self.pan_together)
            self.pan_btn.blockSignals(False)

    def _fit_x(self):
        ents = [e for e in self._shown if e in self._ctx]
        if not ents:
            return
        for e in ents:
            self._x0[e] = float(self._ctx[e].slog.delta)
        self._w = max(float(e.log.duration) for e in ents) or 1.0

    def fit(self):
        """Each log from its (aligned) start, common width = the longest flight, automatic Y."""
        self._fit_x()
        for s in self.specs:
            self._auto_y_state(s)
        self._apply_x()
        self.recompute_y()
        self.flush()

    # --- ranges: Y ---
    def _auto_y_state(self, spec: Spec):
        fx = spec.fixed_range()
        spec.y_auto = fx is None
        if fx is not None:
            spec.y_range = fx

    def auto_y(self, spec: Spec):
        self._auto_y_state(spec)
        self.recompute_y([spec])

    def _schedule_y(self):
        if any(s.linked for s in self.specs):
            self._ytimer.start()

    def spec_y_union(self, spec: Spec) -> tuple[float, float] | None:
        spans = []
        for e, p in spec.plots.items():
            x0 = self._x0.get(e)
            if x0 is None or self._w is None:
                continue
            for t, y in p.left_data():
                spans.append(line_refs.visible_span(t, y, x0, x0 + self._w))
            if p.graph is not None and p.graph.y_start is not None:
                spans.append((p.graph.y_start, p.graph.y_start))
        return line_refs.union_span(spans)

    def _link_right(self, s: Spec, everything: bool):
        """One right range for the whole graph: union of every log's visible right data."""
        plots = [(e, p) for e, p in s.plots.items() if p.right is not None]
        if not plots:
            s.right_link = None
            return
        data, target = [], []
        for e, p in plots:
            x0 = self._x0.get(e)
            if x0 is None or self._w is None:
                continue
            data += [line_refs.visible_span(t, y, x0, x0 + self._w) for t, y in p.right_arrays()]
            if p.right_settings.get("mode") == "match":
                target += [line_refs.visible_span(t, y, x0, x0 + self._w)
                           for t, y in p.left_arrays(p.right_settings.get("match"))]
        s.right_link = (line_refs.union_span(data), line_refs.union_span(target))
        top, bottom = self._window()
        for _e, p in plots:
            if everything or self._visible(p, top, bottom):
                p.right._data, p.right._target = s.right_link
                p.right.apply()
            else:
                p.stale = True

    def recompute_y(self, specs=None, everything: bool = False):
        top, bottom = self._window()
        for s in specs or self.specs:
            if not s.linked:
                continue
            self._link_right(s, everything)
            if s.y_auto:
                u = self.spec_y_union(s)
                if u is None:
                    continue
                rng = line_refs.pad_span(*u, 0.04)
                if s.y_range is not None and all(abs(a - b) <= 1e-9 * max(1.0, abs(b))
                                                 for a, b in zip(rng, s.y_range)):
                    for p in s.plots.values():
                        if p.stale and (everything or self._visible(p, top, bottom)):
                            self._apply_view(s, p)
                    continue
                s.y_range = rng
            if s.y_range is None:
                continue
            self._syncing += 1
            try:
                for p in s.plots.values():
                    if everything or self._visible(p, top, bottom):
                        p.vb.setYRange(*s.y_range, padding=0)
                        if p.stale:
                            self._apply_view(s, p)
                    else:
                        p.stale = True
            finally:
                self._syncing -= 1

    def _on_plot_y(self, plot: ComparePlot, rng):
        if self._syncing or plot.dead or not plot.time_based or not plot.spec.linked:
            return
        s = plot.spec
        s.y_auto = False
        s.y_range = (float(rng[0]), float(rng[1]))
        top, bottom = self._window()
        self._syncing += 1
        try:
            for p in s.plots.values():
                if p is plot:
                    continue
                if self._visible(p, top, bottom):
                    p.vb.setYRange(*s.y_range, padding=0)
                else:
                    p.stale = True
        finally:
            self._syncing -= 1

    def set_y(self, index: int, lo: float, hi: float):
        """Zoom the Y of one graph (all its per-log plots) to [lo, hi] like a box zoom would."""
        s = self.specs[index]
        s.y_auto, s.y_range = False, (float(lo), float(hi))
        self.recompute_y([s], everything=True)

    def y_range(self, index: int) -> tuple[float, float] | None:
        return self.specs[index].y_range

    def y_ranges(self, index: int) -> list[tuple[float, float]]:
        """The Y range actually shown by each plot of spec ``index`` (after flush())."""
        return [tuple(p.vb.viewRange()[1]) for p in self.plots_for(index)]

    def set_box_zoom(self, on: bool):
        self._box = bool(on)
        if self.box_btn.isChecked() != self._box:
            self.box_btn.blockSignals(True)
            self.box_btn.setChecked(self._box)
            self.box_btn.blockSignals(False)
        for _s, p in self._all_plots():
            p.set_box_zoom(self._box)
        self.changed.emit()

    def set_legends(self, on: bool):
        self._legends = bool(on)
        if self.legend_btn.isChecked() != self._legends:
            self.legend_btn.blockSignals(True)
            self.legend_btn.setChecked(self._legends)
            self.legend_btn.blockSignals(False)
        self._update_legends()
        self.changed.emit()

    LEGEND_MIN_COLUMN = 380       # side by side: narrower plots would be mostly legend

    def _legend_effective(self) -> bool:
        if not self._legends:
            return False
        n = len(self._shown)
        return not (self.mode == "columns" and n > 1 and self.view.width() / n < self.LEGEND_MIN_COLUMN)

    def _update_legends(self):
        on = self._legend_effective()
        for _s, p in self._all_plots():
            if p.legend_on != on:
                p.set_legend_visible(on)

    def set_plot_height(self, key: str):
        self.height_box.setCurrentText(key)

    # --- persistence ---
    def to_dict(self) -> dict:
        return {"id": self.panel_id, "title": self.title, "mode": self.mode,
                "height": self.height_box.currentText(),
                "plots": [json.loads(json.dumps(s.d)) for s in self.specs],
                **({"box_zoom": True} if self._box else {}),
                **({} if self._legends else {"legends": False})}

    def load_dict(self, d: dict):
        for s in self.specs:
            for p in s.plots.values():
                p.dispose()
        self.specs = []
        self.title = d.get("title", self.title)
        self.height_box.blockSignals(True)
        self.height_box.setCurrentText(d.get("height", "M"))
        self.height_box.blockSignals(False)
        mode = d.get("mode", "columns")
        self.mode = mode if mode in MODES else "columns"
        self.mode_box.blockSignals(True)
        self.mode_box.setCurrentIndex(max(0, self.mode_box.findData(self.mode)))
        self.mode_box.blockSignals(False)
        self.set_box_zoom(bool(d.get("box_zoom", False)))
        self.set_legends(bool(d.get("legends", True)))
        for sd in d.get("plots", []):
            if isinstance(sd, dict) and (sd.get("graph") is not None or "title" in sd or "signals" in sd):
                spec = Spec(dict(sd))
                self.specs.append(spec)
                for e in self._shown:
                    self._make_into(spec, e)
                self._init_y(spec)
        self._relayout()
        self._after_structure()
        self._update_status()

    def dispose(self):
        """The panel is removed: stop timers, drop plots, disconnect from the LogSet."""
        self._disposed = True
        self._ytimer.stop()
        self._shift_timer.stop()
        for s in self.specs:
            for p in s.plots.values():
                p.dispose()
        for sig, slot in ((self.logset.changed, self.sync), (self.logset.graphs_ready, self.sync)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def rename_dialog(self) -> str | None:
        name, ok = QInputDialog.getText(self, "Rename panel", "Panel name:", text=self.title)
        return name.strip() if ok and name.strip() else None


def _set_header(hdr: pg.LabelItem, e: LogEntry):
    hdr.setText(f"● {e.name}", color=e.color, bold=True, size="11pt")
    hdr.updateMin = lambda: None            # a long name must not stop the columns from narrowing
    hdr.setMinimumWidth(0)
    hdr.setMinimumHeight(HEADER_H - 4)


def _builtin_presets() -> dict:
    try:
        from ..log.fr_graphs import GRAPH_PRESETS      # noqa: F401 - where the presets may live
        return GRAPH_PRESETS
    except ImportError:
        from ..app import GRAPH_PRESETS
        return GRAPH_PRESETS
