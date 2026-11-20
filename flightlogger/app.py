"""Main window: dock layout, menus, transport bar, keyboard handling, log loading."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QThread, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox,
                               QFileDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressDialog, QTextEdit, QToolBar)

from .log.ulog_loader import FlightLog
from .playback import SPEED_PRESETS, Playback
from .plots.common import fmt_time
from .plots.graph_panel import GraphPanel
from .plots.timeline import TimelineWidget
from .timebus import TimeBus
from .ui import dock_host as dh
from .ui.graph_list import GraphList
from .ui.signal_browser import SignalBrowser
from .ui.tables import MessagesTable, ParamsTable
from .ui.track_view import TrackView
from .video.mpv_widget import VIDEO_EXTENSIONS
from .video.video_panel import VideoPanel

STEP_SMALL, STEP_MEDIUM, STEP_LARGE = 0.1, 1.0, 10.0


class _LoadThread(QThread):
    loaded = Signal(object, str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self):
        try:
            log = FlightLog(self.path)
            log.graphs  # Flight Review graphs + track, computed off the UI thread
            log.track
            self.loaded.emit(log, "")
        except Exception as e:  # noqa: BLE001 - show any parse error to the user
            self.loaded.emit(None, f"{type(e).__name__}: {e}")


class _KeyFilter(QObject):
    """App-wide transport keys, so they work in floating dock windows too."""

    TEXT_WIDGETS = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox)

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.KeyPress or not isinstance(obj, QObject):
            return False
        fw = QApplication.focusWidget()
        if isinstance(fw, self.TEXT_WIDGETS):
            return False
        key, mods = ev.key(), ev.modifiers()
        arrows = key in (Qt.Key_Left, Qt.Key_Right)
        if arrows and isinstance(fw, QAbstractItemView):
            return False  # keep arrow navigation in trees/tables
        w = self.win
        if key == Qt.Key_Space and not mods:
            w.clock.toggle()
        elif arrows:
            sign = 1 if key == Qt.Key_Right else -1
            if mods & Qt.ControlModifier:
                w.step(sign * STEP_LARGE)
            elif mods & Qt.ShiftModifier:
                w.step(sign * STEP_MEDIUM)
            elif not w.clock.step_frames(sign):  # one video frame, or 0.1 s without video
                w.step(sign * STEP_SMALL)
        elif key == Qt.Key_Escape and w.video_panel.fullscreen:
            w.video_panel.exit_fullscreen()
        elif key == Qt.Key_BracketLeft:
            w.clock.slower()
        elif key == Qt.Key_BracketRight:
            w.clock.faster()
        elif key == Qt.Key_Home:
            w.bus.seek(0.0)
        elif key == Qt.Key_End:
            w.bus.seek(w.bus.duration)
        else:
            return False
        return True


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FlightLogger")
        self.resize(1500, 900)
        self.bus = TimeBus(self)
        self.clock = Playback(self.bus, self)
        self.log: FlightLog | None = None
        self.panels: dict[str, tuple[GraphPanel, object]] = {}
        self.active_panel: GraphPanel | None = None
        self._panel_seq = 0
        self._loader: _LoadThread | None = None

        self.docks = dh.DockHost(self)
        self.browser = SignalBrowser()
        self.browser.signalsActivated.connect(self._add_to_active)
        self.graph_list = GraphList()
        self.graph_list.toggled.connect(self._on_graph_toggled)
        self.graph_list.allRequested.connect(self._on_all_graphs)
        self.timeline = TimelineWidget(self.bus)
        self.track = TrackView(self.bus)
        self.messages = MessagesTable(self.bus)
        self.params = ParamsTable()
        self.video_panel = VideoPanel(self.bus, self.clock)
        self.video_panel.openRequested.connect(self.open_video_dialog)

        panel_dw = self._create_panel_dock()
        self.graphs_dw = self.docks.make(self.graph_list, "Graphs", "graphs")
        self.browser_dw = self.docks.make(self.browser, "Signals", "signals")
        self.timeline_dw = self.docks.make(self.timeline, "Timeline", "timeline")
        self.track_dw = self.docks.make(self.track, "Track", "track")
        self.messages_dw = self.docks.make(self.messages, "Messages", "messages")
        self.params_dw = self.docks.make(self.params, "Parameters", "parameters")
        self.video_dw = self.docks.make(self.video_panel, "Video", "video")
        center = self.docks.add(panel_dw, dh.CENTER)
        left = self.docks.add(self.video_dw, dh.LEFT)
        lists = self.docks.add(self.graphs_dw, dh.BOTTOM, left)
        self.docks.mgr.addDockWidgetTabToArea(self.browser_dw, lists)
        right = self.docks.add(self.track_dw, dh.RIGHT)
        msg_area = self.docks.add(self.messages_dw, dh.BOTTOM, right)
        self.docks.mgr.addDockWidgetTabToArea(self.params_dw, msg_area)
        timeline_area = self.docks.add(self.timeline_dw, dh.BOTTOM)
        self.graphs_dw.setAsCurrentTab()
        self.docks.mgr.setSplitterSizes(timeline_area, [800, 110])
        self.docks.mgr.setSplitterSizes(center, [520, 700, 340])
        self.docks.mgr.setSplitterSizes(left, [440, 380])
        self.docks.mgr.setSplitterSizes(right, [380, 420])
        self._fixed_docks = [self.video_dw, self.graphs_dw, self.browser_dw, self.timeline_dw, self.track_dw,
                             self.messages_dw, self.params_dw]

        self._build_toolbar()
        self._build_menus()
        self.status = QLabel("Open a .ulg log (Ctrl+O) or drop one on the window")
        self.statusBar().addWidget(self.status, 1)
        QApplication.instance().installEventFilter(_KeyFilter(self))
        self.setAcceptDrops(True)

    # --- panels ---
    def _create_panel_dock(self, title: str | None = None):
        self._panel_seq += 1
        pid = f"graphs_{self._panel_seq}"
        title = title or f"Graphs {self._panel_seq}"
        panel = GraphPanel(pid, self.bus, title)
        panel.set_log(self.log)
        panel.activated.connect(self._set_active)
        panel.changed.connect(lambda p=panel: self._on_panel_changed(p))
        dw = self.docks.make(panel, title, pid, deletable=True)
        dw.closed.connect(lambda pid=pid: self._on_panel_closed(pid))
        self.panels[pid] = (panel, dw)
        self.active_panel = panel
        return dw

    def new_panel(self):
        """New graph panel, placed side by side with the active one."""
        target = None
        if self.active_panel and self.active_panel.panel_id in self.panels:
            target = self.panels[self.active_panel.panel_id][1].dockAreaWidget()
        dw = self._create_panel_dock()
        if target is not None:
            self.docks.add(dw, dh.RIGHT, target)
        else:
            self.docks.add(dw, dh.CENTER)
        return self.panels[dw.objectName()][0]

    def _set_active(self, panel: GraphPanel):
        if panel is not self.active_panel:
            self.active_panel = panel
            self.graph_list.sync(panel)

    def _on_panel_changed(self, panel: GraphPanel):
        if panel is self.active_panel:
            self.graph_list.sync(panel)

    def _on_panel_closed(self, pid: str):
        panel, _ = self.panels.pop(pid, (None, None))
        if panel is self.active_panel:
            self.active_panel = next((p for p, _ in self.panels.values()), None)
            self.graph_list.sync(self.active_panel)

    def _target_panel(self) -> GraphPanel:
        if self.active_panel is None:
            self.active_panel = self.new_panel()
        return self.active_panel

    def _on_graph_toggled(self, graph_id: str, on: bool):
        panel = self._target_panel()
        if on:
            panel.add_graph(graph_id)
        else:
            panel.remove_graph(graph_id)
        self.graph_list.sync(panel)

    def _on_all_graphs(self, on: bool):
        if self.log is None:
            return
        panel = self._target_panel()
        if on:
            panel.add_graphs(self.log.graphs)
        else:
            for gid in self.log.graphs:
                panel.remove_graph(gid)
        self.graph_list.sync(panel)

    def _add_to_active(self, keys: list[str]):
        panel = self.active_panel or (self.new_panel() if not self.panels else
                                      next(iter(self.panels.values()))[0])
        panel.add_plot(keys)

    # --- toolbar / menus ---
    def _build_toolbar(self):
        tb = QToolBar("Transport")
        tb.setObjectName("transport")
        self.addToolBar(Qt.TopToolBarArea, tb)
        tb.addAction("⏮", lambda: self.bus.seek(0.0)).setToolTip("Start (Home)")
        tb.addAction("−1s", lambda: self.step(-STEP_MEDIUM)).setToolTip("Back 1 s (Shift+←)")
        tb.addAction("−0.1s", lambda: self.step(-STEP_SMALL)).setToolTip("Back 0.1 s (←)")
        self.play_act = tb.addAction("▶", self.clock.toggle)
        self.play_act.setToolTip("Play/Pause (Space)")
        tb.addAction("+0.1s", lambda: self.step(STEP_SMALL)).setToolTip("Forward 0.1 s (→)")
        tb.addAction("+1s", lambda: self.step(STEP_MEDIUM)).setToolTip("Forward 1 s (Shift+→)")
        tb.addSeparator()
        self.speed_box = QComboBox()
        self.speed_box.setFocusPolicy(Qt.NoFocus)
        for s in SPEED_PRESETS:
            self.speed_box.addItem(f"{s:g}×", s)
        self.speed_box.setCurrentIndex(SPEED_PRESETS.index(1.0))
        self.speed_box.setToolTip("Playback speed ([ slower, ] faster)")
        self.speed_box.currentIndexChanged.connect(
            lambda i: self.clock.set_speed(self.speed_box.itemData(i)))
        tb.addWidget(self.speed_box)
        tb.addSeparator()
        self.time_label = QLabel()
        self.time_label.setStyleSheet("font-family: monospace; font-size: 13px; padding: 0 8px;")
        tb.addWidget(self.time_label)

        self.clock.playingChanged.connect(lambda p: self.play_act.setText("⏸" if p else "▶"))
        self.clock.speedChanged.connect(self._on_speed)
        self.bus.timeChanged.connect(self._update_time_label)
        self._update_time_label(0.0)

    def _on_speed(self, s: float):
        i = self.speed_box.findData(s)
        if i >= 0 and i != self.speed_box.currentIndex():
            self.speed_box.blockSignals(True)
            self.speed_box.setCurrentIndex(i)
            self.speed_box.blockSignals(False)

    def _update_time_label(self, t: float):
        self.time_label.setText(f"{fmt_time(t)} / {fmt_time(self.bus.duration, 1)}")

    def _build_menus(self):
        m = self.menuBar().addMenu("&File")
        a = QAction("&Open log…", self, shortcut=QKeySequence.Open, triggered=self.open_log_dialog)
        m.addAction(a)
        m.addAction(QAction("Open &video…", self, shortcut="Ctrl+Shift+O",
                            triggered=self.open_video_dialog))
        m.addSeparator()
        m.addAction(QAction("&Quit", self, shortcut=QKeySequence.Quit, triggered=self.close))

        v = self.menuBar().addMenu("&View")
        v.addAction(QAction("New &graph panel", self, shortcut="Ctrl+G", triggered=self.new_panel))
        v.addSeparator()
        for dw in self._fixed_docks:
            v.addAction(dw.toggleViewAction())

    # --- stepping ---
    def step(self, dt: float):
        self.clock.step_seconds(dt)

    # --- video ---
    def open_video_dialog(self):
        start = str(Path(self.video_panel.video_path).parent) if self.video_panel.video_path else (
            str(self.log.path.parent) if self.log else str(Path.cwd()))
        pattern = " ".join(f"*{e} *{e.upper()}" for e in VIDEO_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(self, "Open flight video", start,
                                              f"Videos ({pattern});;All files (*)")
        if path:
            self.open_video(path)

    def open_video(self, path: str):
        if not self.video_panel.load(path):
            QMessageBox.warning(self, "Video unavailable", self.video_panel.ph_label.text())
            return
        self.video_dw.toggleView(True)
        self.video_dw.raise_()

    def closeEvent(self, ev):
        self.clock.pause()
        self.video_panel.shutdown()
        super().closeEvent(ev)

    # --- log loading ---
    def open_log_dialog(self):
        start = str(self.log.path.parent) if self.log else str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, "Open PX4 log", start, "PX4 ULog (*.ulg);;All files (*)")
        if path:
            self.open_log(path)

    def open_log(self, path: str):
        self.clock.pause()
        dlg = QProgressDialog(f"Loading {Path(path).name}…", None, 0, 0, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        self._loader = _LoadThread(path)
        self._loader.loaded.connect(lambda log, err: (dlg.close(), self._on_loaded(log, err)))
        self._loader.start()

    def _on_loaded(self, log: FlightLog | None, err: str):
        self._loader = None
        if log is None:
            QMessageBox.critical(self, "Could not open log", err)
            return
        self.log = log
        self.bus.reset(log.duration)
        self.browser.set_log(log)
        self.graph_list.set_graphs(log.graphs)
        self.timeline.set_log(log)
        self.track.set_log(log)
        self.messages.set_log(log)
        self.params.set_log(log)
        self.video_panel.set_log(log)
        self.clock._seek_video_to_log(0.0)
        for panel, _ in self.panels.values():
            panel.set_log(log)
        if self.panels and all(not p.plots for p, _ in self.panels.values()):
            panel = self.active_panel or next(iter(self.panels.values()))[0]
            panel.add_graphs(log.graphs)  # Flight Review's full page by default
        self.graph_list.sync(self.active_panel)
        self.bus.set_x_range(0.0, log.duration, None)
        self.status.setText(log.describe())
        self.setWindowTitle(f"FlightLogger — {log.path.name}")

    # --- drag & drop a .ulg and/or a video onto the window ---
    @staticmethod
    def _dropped(ev):
        files = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        logs = [f for f in files if f.lower().endswith(".ulg")]
        videos = [f for f in files if f.lower().endswith(VIDEO_EXTENSIONS)]
        return logs, videos

    def dragEnterEvent(self, ev):
        logs, videos = self._dropped(ev)
        if logs or videos:
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        logs, videos = self._dropped(ev)
        if logs:
            self.open_log(logs[0])
        if videos:
            self.open_video(videos[0])
