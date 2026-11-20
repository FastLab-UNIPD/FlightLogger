"""Main window: dock layout, menus, transport bar, keyboard handling, log loading."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox,
                               QFileDialog, QInputDialog, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QProgressDialog, QTextEdit, QToolBar)

from .log.flight_review import DownloadCancelled, LinkError, download_log, parse_link
from .log.ulog_loader import FlightLog
from .notes.model import NotesModel
from .notes.panel import NotesPanel
from .project import (PROJECT_EXT, LayoutStore, RecentProjects, Settings, b64, load_project,
                      save_project, unb64)
from .playback import SPEED_PRESETS, Playback
from .sync import Sync
from .plots.common import MESSAGE_FILTERS, filter_messages, fmt_time
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


def _is_link(text: str) -> bool:
    try:
        parse_link(text)
        return "://" in text or text.startswith("www.")
    except LinkError:
        return False


class _DownloadThread(QThread):
    progress = Signal(int, int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, link: str):
        super().__init__()
        self.link = link
        self.cancel = False

    def run(self):
        try:
            path = download_log(self.link, progress=lambda d, t: self.progress.emit(d, t),
                                cancelled=lambda: self.cancel)
            self.finished_ok.emit(str(path))
        except DownloadCancelled:
            self.failed.emit("")
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.failed.emit(str(e))


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
        elif key == Qt.Key_N and not mods:
            w.add_note()
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
        self.notes = NotesModel(self)
        self.layouts = LayoutStore()
        self.recent = RecentProjects()
        self.project_path: Path | None = None
        self.settings = Settings()
        self.message_mode = self.settings.get("message_markers", "warnings")
        if self.message_mode not in MESSAGE_FILTERS:
            self.message_mode = "warnings"
        self.log_source_url: str | None = None   # Flight Review link the log came from
        self._pending_source_url: str | None = None
        self._download: _DownloadThread | None = None
        self.interactive = True      # False in tests: no dialogs
        self._dirty = False
        self._after_load = None      # callback once the log finished loading

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
        self.notes_panel = NotesPanel(self.bus, self.notes)

        panel_dw = self._create_panel_dock()
        self.graphs_dw = self.docks.make(self.graph_list, "Graphs", "graphs")
        self.browser_dw = self.docks.make(self.browser, "Signals", "signals")
        self.timeline_dw = self.docks.make(self.timeline, "Timeline", "timeline")
        self.track_dw = self.docks.make(self.track, "Track", "track")
        self.messages_dw = self.docks.make(self.messages, "Messages", "messages")
        self.params_dw = self.docks.make(self.params, "Parameters", "parameters")
        # The video stays in the main window (move, resize, tab freely); tearing
        # it off into its own window is not supported yet. Use fullscreen (⛶)
        # to show it on another screen.
        self.video_dw = self.docks.make(self.video_panel, "Video", "video", floatable=False)
        self.notes_dw = self.docks.make(self.notes_panel, "Notes", "notes")
        center = self.docks.add(panel_dw, dh.CENTER)
        left = self.docks.add(self.video_dw, dh.LEFT)
        lists = self.docks.add(self.graphs_dw, dh.BOTTOM, left)
        self.docks.mgr.addDockWidgetTabToArea(self.browser_dw, lists)
        right = self.docks.add(self.track_dw, dh.RIGHT)
        msg_area = self.docks.add(self.messages_dw, dh.BOTTOM, right)
        self.docks.mgr.addDockWidgetTabToArea(self.params_dw, msg_area)
        self.docks.mgr.addDockWidgetTabToArea(self.notes_dw, msg_area)
        timeline_area = self.docks.add(self.timeline_dw, dh.BOTTOM)
        self.graphs_dw.setAsCurrentTab()
        self.notes_dw.setAsCurrentTab()
        self.docks.mgr.setSplitterSizes(timeline_area, [800, 110])
        self.docks.mgr.setSplitterSizes(center, [520, 700, 340])
        self.docks.mgr.setSplitterSizes(left, [440, 380])
        self.docks.mgr.setSplitterSizes(right, [380, 420])
        # bring torn-off windows to the front (some window managers keep a
        # window opened without direct user input behind the active one)
        self.docks.mgr.floatingWidgetCreated.connect(
            lambda fw: QTimer.singleShot(0, lambda: (fw.raise_(), fw.activateWindow())))
        self._fixed_docks = [self.video_dw, self.graphs_dw, self.browser_dw, self.timeline_dw,
                             self.track_dw, self.messages_dw, self.params_dw, self.notes_dw]

        self._build_toolbar()
        self._build_menus()
        self.status = QLabel("Open a .ulg log (Ctrl+O) or drop one on the window")
        self.statusBar().addWidget(self.status, 1)
        QApplication.instance().installEventFilter(_KeyFilter(self))
        self.setAcceptDrops(True)

        self.notes.changed.connect(self._on_notes_changed)
        self.clock.offsetChanged.connect(lambda _o: self._set_dirty(True))
        self._default_layout = self.capture_layout()
        self._update_title()
        startup = self.layouts.startup
        if startup:
            try:
                self.apply_layout(self.layouts.load(startup))
            except (OSError, ValueError, KeyError):
                pass

    # --- panels ---
    def _create_panel_dock(self, title: str | None = None, pid: str | None = None):
        if pid is None:
            self._panel_seq += 1
            pid = f"graphs_{self._panel_seq}"
        else:
            num = pid.rsplit("_", 1)[-1]
            if num.isdigit():
                self._panel_seq = max(self._panel_seq, int(num))
        title = title or f"Graphs {pid.rsplit('_', 1)[-1]}"
        panel = GraphPanel(pid, self.bus, title)
        panel.set_log(self.log)
        panel.set_notes(self.notes.notes)
        panel.set_messages(self._visible_messages())
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
        self._set_dirty(True)
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
        m.addAction(QAction("Open Flight Review &link…", self, shortcut="Ctrl+L",
                            triggered=self.open_flight_review_dialog))
        m.addAction(QAction("Open &video…", self, shortcut="Ctrl+Shift+O",
                            triggered=self.open_video_dialog))
        m.addSeparator()
        m.addAction(QAction("Open &project…", self, shortcut="Ctrl+Shift+P",
                            triggered=self.open_project_dialog))
        self.recent_menu = m.addMenu("Recent projects")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        m.addAction(QAction("&Save project", self, shortcut=QKeySequence.Save,
                            triggered=lambda: self.save_project()))
        m.addAction(QAction("Save project &as…", self, shortcut="Ctrl+Shift+S",
                            triggered=lambda: self.save_project(ask=True)))
        m.addSeparator()
        m.addAction(QAction("&Quit", self, shortcut=QKeySequence.Quit, triggered=self.close))

        v = self.menuBar().addMenu("&View")
        v.addAction(QAction("New &graph panel", self, shortcut="Ctrl+G", triggered=self.new_panel))
        v.addAction(QAction("Add &note at cursor", self, triggered=self.add_note))  # key: N
        self.layout_menu = v.addMenu("&Layouts")
        self.layout_menu.aboutToShow.connect(self._fill_layout_menu)
        msg_menu = v.addMenu("Log &messages on graphs")
        group = QActionGroup(msg_menu)
        self._message_actions = {}
        for mode, label in (("off", "Off"), ("errors", "Errors"),
                            ("warnings", "Warnings and errors"), ("all", "All (incl. info)")):
            a = msg_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(mode == self.message_mode)
            group.addAction(a)
            a.triggered.connect(lambda _c=False, mode=mode: self.set_message_mode(mode))
            self._message_actions[mode] = a
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
        if not self._maybe_save():
            ev.ignore()
            return
        self.clock.pause()
        self.video_panel.shutdown()
        super().closeEvent(ev)

    # --- log loading ---
    def open_log_dialog(self):
        start = str(self.log.path.parent) if self.log else str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, "Open PX4 log", start, "PX4 ULog (*.ulg);;All files (*)")
        if path:
            self.open_log(path)

    def open_log(self, path: str, source_url: str | None = None):
        self._pending_source_url = source_url
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
        self.log_source_url, self._pending_source_url = self._pending_source_url, None
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
        self._fill_empty_panels()
        self._apply_messages()
        self.graph_list.sync(self.active_panel)
        self.bus.set_x_range(0.0, log.duration, None)
        self.status.setText(log.describe())
        self._update_title()
        if self._after_load is not None:
            cb, self._after_load = self._after_load, None
            cb()

    def _fill_empty_panels(self):
        """With no graphs anywhere, show Flight Review's full page."""
        if self.log and self.panels and all(not p.has_content for p, _ in self.panels.values()):
            panel = self.active_panel or next(iter(self.panels.values()))[0]
            panel.add_graphs(self.log.graphs)

    # --- drag & drop a .ulg and/or a video onto the window ---
    @staticmethod
    def _dropped(ev):
        md = ev.mimeData()
        files = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        logs = [f for f in files if f.lower().endswith(".ulg")]
        videos = [f for f in files if f.lower().endswith(VIDEO_EXTENSIONS)]
        links = [u.toString() for u in md.urls() if u.scheme() in ("http", "https")]
        if not links and md.hasText() and md.text().strip().startswith("http"):
            links = [md.text().strip()]
        links = [l for l in links if _is_link(l)]
        return logs, videos, links

    def dragEnterEvent(self, ev):
        if any(self._dropped(ev)):
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        logs, videos, links = self._dropped(ev)
        if logs:
            self.open_log(logs[0])
        elif links:  # a Flight Review link dragged from the browser
            self.open_flight_review(links[0])
        if videos:
            self.open_video(videos[0])

    # --- notes ---
    def add_note(self):
        self.notes_dw.toggleView(True)
        self.notes_dw.setAsCurrentTab()
        return self.notes_panel.add_at_cursor()

    def _on_notes_changed(self):
        notes = self.notes.notes
        for panel, _ in self.panels.values():
            panel.set_notes(notes)
        self.timeline.set_notes(notes)
        self._set_dirty(True)

    # --- dirty state / title ---
    def _set_dirty(self, dirty: bool):
        self._dirty = dirty
        self.setWindowModified(dirty)

    def _update_title(self):
        name = (self.project_path.name if self.project_path
                else self.log.path.name if self.log else None)
        self.setWindowTitle(f"{name}[*] — FlightLogger" if name else "FlightLogger[*]")
        self.setWindowModified(self._dirty)

    def _worth_saving(self) -> bool:
        return bool(len(self.notes) or self.project_path or self.clock.offset != 0.0)

    def _maybe_save(self) -> bool:
        """Ask to save unsaved work. False = the user cancelled."""
        if not (self._dirty and self.interactive and self._worth_saving()):
            return True
        r = QMessageBox.question(
            self, "Unsaved changes", "Save the notes, sync and layout to a project file?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Save:
            return self.save_project()
        return True

    # --- layouts ---
    def capture_layout(self) -> dict:
        """Panels and their graphs + dock arrangement + window geometry."""
        return {"panels": [p.to_dict() for p, _ in self.panels.values()],
                "dock_state": b64(self.docks.mgr.saveState()),
                "geometry": b64(self.saveGeometry())}

    def apply_layout(self, layout: dict):
        # replace the graph panels
        for pid, (_panel, dw) in list(self.panels.items()):
            self.panels.pop(pid)
            dw.closeDockWidget()  # deleted on close
        self.active_panel = None
        first_area = None
        for pd in layout.get("panels", []):
            dw = self._create_panel_dock(pd.get("title"), pid=pd.get("id"))
            self.panels[dw.objectName()][0].load_dict(pd)
            if first_area is None:
                first_area = self.docks.add(dw, dh.CENTER)
            else:
                self.docks.mgr.addDockWidgetTabToArea(dw, first_area)
        if not self.panels:  # a layout always keeps at least one graph panel
            self.docks.add(self._create_panel_dock(), dh.CENTER)
        state = unb64(layout.get("dock_state"))
        if state:
            self.docks.mgr.restoreState(QByteArray(state))
        geometry = unb64(layout.get("geometry"))
        if geometry:
            self.restoreGeometry(QByteArray(geometry))
        self._rescue_offscreen_windows()
        self.active_panel = next(iter(self.panels.values()))[0]
        self._fill_empty_panels()
        self.graph_list.sync(self.active_panel)

    def _rescue_offscreen_windows(self):
        """Floating windows saved on a monitor that is not connected now come
        back to the main window's screen."""
        screens = [s.availableGeometry() for s in QGuiApplication.screens()]
        home = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        for fw in self.docks.mgr.floatingWidgets():
            g = fw.frameGeometry()
            if not any(sg.intersects(g) for sg in screens):
                w, h = min(g.width(), home.width()), min(g.height(), home.height())
                fw.setGeometry(home.x() + (home.width() - w) // 2,
                               home.y() + (home.height() - h) // 2, w, h)

    def save_layout_dialog(self):
        name, ok = QInputDialog.getText(self, "Save layout", "Layout name:")
        name = name.strip()
        if not ok or not name:
            return
        if name in self.layouts.names() and QMessageBox.question(
                self, "Save layout", f"Replace the layout \u201c{name}\u201d?") != QMessageBox.Yes:
            return
        self.layouts.save(name, self.capture_layout())
        self.statusBar().showMessage(f"Layout \u201c{name}\u201d saved", 4000)

    def apply_named_layout(self, name: str):
        try:
            self.apply_layout(self.layouts.load(name))
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "Layout", f"Could not load layout \u201c{name}\u201d: {e}")

    def _fill_layout_menu(self):
        m = self.layout_menu
        m.clear()
        m.addAction("Save current layout as…", self.save_layout_dialog)
        names = self.layouts.names()
        if names:
            m.addSeparator()
            for n in names:
                m.addAction(n, lambda n=n: self.apply_named_layout(n))
            m.addSeparator()
            startup = m.addMenu("Use at startup")
            group = QActionGroup(startup)
            current = self.layouts.startup
            for n in [None] + names:
                a = startup.addAction(n or "(none: default layout)")
                a.setCheckable(True)
                a.setChecked(n == current)
                group.addAction(a)
                a.triggered.connect(lambda _c=False, n=n: setattr(self.layouts, "startup", n))
            delete = m.addMenu("Delete")
            for n in names:
                delete.addAction(n, lambda n=n: self._delete_layout(n))
        m.addSeparator()
        m.addAction("Reset to default layout", lambda: self.apply_layout(self._default_layout))

    def _delete_layout(self, name: str):
        if QMessageBox.question(self, "Delete layout",
                                f"Delete the layout \u201c{name}\u201d?") == QMessageBox.Yes:
            self.layouts.delete(name)

    # --- projects ---
    def project_data(self) -> dict:
        video = self.video_panel.video_path or self.video_panel._pending_path
        log = None
        if self.log:
            log = {"path": str(self.log.path)}
            if self.log_source_url:
                log["source_url"] = self.log_source_url
        return {"log": log,
                "video": {"path": video} if video else None,
                "sync": self.clock.sync.to_dict(),
                "cursor": self.bus.t,
                "x_range": list(self.bus.x_range) if self.bus.x_range else None,
                "speed": self.clock.speed,
                "notes": self.notes.to_list(),
                "layout": self.capture_layout()}

    def save_project(self, path: str | Path | None = None, ask: bool = False) -> bool:
        if path is None and (ask or self.project_path is None):
            if not self.interactive:
                return False
            if self.project_path:
                start = str(self.project_path)
            elif self.log:
                start = str(self.log.path.with_suffix(PROJECT_EXT))
            else:
                start = str(Path.cwd() / ("flight" + PROJECT_EXT))
            path, _ = QFileDialog.getSaveFileName(self, "Save project", start,
                                                  f"FlightLogger project (*{PROJECT_EXT})")
            if not path:
                return False
            if not path.endswith(PROJECT_EXT):
                path += PROJECT_EXT
        path = Path(path or self.project_path)
        try:
            save_project(path, self.project_data())
        except OSError as e:
            if self.interactive:
                QMessageBox.critical(self, "Save project", f"Could not save {path}: {e}")
            return False
        self.project_path = path
        self.recent.add(path)
        self._set_dirty(False)
        self._update_title()
        self.statusBar().showMessage(f"Saved {path.name}", 4000)
        return True

    def open_project_dialog(self):
        start = str(self.project_path.parent) if self.project_path else (
            str(self.log.path.parent) if self.log else str(Path.cwd()))
        path, _ = QFileDialog.getOpenFileName(self, "Open project", start,
                                              f"FlightLogger project (*{PROJECT_EXT})")
        if path:
            self.open_project(path)

    def _fill_recent_menu(self):
        self.recent_menu.clear()
        items = self.recent.list()
        if not items:
            self.recent_menu.addAction("(none)").setEnabled(False)
        for p in items:
            self.recent_menu.addAction(Path(p).name, lambda p=p: self.open_project(p))

    def _locate(self, path: str | None, what: str, file_filter: str) -> str | None:
        """The project's file, or one the user picks if it moved."""
        if not path:
            return None
        if Path(path).exists():
            return path
        if not self.interactive:
            return None
        r = QMessageBox.question(self, "File not found",
                                 f"The project's {what} was not found:\n{path}\n\nLocate it?")
        if r != QMessageBox.Yes:
            return None
        found, _ = QFileDialog.getOpenFileName(self, f"Locate {what}", str(Path(path).parent),
                                               file_filter)
        return found or None

    def open_project(self, path: str | Path) -> bool:
        if not self._maybe_save():
            return False
        try:
            data = load_project(path)
        except (OSError, ValueError, KeyError) as e:
            if self.interactive:
                QMessageBox.critical(self, "Open project", f"Could not open {path}: {e}")
            return False
        self.clock.pause()
        self.project_path = Path(path)
        self.notes.load_list(data.get("notes", []))
        self.apply_layout(data.get("layout", {}))
        self.clock.set_sync(Sync.from_dict(data.get("sync") or {}))
        log_entry = data.get("log") or {}
        source_url = log_entry.get("source_url")
        if log_entry.get("path") and not Path(log_entry["path"]).exists() and source_url:
            log_path = None  # downloaded from Flight Review: fetch it again
        else:
            log_path = self._locate(log_entry.get("path"), "log", "PX4 ULog (*.ulg);;All files (*)")
        video_path = self._locate((data.get("video") or {}).get("path"), "video",
                                  "Videos (*);;All files (*)")

        def finish():
            if data.get("x_range"):
                self.bus.set_x_range(*data["x_range"], None)
            self.bus.seek(float(data.get("cursor", 0.0)))
            self.clock.set_speed(float(data.get("speed", 1.0)))
            if video_path:
                self.open_video(video_path)
            self.recent.add(self.project_path)
            self._set_dirty(False)
            self._update_title()

        if log_path:
            self._after_load = finish
            self.open_log(log_path, source_url=source_url)
        elif source_url:
            self._after_load = finish
            self.open_flight_review(source_url)
        else:
            finish()
        return True

    # --- Flight Review links ---
    def open_flight_review_dialog(self):
        clip = QGuiApplication.clipboard().text().strip()
        default = clip if clip and _is_link(clip) else ""
        link, ok = QInputDialog.getText(
            self, "Open Flight Review link",
            "Flight Review link (e.g. https://logs.px4.io/plot_app?log=…) or log id:", text=default)
        if ok and link.strip():
            self.open_flight_review(link.strip())

    def open_flight_review(self, link: str):
        """Download the log behind a Flight Review link (cached) and open it."""
        try:
            parse_link(link)
        except LinkError as e:
            if self.interactive:
                QMessageBox.warning(self, "Flight Review link", str(e))
            return
        if self._download is not None:
            return
        self.clock.pause()
        dlg = QProgressDialog("Downloading log from Flight Review…", "Cancel", 0, 0, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(400)
        th = _DownloadThread(link)
        self._download = th

        def progress(done, total):
            if total:
                dlg.setMaximum(total // 1024)
                dlg.setValue(done // 1024)
            dlg.setLabelText(f"Downloading log from Flight Review… {done / 1e6:.1f} MB"
                             + (f" of {total / 1e6:.1f} MB" if total else ""))

        def ok(path):
            self._download = None
            dlg.close()
            self.open_log(path, source_url=link)

        def failed(err):
            self._download = None
            dlg.close()
            self._after_load = None
            if err and self.interactive:
                QMessageBox.warning(self, "Flight Review link", f"Could not download the log:\n{err}")
            self.statusBar().showMessage(f"Download failed: {err}" if err else "Download cancelled",
                                         6000)

        th.progress.connect(progress)
        th.finished_ok.connect(ok)
        th.failed.connect(failed)
        dlg.canceled.connect(lambda: setattr(th, "cancel", True))
        th.start()

    # --- PX4 message markers ---
    def _visible_messages(self):
        return filter_messages(self.log.messages(), self.message_mode) if self.log else []

    def _apply_messages(self):
        msgs = self._visible_messages()
        for panel, _ in self.panels.values():
            panel.set_messages(msgs)
        self.timeline.set_messages(msgs)

    def set_message_mode(self, mode: str):
        if mode not in MESSAGE_FILTERS:
            return
        self.message_mode = mode
        self.settings.set("message_markers", mode)
        if mode in getattr(self, "_message_actions", {}):
            self._message_actions[mode].setChecked(True)
        self._apply_messages()
