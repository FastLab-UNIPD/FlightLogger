"""Main window: dock layout, menus, transport bar, keyboard handling, log loading."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QObject, QProcess, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox,
                               QDialog, QFileDialog, QInputDialog, QLabel, QLineEdit, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit, QProgressDialog, QSizePolicy, QWidget, QTextEdit, QToolBar, QToolButton)

from .log.flight_review import DownloadCancelled, LinkError, download_log, parse_link
from .log.ulog_loader import FlightLog
from .notes.export import EXTENSIONS, FILE_FILTERS, render
from .notes.export_dialog import ExportNotesDialog
from .notes.model import NotesModel
from .notes.panel import NotesPanel
from .project import (PROJECT_EXT, LayoutStore, PresetStore, LastDirs, RecentProjects, Settings, b64, load_project,
                      save_project, unb64)
from .playback import SPEED_PRESETS, Playback
from .sync import Sync
from .plots.common import MESSAGE_FILTERS, filter_messages, fmt_time
from .plots.graph_panel import GraphPanel
from .undo import UndoManager
from .plots.timeline import TimelineWidget
from .timebus import TimeBus
from .ui import dock_host as dh
from .ui import icons, theme
from .ui.graph_list import GraphList
from .ui.signal_browser import SignalBrowser
from .ui.tables import MessagesTable, ParamsTable
from .ui.track_view import TrackView
from .video.mpv_widget import VIDEO_EXTENSIONS
from .video.video_panel import VideoPanel

STEP_SMALL, STEP_MEDIUM, STEP_LARGE = 0.1, 1.0, 10.0

# Quick graph groups for "New graph panel" (Flight Review graph ids; missing ones are skipped)
GRAPH_PRESETS = {
    "Positions": ["local_position_x", "local_position_y", "local_position_z"],
    "Angles": ["roll_angle", "pitch_angle", "yaw_angle"],
    "Angular rates": ["roll_angular_rate", "pitch_angular_rate", "yaw_angular_rate"],
    "Velocity and inputs": ["velocity", "manual_control_inputs_radio_or_joystick"],
    "Actuators": ["actuator_controls", "motor_outputs", "servo_outputs"],
    "Raw sensors": ["raw_acceleration", "raw_angular_speed_gyroscope", "raw_magnetic_field_strength",
                    "vibration_metrics"],
    "Power and system": ["power", "temperature", "cpu_ram"],
}


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
        elif key == Qt.Key_N and mods == Qt.ControlModifier:
            w.notes_panel.set_end_at_cursor()
        elif key == Qt.Key_Delete and not mods and w.delete_selected():
            pass
        elif key == Qt.Key_Z and mods == Qt.ControlModifier:
            w.undo()
        elif key == Qt.Key_Z and mods == (Qt.ControlModifier | Qt.ShiftModifier) \
                or key == Qt.Key_Y and mods == Qt.ControlModifier:
            w.redo()
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


class _FloatingKeeper(QObject):
    """Torn-off panels are ordinary top-level windows (own title bar with minimize /
    maximize / close, taskbar entry, free placement on the desktop). This only makes
    sure a newly created one is really shown and lies inside a screen: QtAds sometimes
    leaves a new one hidden or off screen."""

    def __init__(self, win: QMainWindow):
        super().__init__(win)
        self.win = win

    FLAGS = (Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint
             | Qt.WindowSystemMenuHint | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)

    def watch(self, fw):
        # QtAds sets its flags after this signal, so apply ours a moment later
        QTimer.singleShot(60, lambda: self._normalize(fw))

    def _normalize(self, fw):
        """A plain window with minimize / maximize buttons and a taskbar entry (QtAds'
        own flags make the window manager hide it from the taskbar and the Alt+Tab list)."""
        try:
            if not any(not d.isClosed() for d in fw.dockWidgets()):
                return
            if fw.windowFlags() != self.FLAGS:
                visible = fw.isVisible()
                fw.setWindowFlags(self.FLAGS)
                if visible:
                    fw.show()
            self._check(fw)
        except RuntimeError:
            pass

    def _check(self, fw, recheck: bool = True):
        try:
            if not any(not d.isClosed() for d in fw.dockWidgets()):
                return               # closed on purpose: leave it hidden
            if not fw.isVisible() and not fw.isMinimized():
                fw.show()
            avail = [s.availableGeometry() for s in QGuiApplication.screens()]
            fr = fw.frameGeometry()
            if not any(fr.intersected(a).width() > 120 and fr.intersected(a).height() > 40
                       for a in avail):
                c = self.win.frameGeometry().center()
                fw.move(c.x() - fr.width() // 2, c.y() - fr.height() // 2)
            if recheck:
                QTimer.singleShot(700, lambda: self._check(fw, False))
        except RuntimeError:  # already deleted
            pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        from . import TITLE_SUFFIX
        self.setWindowTitle(f"FlightLogger{TITLE_SUFFIX}")
        self.setMinimumSize(1100, 700)
        self.resize(1500, 900)
        self.bus = TimeBus(self)
        self.clock = Playback(self.bus, self)
        self.log: FlightLog | None = None
        self.panels: dict[str, tuple[GraphPanel, object]] = {}
        self.active_panel: GraphPanel | None = None
        self._panel_seq = 0
        self.undo_mgr = UndoManager(self)
        self._loader: _LoadThread | None = None
        self._load_threads: set = set()
        self.notes = NotesModel(self)
        self.layouts = LayoutStore()
        self.presets = PresetStore()
        self.recent = RecentProjects()
        self.project_path: Path | None = None
        self.settings = Settings()
        self.last_dirs = LastDirs()
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
        self.video_panel.overlayRequested.connect(
            lambda kind, on: (self.video_notes_act if kind == "notes" else self.video_msgs_act).setChecked(on))
        self.video_panel.notes_source = lambda: self.notes.notes
        self.video_panel.messages_source = self._video_messages
        self.notes_panel = NotesPanel(self.bus, self.notes)
        self.notes_panel.exportRequested.connect(self.export_notes_dialog)
        self.timeline.contextRequested.connect(self._timeline_menu)

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
        self.docks.mgr.setSplitterSizes(center, [680, 600, 320])
        self.docks.mgr.setSplitterSizes(left, [560, 300])
        self.docks.mgr.setSplitterSizes(right, [380, 420])
        # Torn-off windows stay above the main window (transient for it), so
        # clicking the main window no longer hides them behind it; they can
        # still go to any screen. Also bring each new one to the front.
        self._floating_keeper = _FloatingKeeper(self)
        self.docks.mgr.floatingWidgetCreated.connect(self._floating_keeper.watch)
        self._fixed_docks = [self.video_dw, self.graphs_dw, self.browser_dw, self.timeline_dw,
                             self.track_dw, self.messages_dw, self.params_dw, self.notes_dw]

        self._build_toolbar()
        self._build_menus()
        from .ui import dock_debug
        dock_debug.install(self)   # only active with FLIGHTLOGGER_DEBUG_DOCK=<logfile>
        self.status = QLabel("Open a .ulg log (Ctrl+O) or drop one on the window")
        self.statusBar().addWidget(self.status, 1)
        QApplication.instance().installEventFilter(_KeyFilter(self))
        self.setAcceptDrops(True)

        self.notes.changed.connect(self._on_notes_changed)
        self.undo_mgr.reset()
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
    def _create_panel_dock(self, title: str | None = None, pid: str | None = None,
                           deletable: bool = False):
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
        panel.isolateRequested.connect(self.isolate_plot)
        panel.renameRequested.connect(lambda p: self.rename_panel_dialog(p.panel_id))
        panel.presetRequested.connect(lambda p: self.save_panel_preset_dialog(p.panel_id))
        panel.previews = self.timeline.previews
        panel.changed.connect(lambda p=panel: self._on_panel_changed(p))
        # Closing a graph panel only hides it (Window menu brings it back);
        # deleting is explicit. Isolated plot copies are deleted on close.
        dw = self.docks.make(panel, title, pid, deletable=deletable)
        dw.closed.connect(lambda pid=pid: self._on_panel_closed(pid))
        self.panels[pid] = (panel, dw)
        self.active_panel = panel
        return dw

    def _build_panel_menu(self, menu: QMenu) -> QMenu:
        menu.setIcon(icons.icon("graph"))
        # one shared action for every copy of this menu: two with Ctrl+G would be ambiguous
        if getattr(self, "_new_panel_act", None) is None:
            self._new_panel_act = QAction(icons.icon("graph"), "Empty panel", self, shortcut="Ctrl+G",
                                          triggered=lambda: self.new_panel())
        menu.addAction(self._new_panel_act)
        menu.addSeparator()
        for name in GRAPH_PRESETS:
            menu.addAction(name, lambda name=name: self.new_preset_panel(name))
        dynamic: list = []   # the user presets: refilled each time the menu opens

        def refill():
            for a in dynamic:
                menu.removeAction(a)
                (a.menu() or a).deleteLater()
            dynamic.clear()
            names = self.presets.names()
            if not names:
                return
            dynamic.append(menu.addSeparator())
            for n in names:
                dynamic.append(menu.addAction(n, lambda n=n: self.new_user_preset_panel(n)))
            sub = menu.addMenu("Delete preset")
            for n in names:
                sub.addAction(n, lambda n=n: self.delete_user_preset(n))
            dynamic.append(sub.menuAction())

        menu.aboutToShow.connect(refill)
        return menu

    def _unique_title(self, base: str) -> str:
        used = {p.title for p, _ in self.panels.values()}
        title, n = base, 1
        while title in used:
            n += 1
            title = f"{base} {n}"
        return title

    def rename_panel(self, pid: str, title: str) -> bool:
        panel, dw = self.panels.get(pid, (None, None))
        title = title.strip()
        if panel is None or not title or title == panel.title:
            return False
        panel.title = title
        self.docks.set_title(dw, title)
        panel.changed.emit()
        return True

    def rename_panel_dialog(self, pid: str):
        panel = self.panels.get(pid, (None, None))[0]
        if panel is None or not self.interactive:
            return
        title, ok = QInputDialog.getText(self, "Rename panel", "Panel name:", text=panel.title)
        if ok:
            self.rename_panel(pid, title)

    def save_panel_preset(self, pid: str, name: str) -> bool:
        panel = self.panels.get(pid, (None, None))[0]
        if panel is None or not name.strip():
            return False
        d = panel.to_dict()
        d.pop("id", None)
        self.presets.save(name.strip(), d)
        return True

    def save_panel_preset_dialog(self, pid: str):
        panel = self.panels.get(pid, (None, None))[0]
        if panel is None or not self.interactive:
            return
        name, ok = QInputDialog.getText(self, "Save panel as preset", "Preset name:", text=panel.title)
        name = name.strip()
        if not (ok and name):
            return
        if name in self.presets.names() and QMessageBox.question(
                self, "Save panel as preset", f"Replace the preset \u201c{name}\u201d?") != QMessageBox.Yes:
            return
        self.save_panel_preset(pid, name)

    def new_user_preset_panel(self, name: str):
        try:
            d = self.presets.load(name)
        except (OSError, ValueError, KeyError):
            return None
        panel = self.new_panel(self._unique_title(name))
        panel.load_dict(dict(d, title=panel.title))
        self._set_active(panel)
        return panel

    def delete_user_preset(self, name: str, confirm: bool = True) -> bool:
        if confirm and self.interactive and QMessageBox.question(
                self, "Delete preset", f"Delete the preset \u201c{name}\u201d?") != QMessageBox.Yes:
            return False
        self.presets.delete(name)
        return True

    def new_preset_panel(self, name: str):
        panel = self.new_panel(self._unique_title(name))
        if self.log is not None:
            for gid in GRAPH_PRESETS[name]:
                if gid in self.log.graphs:
                    panel.add_graph(gid)
        self._set_active(panel)
        return panel

    def isolate_plot(self, plot):
        """Open a copy of a plot in its own landscape floating window (always a fresh window,
        whatever panel is active)."""
        dw = self._create_panel_dock(plot.title or None, deletable=True)
        panel = self.panels[dw.objectName()][0]
        panel.load_plots([plot.to_dict()])
        panel.set_plot_height("L")
        fc = self.docks.mgr.addDockWidgetFloating(dw)
        fc.resize(1000, 480)
        fc.move(self.geometry().center() - fc.rect().center())
        self._set_active(panel)
        return panel

    def _docked_open(self, panel) -> bool:
        dw = self.panels.get(panel.panel_id, (None, None))[1] if panel else None
        return dw is not None and not dw.isClosed() and not dw.isFloating()

    def new_panel(self, title: str | None = None):
        """New graph panel, side by side with the active one when that one sits in
        the main window; otherwise next to the first docked panel (never inside a
        floating or closed panel's window, where it could go unnoticed)."""
        anchor = self.active_panel if self._docked_open(self.active_panel) else next(
            (p for p, _ in self.panels.values() if self._docked_open(p)), None)
        target = self.panels[anchor.panel_id][1].dockAreaWidget() if anchor else None
        dw = self._create_panel_dock(title)
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
        self.undo_mgr.on_panel_changed(panel)
        self._set_dirty(True)
        if panel is self.active_panel:
            self.graph_list.sync(panel)

    def _on_panel_closed(self, pid: str):
        panel, dw = self.panels.get(pid, (None, None))
        if panel is None:
            return
        if dw.features() & dw.DockWidgetFeature.DockWidgetDeleteOnClose:
            self.panels.pop(pid, None)
            self.undo_mgr.forget_panel(pid)
        if panel is self.active_panel:
            self.active_panel = next((p for p, d in self.panels.values() if not d.isClosed()), None)
            self.graph_list.sync(self.active_panel)

    def delete_panel(self, pid: str, confirm: bool = True) -> bool:
        """Remove a graph panel for good (closing one only hides it)."""
        panel, dw = self.panels.get(pid, (None, None))
        if panel is None:
            return False
        if confirm and self.interactive and panel.plots and QMessageBox.question(
                self, "Delete graph panel",
                f"Delete \u201c{panel.title}\u201d and its {len(panel.plots)} plot(s)?") != QMessageBox.Yes:
            return False
        self.panels.pop(pid)
        self.undo_mgr.forget_panel(pid)
        panel.clear()   # also drops a stale plot highlight
        self.docks.mgr.removeDockWidget(dw)
        dw.deleteLater()
        if panel is self.active_panel:
            self.active_panel = next((p for p, d in self.panels.values() if not d.isClosed()), None)
            self.graph_list.sync(self.active_panel)
        return True

    def show_panel(self, pid: str):
        panel, dw = self.panels.get(pid, (None, None))
        if dw is None:
            return
        dw.toggleView(True)
        dw.setAsCurrentTab()
        if dw.isFloating() and dw.floatingDockContainer():
            dw.floatingDockContainer().raise_()
        self._set_active(panel)

    def raise_floating_windows(self):
        for fw in self.docks.mgr.floatingWidgets():
            if fw.isVisible():
                fw.raise_()

    def dock_at_edge(self, docks, area):
        """Dock torn-off panels along a whole edge of the window (full height / width),
        which dropping on an existing panel cannot do."""
        mgr = self.docks.mgr
        first = docks[0]
        mgr.addDockWidget(area, first)
        for d in docks[1:]:
            mgr.addDockWidgetTabToArea(d, first.dockAreaWidget())
        first.toggleView(True)

    def dock_floating_windows(self):
        """Bring every torn-off panel back into the main window."""
        anchor = next((p for p, _ in self.panels.values() if self._docked_open(p)), None)
        area = self.panels[anchor.panel_id][1].dockAreaWidget() if anchor else None
        for fw in list(self.docks.mgr.floatingWidgets()):
            for dw in list(fw.dockWidgets()):
                if dw.objectName() in self.panels:
                    if area is not None:
                        self.docks.mgr.addDockWidgetTabToArea(dw, area)
                    else:
                        area = self.docks.add(dw, dh.CENTER)
                else:
                    self.docks.add(dw, dh.RIGHT)

    def _fill_window_menu(self):
        m = self.window_menu
        m.clear()
        m.addSection("Graph panels")
        for pid, (panel, dw) in self.panels.items():
            where = " (closed)" if dw.isClosed() else " (floating)" if dw.isFloating() else ""
            a = m.addAction(panel.title + where)
            a.setCheckable(True)
            a.setChecked(not dw.isClosed())
            a.triggered.connect(lambda on, pid=pid: self.show_panel(pid) if on
                                else self.panels[pid][1].toggleView(False))
        if not self.panels:
            m.addAction("(none)").setEnabled(False)
        m.addAction("Show all graph panels",
                    lambda: [self.show_panel(pid) for pid in list(self.panels)])
        delete = m.addMenu("Delete graph panel")
        for pid, (panel, _dw) in self.panels.items():
            delete.addAction(panel.title, lambda pid=pid: self.delete_panel(pid))
        delete.setEnabled(bool(self.panels))
        m.addSeparator()
        floating = bool(self.docks.mgr.floatingWidgets())
        a = m.addAction("Bring floating windows to front", self.raise_floating_windows)
        a.setEnabled(floating)
        a = m.addAction("Dock all floating windows", self.dock_floating_windows)
        a.setEnabled(floating)
        edge = m.addMenu("Dock floating window to window edge")
        edge.setEnabled(floating)
        for fw in self.docks.mgr.floatingWidgets():
            docks = [d for d in fw.dockWidgets() if not d.isClosed()]
            if not docks:
                continue
            sub = edge.addMenu(" + ".join(d.windowTitle() for d in docks))
            for label, area in (("Left", dh.LEFT), ("Right", dh.RIGHT), ("Top", dh.TOP), ("Bottom", dh.BOTTOM)):
                sub.addAction(label, lambda docks=docks, area=area: self.dock_at_edge(docks, area))

    def _target_panel(self) -> GraphPanel:
        if self.active_panel is None:
            self.active_panel = self.new_panel()
        else:
            dw = self.panels.get(self.active_panel.panel_id, (None, None))[1]
            if dw is not None and dw.isClosed():   # ticking a graph into a closed panel
                self.show_panel(self.active_panel.panel_id)
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
        tb.setIconSize(QSize(18, 18))
        tb.setMovable(False)

        def act(icon_name, text, tip, slot):
            a = tb.addAction(icons.icon(icon_name), text, slot)
            a.setToolTip(tip)
            return a

        act("to_start", "Start", "Start (Home)", lambda: self.bus.seek(0.0))
        act("rewind", "−1 s", "Back 1 s (Shift+←)", lambda: self.step(-STEP_MEDIUM))
        act("step_back", "−0.1 s", "Back one video frame / 0.1 s (←)", lambda: self.step(-STEP_SMALL))
        self.play_act = tb.addAction(icons.icon("play", "primary"), "Play", self.clock.toggle)
        self.play_act.setToolTip("Play/Pause (Space)")
        play_btn = tb.widgetForAction(self.play_act)
        play_btn.setObjectName("playButton")
        play_btn.setProperty("variant", "primary")
        act("step_forward", "+0.1 s", "Forward one video frame / 0.1 s (→)", lambda: self.step(STEP_SMALL))
        act("forward", "+1 s", "Forward 1 s (Shift+→)", lambda: self.step(STEP_MEDIUM))
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
        self.time_label = QLabel(objectName="timeLabel")
        if not theme.is_themed():
            self.time_label.setStyleSheet("font-family: monospace; font-size: 13px; padding: 0 8px;")
        tb.addWidget(self.time_label)

        # second row: file / edit commands, with words so they are clear
        self.addToolBarBreak(Qt.TopToolBarArea)
        ab = QToolBar("Actions")
        ab.setObjectName("actions")
        self.addToolBar(Qt.TopToolBarArea, ab)
        ab.setIconSize(QSize(18, 18))
        ab.setMovable(False)
        ab.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        def aact(icon_name, text, tip, slot):
            a = ab.addAction(icons.icon(icon_name), text, slot)
            a.setToolTip(tip)
            return a

        aact("log", "Open log", "Open a flight log (Ctrl+O)", self.open_log_dialog)
        aact("film", "Open video", "Open a video (Ctrl+Shift+O)", self.open_video_dialog)
        aact("folder", "Open project", "Open a project file (Ctrl+Shift+P)", self.open_project_dialog)
        aact("save", "Save", "Save the project (Ctrl+S)", lambda: self.save_project())
        ab.addSeparator()
        self.undo_act = aact("undo", "Undo", "Undo the last change to notes or plots (Ctrl+Z)", self.undo)
        self.redo_act = aact("redo", "Redo", "Redo (Ctrl+Shift+Z or Ctrl+Y)", self.redo)
        self.update_undo_actions(False, False)
        ab.addSeparator()
        panel_btn = QToolButton(text="New panel  ▾", toolTip="New graph panel: empty (Ctrl+G) or a preset group")
        icons.decorate(panel_btn, "graph")
        panel_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        panel_btn.setPopupMode(QToolButton.InstantPopup)
        panel_btn.setMenu(self._build_panel_menu(QMenu(self)))
        ab.addWidget(panel_btn)
        aact("edit", "Edit plot", "Edit the selected (last clicked) plot: colors, thickness, "
             "line style", self.edit_selected_plot)
        aact("note", "Add note", "Add a note at the cursor (N)", self.add_note)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        spacer.setStyleSheet("background: transparent;")
        ab.addWidget(spacer)
        light_now = theme.is_themed() and not theme.is_dark()
        aact("moon" if light_now else "sun", "Dark mode" if light_now else "Light mode",
             "Switch the theme (the app restarts, asking to save first)",
             lambda: self._set_theme("dark" if light_now else "light"))

        self.clock.playingChanged.connect(
            lambda p: self.play_act.setIcon(icons.icon("pause" if p else "play", "primary")))
        self.clock.speedChanged.connect(self._on_speed)
        self.bus.timeChanged.connect(self._update_time_label)
        self._update_time_label(0.0)

    def undo(self):
        self.undo_mgr.undo()

    def redo(self):
        self.undo_mgr.redo()

    def update_undo_actions(self, can_undo: bool, can_redo: bool):
        self.undo_act.setEnabled(can_undo)
        self.redo_act.setEnabled(can_redo)

    def edit_selected_plot(self):
        from .plots.graph_panel import highlighted_plot
        plot = highlighted_plot()
        if plot is None and self.active_panel is not None and self.active_panel.plots:
            plot = self.active_panel.plots[0]
        if plot is None:
            self.statusBar().showMessage("Click a plot first, then press Edit plot", 4000)
            return
        plot.edit()

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
        a = QAction(icons.icon("log"), "&Open log…", self, shortcut=QKeySequence.Open,
                    triggered=self.open_log_dialog)
        m.addAction(a)
        m.addAction(QAction(icons.icon("globe"), "Open Flight Review &link…", self, shortcut="Ctrl+L",
                            triggered=self.open_flight_review_dialog))
        m.addAction(QAction(icons.icon("film"), "Open &video…", self, shortcut="Ctrl+Shift+O",
                            triggered=self.open_video_dialog))
        m.addSeparator()
        m.addAction(QAction(icons.icon("folder"), "Open &project…", self, shortcut="Ctrl+Shift+P",
                            triggered=self.open_project_dialog))
        self.recent_menu = m.addMenu("Recent projects")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        m.addAction(QAction(icons.icon("save"), "&Save project", self, shortcut=QKeySequence.Save,
                            triggered=lambda: self.save_project()))
        m.addAction(QAction("Save project &as…", self, shortcut="Ctrl+Shift+S",
                            triggered=lambda: self.save_project(ask=True)))
        m.addAction(QAction("&Export notes as text…", self, triggered=self.export_notes_dialog))
        m.addSeparator()
        m.addAction(QAction("&Quit", self, shortcut=QKeySequence.Quit, triggered=self.close))

        v = self.menuBar().addMenu("&View")
        v.addMenu(self._build_panel_menu(QMenu("New &graph panel", self)))
        v.addAction(QAction(icons.icon("note"), "Add &note at cursor", self,
                            triggered=self.add_note))  # key: N
        trim_menu = v.addMenu("Hide &time range")
        self._add_trim_actions(trim_menu)
        self.layout_menu = v.addMenu(icons.icon("layout"), "&Layouts")
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
        def toggle(text, key, default, slot):
            a = QAction(text, self, checkable=True)
            a.setChecked(bool(self.settings.get(key, default)))
            a.toggled.connect(lambda on: (self.settings.set(key, bool(on)), slot(bool(on))))
            v.addAction(a)
            slot(a.isChecked())
            return a

        self.previews_act = toggle("Note &previews on hover", "note_previews", True,
                                   self._set_previews)
        self.video_notes_act = toggle("Show &notes over the video", "video_notes", False,
                                      lambda on: self.video_panel.set_overlay(notes=on))
        self.video_msgs_act = toggle("Show log &messages over the video", "video_messages", False,
                                     lambda on: self.video_panel.set_overlay(messages=on))
        self.markers_act = toggle("Notes and warnings on the video &bar", "video_markers", True,
                                  self.video_panel.set_show_markers)
        self.timeline_msgs_act = QAction("Show warnings on the &timeline", self, checkable=True)
        self.timeline_msgs_act.setChecked(bool(self.settings.get("timeline_messages", True)))
        for layer in ("notes", "events", "modes"):
            self.timeline.layer_visible[layer] = bool(self.settings.get(f"timeline_{layer}", True))
        self.timeline_msgs_act.toggled.connect(self._on_timeline_msgs_toggled)
        v.addAction(self.timeline_msgs_act)
        theme_menu = v.addMenu("&Theme")
        tgroup = QActionGroup(theme_menu)
        current = self.settings.get("theme", "dark")
        for mode, label in (("dark", "Dark"), ("light", "Light")):
            a = theme_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(mode == current)
            tgroup.addAction(a)
            a.triggered.connect(lambda _c=False, mode=mode: self._set_theme(mode))
        v.addSeparator()
        for dw in self._fixed_docks:
            v.addAction(dw.toggleViewAction())

        self.window_menu = self.menuBar().addMenu("&Window")
        self.window_menu.aboutToShow.connect(self._fill_window_menu)
        self._fill_window_menu()
        self._make_shortcuts_global()

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("&Update parameter definitions…", self.update_param_definitions)
        help_menu.addAction("&About FlightLogger", self.show_about)

    def _make_shortcuts_global(self):
        """Ctrl+S & co. must work from every FlightLogger window: panels floating on their own
        (e.g. Notes, while typing in it) are separate windows, which a window shortcut misses.
        Open modal dialogs still block them."""
        for a in self.findChildren(QAction):
            if not a.shortcut().isEmpty():
                a.setShortcutContext(Qt.ApplicationShortcut)

    def update_param_definitions(self):
        from .ui.param_help import update_definitions
        self._param_update = update_definitions(self, self.interactive)

    def show_about(self):
        from . import IS_DEV, __version__
        box = QMessageBox(self)
        box.setWindowTitle(f"About FlightLogger {__version__}" + (" (development)" if IS_DEV else ""))
        logo = Path(__file__).parent / "resources" / "flightlogger.svg"
        if logo.exists():
            box.setIconPixmap(QIcon(str(logo)).pixmap(64, 64))
        box.setTextFormat(Qt.RichText)
        dev = ("<p><b>Development version</b>: not an official release.</p>" if IS_DEV else "")
        box.setText(f"<h3>FlightLogger {__version__}</h3>" + dev +
                    "<p>Desktop viewer for PX4 <code>.ulg</code> flight logs with Flight Review "
                    "graphs, synced flight video and timeline notes.</p>"
                    "<p>All the code is entirely AI generated.</p>"
                    "<p>Graphs ported from <a href=\"https://github.com/PX4/flight_review\">PX4 Flight "
                    "Review</a> (BSD-3-Clause).</p>"
                    "<p><a href=\"https://github.com/FastLab-UNIPD/FlightLogger\">"
                    "github.com/FastLab-UNIPD/FlightLogger</a></p>")
        box.setTextInteractionFlags(Qt.TextBrowserInteraction)
        box.exec()

    # --- stepping ---
    def step(self, dt: float):
        self.clock.step_seconds(dt)

    # --- video ---
    def open_video_dialog(self):
        start = str(Path(self.video_panel.video_path).parent) if self.video_panel.video_path else (
            str(self.log.path.parent) if self.log else str(Path.cwd()))
        start = self.last_dirs.get("video", start)
        pattern = " ".join(f"*{e} *{e.upper()}" for e in VIDEO_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(self, "Open flight video", start,
                                              f"Videos ({pattern});;All files (*)")
        if path:
            self.last_dirs.remember("video", path)
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
        start = self.last_dirs.get("log", self.log.path.parent if self.log else Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, "Open PX4 log", start, "PX4 ULog (*.ulg);;All files (*)")
        if path:
            self.last_dirs.remember("log", path)
            self.open_log(path)

    def open_log(self, path: str, source_url: str | None = None):
        self._pending_source_url = source_url
        self.clock.pause()
        self._dismiss_load_dialogs()   # never stack a second dialog on an unfinished one
        dlg = QProgressDialog(f"Loading {Path(path).name}…", None, 0, 0, self)
        dlg.setObjectName("loadProgress")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        loader = _LoadThread(path)
        self._loader = loader
        self._load_threads.add(loader)   # keep the thread object alive until it has finished
        loader.finished.connect(lambda l=loader: self._load_threads.discard(l))
        # only the newest load may fill the window; an older one is ignored
        loader.loaded.connect(lambda log, err, l=loader: self._on_load_finished(l, log, err))
        loader.start()

    def _dismiss_load_dialogs(self):
        """Close every "Loading ..." dialog (also one whose show timer is still pending)."""
        for d in self.findChildren(QProgressDialog, "loadProgress"):
            d.reset()
            d.hide()
            d.setParent(None)
            d.deleteLater()

    def _on_load_finished(self, loader, log, err: str):
        if loader is not self._loader:
            return
        self._dismiss_load_dialogs()
        self._on_loaded(log, err)

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
        self.undo_mgr.reset()
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

    def delete_selected(self) -> bool:
        """Canc: the selected note if the notes panel has focus, else the highlighted plot."""
        from .plots.graph_panel import highlighted_plot
        fw = QApplication.focusWidget()
        in_notes = fw is not None and self.notes_panel.isAncestorOf(fw)
        plot = highlighted_plot()
        if plot is not None and not in_notes:
            for panel, _ in self.panels.values():
                if plot in panel.plots:
                    panel.remove_plot(plot)
                    return True
        if self.notes_panel.has_selection():
            self.notes_panel.delete_current()
            return True
        return False

    def _on_notes_changed(self):
        notes = self.notes.notes
        for panel, _ in self.panels.values():
            panel.set_notes(notes)
        self.timeline.set_notes(notes)
        self.video_panel.update_overlay()
        self.video_panel.update_markers()
        self.undo_mgr.on_notes_changed()
        self._set_dirty(True)

    # --- dirty state / title ---
    def _set_dirty(self, dirty: bool):
        self._dirty = dirty
        self.setWindowModified(dirty)

    def _update_title(self):
        name = (self.project_path.name if self.project_path
                else self.log.path.name if self.log else None)
        from . import TITLE_SUFFIX
        self.setWindowTitle(f"{name}[*] — FlightLogger{TITLE_SUFFIX}" if name
                            else f"FlightLogger{TITLE_SUFFIX}[*]")
        self.setWindowModified(self._dirty)

    def _worth_saving(self) -> bool:
        return bool(len(self.notes) or self.project_path or self.clock.offset != 0.0)

    def _maybe_save(self) -> bool:
        """Ask to save unsaved work. False = the user cancelled."""
        self.notes_panel.flush_edits()
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
        for pid in list(self.panels):
            self.delete_panel(pid, confirm=False)
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
        self.notes_panel.flush_edits()   # a comment typed a moment ago must be in the file
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
                **({"trim": list(self.bus.trim)} if self.bus.trim else {}),
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
            if not self.project_path:   # a remembered folder only replaces the folder, not the name
                start = str(Path(self.last_dirs.get("project", Path(start).parent)) / Path(start).name)
            path, _ = QFileDialog.getSaveFileName(self, "Save project", start,
                                                  f"FlightLogger project (*{PROJECT_EXT})")
            if not path:
                return False
            self.last_dirs.remember("project", path)
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
        start = self.last_dirs.get("project", start)
        path, _ = QFileDialog.getOpenFileName(self, "Open project", start,
                                              f"FlightLogger project (*{PROJECT_EXT})")
        if path:
            self.last_dirs.remember("project", path)
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
        found, _ = QFileDialog.getOpenFileName(
            self, f"Locate {what}", self.last_dirs.get("locate", Path(path).parent), file_filter)
        if found:
            self.last_dirs.remember("locate", found)
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
            try:   # optional key (newer projects): the hidden time range
                tr = data.get("trim")
                if tr and len(tr) == 2:
                    self.bus.set_trim(float(tr[0]), float(tr[1]))
            except (TypeError, ValueError):
                pass
            if data.get("x_range"):
                self.bus.set_x_range(*data["x_range"], None)
            self.bus.seek(float(data.get("cursor", 0.0)))
            self.clock.set_speed(float(data.get("speed", 1.0)))
            if video_path:
                self.open_video(video_path)
            self.recent.add(self.project_path)
            self.undo_mgr.reset()
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
        self.video_panel.update_markers()
        msgs = self._visible_messages()
        for panel, _ in self.panels.values():
            panel.set_messages(msgs)
        self.timeline.set_messages(msgs if self.timeline_msgs_act.isChecked() else [])

    def _timeline_menu(self, pos):
        m = QMenu(self)
        m.addAction(self.timeline_msgs_act)
        for layer, label in (("notes", "Show notes"), ("events", "Show flight events"),
                             ("modes", "Show flight modes")):
            a = m.addAction(label)
            a.setCheckable(True)
            a.setChecked(self.timeline.layer_visible[layer])
            a.toggled.connect(lambda on, layer=layer: self._on_timeline_layer(layer, on))
        m.addSeparator()
        self._add_trim_actions(m)
        m.addSeparator()
        m.addAction("Add note at cursor", self.add_note)
        m.exec(pos)

    # --- hide a time range (project-wide; graphs, timeline and track show only the rest) ---
    def _add_trim_actions(self, menu):
        menu.addAction("Hide before cursor", self.hide_before_cursor)
        menu.addAction("Hide after cursor", self.hide_after_cursor)
        menu.addAction("Show whole flight", self.show_whole_flight)

    def set_trim(self, t0=None, t1=None) -> bool:
        """Hide the flight outside [t0, t1] (None keeps that edge). Undoable, marks the project dirty."""
        if self.log is None:
            return False
        prev = self.bus.trim
        if not self.bus.set_trim(t0, t1):
            self.statusBar().showMessage("Cannot hide: nothing would be left", 3000)
            return False
        if self.bus.trim != prev:
            self.undo_mgr.record_trim(prev)
            self._set_dirty(True)
        return True

    def hide_before_cursor(self):
        return self.set_trim(t0=self.bus.t)

    def hide_after_cursor(self):
        return self.set_trim(t1=self.bus.t)

    def show_whole_flight(self):
        if self.log is None or self.bus.trim is None:
            return
        prev = self.bus.trim
        self.bus.clear_trim()
        self.undo_mgr.record_trim(prev)
        self._set_dirty(True)

    def _on_timeline_layer(self, layer: str, on: bool):
        self.settings.set(f"timeline_{layer}", bool(on))
        self.timeline.set_layer_visible(layer, on)

    def _set_theme(self, mode: str):
        """Save the theme and restart the app (after the usual unsaved-changes prompt)."""
        if mode == ("dark" if theme.is_dark() else "light"):
            return
        previous = self.settings.get("theme", "dark")
        args = [str(self.project_path)] if self.project_path else (
            [str(self.log.path)] if self.log else [])
        self.settings.set("theme", mode)
        if self.close():
            QProcess.startDetached(sys.executable, ["-m", "flightlogger", *args],
                                   str(Path(__file__).resolve().parents[1]))
        else:
            self.settings.set("theme", previous)

    def _set_previews(self, on: bool):
        self.timeline.previews = on
        for panel, _ in self.panels.values():
            panel.previews = on

    def _video_messages(self):
        mode = "warnings" if self.message_mode == "off" else self.message_mode
        return filter_messages(self.log.messages(), mode) if self.log else []

    def _on_timeline_msgs_toggled(self, on: bool):
        self.settings.set("timeline_messages", bool(on))
        self._apply_messages()

    def export_notes_dialog(self):
        if not self.interactive:
            return
        has_log = self.log is not None
        if not len(self.notes) and not has_log:
            QMessageBox.information(self, "Export notes", "There are no notes to export.")
            return
        dlg = ExportNotesDialog(self, self.settings.get("notes_export_format", "txt"),
                                bool(self.settings.get("notes_export_messages", False)),
                                self.settings.get("notes_export_level", "warnings"), has_log)
        if dlg.exec() != QDialog.Accepted:
            return
        fmt, include, level = dlg.values()
        if not len(self.notes) and not include:
            QMessageBox.information(self, "Export notes", "There are no notes to export.")
            return
        self.settings.set("notes_export_format", fmt)
        self.settings.set("notes_export_messages", include)
        self.settings.set("notes_export_level", level)
        stem = (self.project_path.stem if self.project_path
                else self.log.path.stem if self.log else "notes")
        base = self.project_path.parent if self.project_path else (
            self.log.path.parent if self.log else Path.cwd())
        start = str(Path(self.last_dirs.get("notes_export", base)) / f"Notes - {stem}{EXTENSIONS[fmt]}")
        path, _ = QFileDialog.getSaveFileName(self, "Export notes", start, FILE_FILTERS[fmt])
        if not path:
            return
        if not Path(path).suffix:
            path += EXTENSIONS[fmt]
        self.last_dirs.remember("notes_export", path)
        self.export_notes(path, fmt, include, level)

    def export_notes(self, path: str, fmt: str = "txt", include_messages: bool = False,
                     level: str = "warnings"):
        self.notes_panel.flush_edits()
        msgs = filter_messages(self.log.messages(), level) if include_messages and self.log else []
        title = (self.project_path.stem if self.project_path
                 else self.log.path.stem if self.log else "")
        Path(path).write_text(render(self.notes.notes, fmt, msgs, title),
                              encoding="utf-8", newline="")
        self.statusBar().showMessage(f"Exported {len(self.notes)} notes to {path}", 5000)

    def set_message_mode(self, mode: str):
        if mode not in MESSAGE_FILTERS:
            return
        self.message_mode = mode
        self.settings.set("message_markers", mode)
        if mode in getattr(self, "_message_actions", {}):
            self._message_actions[mode].setChecked(True)
        self._apply_messages()
