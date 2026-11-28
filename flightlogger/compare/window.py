"""The comparison window: logs list, parameter comparison and (next step) comparison plot panels."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QProcess, QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QDoubleSpinBox, QFileDialog, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPushButton, QTableWidget,
                               QToolBar, QVBoxLayout, QWidget)

from ..project import (COMPARE_EXT, LastDirs, RecentComparisons, RecentProjects, Settings, b64,
                       load_comparison, save_comparison, unb64)
from ..ui import dock_host as dh
from ..ui import icons, theme
from .logs import MAX_ACTIVE, LogEntry, LogSet
from .params import ParamsCompare

_COLS = ["Show", "Color", "Name", "Offset (s)", "Takeoff (s)", ""]


def _swatch(color: str) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(QColor(color))
    return QIcon(pm)


class LogsPanel(QWidget):
    """Imported logs: checkbox (shown), colour, editable name, offset, takeoff time, remove."""

    def __init__(self, logset: LogSet, window: "CompareWindow"):
        super().__init__()
        self.logset, self.win = logset, window
        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.Stretch)
        self.import_btn = QPushButton(icons.icon("plus"), "Import logs…")
        self.import_btn.clicked.connect(window.import_logs_dialog)
        self.hint = QLabel(f"At most {MAX_ACTIVE} logs are shown at once. Logs are aligned at takeoff; "
                           "the offset moves a log in time.")
        self.hint.setWordWrap(True)
        theme.style_role(self.hint, "dim")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.import_btn)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.hint)
        self._sig = None
        self.checks: dict[int, QCheckBox] = {}
        self.spins: dict[int, QDoubleSpinBox] = {}
        self.names: dict[int, QLineEdit] = {}
        logset.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        sig = tuple((id(e), e.name, e.color, e.active, e.log is not None, e.takeoff, e.error, e.loading)
                    for e in self.logset.entries)
        if sig == self._sig:
            return
        self._sig = sig
        t = self.table
        t.setRowCount(0)
        self.checks.clear(), self.spins.clear(), self.names.clear()
        t.setRowCount(len(self.logset.entries))
        for r, e in enumerate(self.logset.entries):
            chk = QCheckBox()
            chk.setChecked(e.active)
            chk.toggled.connect(lambda on, e=e, chk=chk: self._toggle(e, chk, on))
            t.setCellWidget(r, 0, self._centered(chk))
            btn = QPushButton(_swatch(e.color), "")
            btn.setToolTip("Change the color")
            btn.setFixedWidth(34)
            btn.clicked.connect(lambda _c=False, e=e: self._pick_color(e))
            t.setCellWidget(r, 1, btn)
            name = QLineEdit(e.name)
            name.setToolTip(e.path + (f"\n{e.error}" if e.error else ""))
            name.editingFinished.connect(lambda e=e, w=name: self.logset.set_name(e, w.text()))
            t.setCellWidget(r, 2, name)
            spin = QDoubleSpinBox()
            spin.setRange(-100000.0, 100000.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.5)
            spin.setValue(e.offset_s)
            spin.setToolTip("Shifts this log in time (positive = later). 0 = takeoffs aligned")
            spin.valueChanged.connect(lambda v, e=e: self.logset.set_offset(e, v))
            t.setCellWidget(r, 3, spin)
            if e.error:
                txt = "error"
            elif e.log is None:
                txt = "loading…"
            else:
                txt = f"{e.takeoff:.1f}"
            lbl = QLabel(txt)
            lbl.setToolTip(e.error or "Time of takeoff in the log (first landed to airborne, "
                           "else first arming)")
            lbl.setAlignment(Qt.AlignCenter)
            t.setCellWidget(r, 4, lbl)
            rm = QPushButton(icons.icon("trash"), "")
            rm.setToolTip("Remove this log from the comparison")
            rm.setFixedWidth(34)
            rm.clicked.connect(lambda _c=False, e=e: self.logset.remove(e))
            t.setCellWidget(r, 5, rm)
            self.checks[r], self.spins[r], self.names[r] = chk, spin, name
        for c in (0, 1, 3, 4, 5):
            t.resizeColumnToContents(c)

    @staticmethod
    def _centered(w) -> QWidget:
        host = QWidget()
        from PySide6.QtWidgets import QHBoxLayout
        lay = QHBoxLayout(host)
        lay.setContentsMargins(8, 0, 0, 0)
        lay.addWidget(w)
        return host

    def _toggle(self, e: LogEntry, chk: QCheckBox, on: bool):
        ok, why = self.logset.set_active(e, on)
        if not ok:
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)
            self.win.notify(why)

    def _pick_color(self, e: LogEntry):
        if not self.win.interactive:
            return
        c = QColorDialog.getColor(QColor(e.color), self, "Log color")
        if c.isValid():
            self.logset.set_color(e, c.name())


class CompareWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(1000, 650)
        self.resize(1500, 900)
        self.interactive = True          # False in tests: no dialogs
        self.settings = Settings()
        self.last_dirs = LastDirs()
        self.recent = RecentComparisons()
        self.path: Path | None = None
        self.logset = LogSet(self)
        self.panels: list[QWidget] = []          # comparison panels (filled by the next step)
        self._panel_dicts: list[dict] = []       # saved panels waiting to be rebuilt
        self.pan_together = True
        self._restart_args: list[str] | None = None
        self._saved_snapshot = self._snapshot()

        self.docks = dh.DockHost(self)
        self.logs_panel = LogsPanel(self.logset, self)
        self.params = ParamsCompare(self.logset)
        self.panel_host = QWidget()
        self._panel_lay = QVBoxLayout(self.panel_host)
        self._panel_lay.setContentsMargins(4, 4, 4, 4)
        self.panel_hint = QLabel("Comparison panels come in the next step")
        self.panel_hint.setAlignment(Qt.AlignCenter)
        theme.style_role(self.panel_hint, "dim")
        self._panel_lay.addWidget(self.panel_hint)

        self.center_dw = self.docks.make(self.panel_host, "Comparison", "comparison", floatable=False)
        self.logs_dw = self.docks.make(self.logs_panel, "Logs", "logs")
        self.params_dw = self.docks.make(self.params, "Parameters", "parameters")
        center = self.docks.add(self.center_dw, dh.CENTER)
        logs_area = self.docks.add(self.logs_dw, dh.TOP)
        self.docks.add(self.params_dw, dh.BOTTOM)
        self.docks.mgr.setSplitterSizes(logs_area, [220, 480])
        self._fixed_docks = [self.logs_dw, self.params_dw]
        self._default_state = self.docks.mgr.saveState()

        self.status = QLabel("Import logs (Ctrl+I) to compare them")
        self.statusBar().addWidget(self.status, 1)
        self._build_toolbar()
        self._build_menus()
        self._update_title()
        self.logset.failed.connect(lambda e, err: self.notify(f"{e.name}: {err}", warn=True))
        self.logset.changed.connect(self._update_title)

    # --- state ---
    def _snapshot(self) -> str:
        return json.dumps({"logs": self.logset.to_dict() if hasattr(self, "logset") else [],
                           "panels": self.panel_dicts(), "pan": self.pan_together}, sort_keys=True)

    def panel_dicts(self) -> list[dict]:
        """Panels as dicts: widgets with a to_dict() are asked, else the saved dicts are kept."""
        out = [p.to_dict() for p in self.panels if hasattr(p, "to_dict")]
        return out if self.panels else list(self._panel_dicts)

    @property
    def dirty(self) -> bool:
        return self._snapshot() != self._saved_snapshot

    def capture(self) -> dict:
        return {"logs": self.logset.to_dict(), "panels": self.panel_dicts(),
                "pan_together": bool(self.pan_together),
                "dock_state": b64(self.docks.mgr.saveState()), "geometry": b64(self.saveGeometry())}

    def _update_title(self):
        name = self.path.name if self.path else "untitled"
        self.setWindowTitle(f"Comparison — {name}{'*' if self.dirty else ''}{_title_suffix()}")

    def notify(self, text: str, warn: bool = False):
        self.status.setText(text)
        if self.interactive:
            (QMessageBox.warning if warn else QMessageBox.information)(self, "Comparison", text)

    # --- panels hook for the next step ---
    def add_panel(self, widget: QWidget):
        """Add a comparison panel to the central area (it should offer to_dict() for saving)."""
        self.panel_hint.hide()
        self._panel_lay.addWidget(widget, 1)
        self.panels.append(widget)
        self._update_title()

    def load_panels(self, dicts: list[dict]):
        """Hook: called with the saved panel dicts when a comparison is opened. The base keeps them
        (so saving again does not lose them); the panel step rebuilds the widgets."""
        self._panel_dicts = list(dicts or [])

    # --- toolbar / menus ---
    def _build_toolbar(self):
        tb = QToolBar("Actions")
        tb.setObjectName("actions")
        self.addToolBar(Qt.TopToolBarArea, tb)
        tb.setIconSize(QSize(18, 18))
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        def act(icon_name, text, tip, slot):
            a = tb.addAction(icons.icon(icon_name), text, slot)
            a.setToolTip(tip)
            return a

        act("log", "Import logs", "Add logs to compare (Ctrl+I)", self.import_logs_dialog)
        act("folder", "Open", "Open a comparison file (Ctrl+O)", self.open_comparison_dialog)
        act("save", "Save", "Save the comparison (Ctrl+S)", lambda: self.save())
        tb.addSeparator()
        act("compare", "Back to analysis", "Close the comparison and return to the analysis window",
            self.back_to_analysis)

    def _build_menus(self):
        m = self.menuBar().addMenu("&File")
        m.addAction(QAction("&New comparison", self, shortcut=QKeySequence.New, triggered=self.new_comparison))
        m.addAction(QAction(icons.icon("folder"), "&Open comparison…", self, shortcut=QKeySequence.Open,
                            triggered=self.open_comparison_dialog))
        self.recent_menu = m.addMenu("Recent comparisons")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        m.addAction(QAction(icons.icon("save"), "&Save", self, shortcut=QKeySequence.Save,
                            triggered=lambda: self.save()))
        m.addAction(QAction("Save &as…", self, shortcut="Ctrl+Shift+S", triggered=lambda: self.save(ask=True)))
        m.addSeparator()
        m.addAction(QAction(icons.icon("log"), "&Import logs…", self, shortcut="Ctrl+I",
                            triggered=self.import_logs_dialog))
        m.addSeparator()
        m.addAction(QAction(icons.icon("compare"), "&Back to analysis", self, triggered=self.back_to_analysis))
        m.addAction(QAction("&Quit", self, shortcut=QKeySequence.Quit, triggered=self.close))

        v = self.menuBar().addMenu("&View")
        pan = QAction("Pan and zoom plots &together", self, checkable=True, checked=self.pan_together)
        pan.toggled.connect(lambda on: setattr(self, "pan_together", bool(on)))
        v.addAction(pan)
        self.pan_act = pan
        theme_menu = v.addMenu("&Theme")
        group = QActionGroup(theme_menu)
        current = self.settings.get("theme", "dark")
        for mode, label in (("dark", "Dark"), ("light", "Light")):
            a = theme_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(mode == current)
            group.addAction(a)
            a.triggered.connect(lambda _c=False, mode=mode: self._set_theme(mode))
        v.addSeparator()
        for dw in self._fixed_docks:
            v.addAction(dw.toggleViewAction())
        v.addAction("&Reset layout", self.reset_layout)

        h = self.menuBar().addMenu("&Help")
        h.addAction("&About FlightLogger", self.show_about)
        self._make_shortcuts_global()

    def _make_shortcuts_global(self):
        """Ctrl+S & co. must work from floating docks too (separate windows)."""
        for a in self.findChildren(QAction):
            if not a.shortcut().isEmpty():
                a.setShortcutContext(Qt.ApplicationShortcut)

    def reset_layout(self):
        self.docks.mgr.restoreState(self._default_state)

    def show_about(self):
        from .. import IS_DEV, __version__
        if not self.interactive:
            return
        QMessageBox.about(self, f"About FlightLogger {__version__}" + (" (development)" if IS_DEV else ""),
                          f"<h3>FlightLogger {__version__}</h3><p>Comparison environment: the same "
                          "plots and parameters of several PX4 logs, aligned at takeoff.</p>")

    def _fill_recent_menu(self):
        self.recent_menu.clear()
        items = self.recent.list()
        if not items:
            self.recent_menu.addAction("(none)").setEnabled(False)
        for p in items:
            self.recent_menu.addAction(Path(p).name, lambda p=p: self.open_comparison(p))

    # --- logs ---
    def import_logs_dialog(self):
        if not self.interactive:
            return
        start = self.last_dirs.get("compare", Path.cwd())
        paths, _ = QFileDialog.getOpenFileNames(self, "Import PX4 logs", start,
                                                "PX4 ULog (*.ulg);;All files (*)")
        if paths:
            self.last_dirs.remember("compare", paths[0])
            self.import_logs(paths)

    def import_logs(self, paths) -> list[LogEntry]:
        entries = [self.logset.add(p) for p in paths]
        n_off = sum(not e.active for e in entries)
        self.status.setText(f"Imported {len(entries)} log(s)" + (
            f"; {n_off} not shown (at most {MAX_ACTIVE} at once)" if n_off else ""))
        return entries

    # --- files ---
    def new_comparison(self):
        if not self._maybe_save():
            return
        self.logset.clear()
        self.panels.clear()
        self._panel_dicts = []
        self.path = None
        self._saved_snapshot = self._snapshot()
        self._update_title()

    def open_comparison_dialog(self):
        if not self.interactive or not self._maybe_save():
            return
        start = self.last_dirs.get("comparison", self.last_dirs.get("compare", Path.cwd()))
        path, _ = QFileDialog.getOpenFileName(self, "Open comparison", start,
                                              f"Comparison (*{COMPARE_EXT});;All files (*)")
        if path:
            self.last_dirs.remember("comparison", path)
            self.open_comparison(path)

    def open_comparison(self, path: str | Path):
        try:
            data = load_comparison(path)
        except (OSError, ValueError) as e:
            self.notify(f"Could not open {Path(path).name}: {e}", warn=True)
            return False
        self.path = Path(path)
        self.logset.load_dict(data.get("logs") or [])
        self.pan_together = bool(data.get("pan_together", True))
        self.pan_act.setChecked(self.pan_together)
        self.load_panels(data.get("panels") or [])
        try:
            if data.get("dock_state"):
                self.docks.mgr.restoreState(QByteArray(unb64(data["dock_state"])))
            if data.get("geometry") and self.interactive:
                self.restoreGeometry(QByteArray(unb64(data["geometry"])))
        except Exception:  # noqa: BLE001 - a layout that no longer fits must not block opening
            pass
        self.recent.add(path)
        self._saved_snapshot = self._snapshot()
        self._update_title()
        moved = [Path(e["path"]).name for e in data["logs"] if e.get("relinked")]
        if moved:
            self.status.setText("Found elsewhere: " + ", ".join(moved))
        return True

    def save(self, ask: bool = False) -> bool:
        path = self.path
        if ask or path is None:
            if not self.interactive:
                return False
            start = self.last_dirs.get("comparison", Path.cwd())
            name = (path.name if path else f"comparison{COMPARE_EXT}")
            chosen, _ = QFileDialog.getSaveFileName(self, "Save comparison", str(Path(start) / name),
                                                    f"Comparison (*{COMPARE_EXT})")
            if not chosen:
                return False
            if not chosen.lower().endswith(COMPARE_EXT):
                chosen += COMPARE_EXT
            self.last_dirs.remember("comparison", chosen)
            path = Path(chosen)
        return self.save_to(path)

    def save_to(self, path: str | Path) -> bool:
        try:
            save_comparison(path, self.capture())
        except OSError as e:
            self.notify(f"Could not save: {e}", warn=True)
            return False
        self.path = Path(path)
        self.recent.add(path)
        self._saved_snapshot = self._snapshot()
        self._update_title()
        self.status.setText(f"Saved {self.path.name}")
        return True

    def _maybe_save(self) -> bool:
        """Ask to save unsaved work. False = the user cancelled."""
        if not (self.dirty and self.interactive and (len(self.logset) or self.panels)):
            return True
        r = QMessageBox.question(self, "Unsaved changes", "Save the comparison?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                                 QMessageBox.Save)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Save:
            return self.save()
        return True

    def closeEvent(self, ev):
        if not self._maybe_save():
            ev.ignore()
            return
        super().closeEvent(ev)

    # --- restarts ---
    def _restart(self, args: list[str]) -> bool:
        """Ask to save, then restart FlightLogger with args (only recorded when not interactive)."""
        if not self._maybe_save():
            return False
        self._restart_args = args
        if self.interactive:
            self._saved_snapshot = self._snapshot()   # the question was answered: close silently
            if self.close():
                QProcess.startDetached(sys.executable, ["-m", "flightlogger", *args],
                                       str(Path(__file__).resolve().parents[2]))
        return True

    def back_to_analysis(self) -> bool:
        recent = RecentProjects().list()
        return self._restart([recent[0]] if recent else [])

    def _set_theme(self, mode: str):
        if mode == ("dark" if theme.is_dark() else "light"):
            return
        previous = self.settings.get("theme", "dark")
        self.settings.set("theme", mode)
        if not self._restart(["--compare", *([str(self.path)] if self.path else [])]):
            self.settings.set("theme", previous)


def _title_suffix() -> str:
    from .. import TITLE_SUFFIX
    return TITLE_SUFFIX
