"""Opt-in diagnostics for docking problems: set FLIGHTLOGGER_DEBUG_DOCK=/path/to/log.

Logs every change in the state of the dock widgets and torn-off windows (floating,
closed, visible, geometry, screen) and in the X11 window stack, with timestamps.
"""
from __future__ import annotations

import os
import subprocess
import time

from PySide6.QtCore import QObject, QTimer


class DockDebug(QObject):
    def __init__(self, win, path: str):
        super().__init__(win)
        self.win = win
        self.f = open(path, "a", buffering=1, encoding="utf-8")
        self.t0 = time.time()
        self.last: dict[str, str] = {}
        self.last_stack = ""
        self.log("=== dock debug started ===")
        mgr = win.docks.mgr
        mgr.floatingWidgetCreated.connect(lambda fw: self.log(f"EVENT floatingWidgetCreated {self._fc(fw)}"))
        for name in ("dockWidgetAdded", "dockWidgetAboutToBeRemoved", "dockWidgetRemoved",
                     "dockAreasAdded", "dockAreasRemoved", "focusedDockWidgetChanged"):
            sig = getattr(mgr, name, None)
            if sig is not None:
                sig.connect(lambda *a, n=name: self.log(f"EVENT {n} {[getattr(x, 'objectName', lambda: x)() if hasattr(x, 'objectName') else x for x in a]}"))
        self.timer = QTimer(self, interval=250)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def log(self, msg: str):
        self.f.write(f"{time.time() - self.t0:8.2f}  {msg}\n")

    @staticmethod
    def _fc(fw) -> str:
        try:
            g = fw.frameGeometry()
            scr = fw.screen().name() if fw.screen() else "?"
            return (f"visible={fw.isVisible()} geom=({g.x()},{g.y()},{g.width()},{g.height()}) "
                    f"screen={scr} docks={[d.objectName() for d in fw.dockWidgets()]} "
                    f"flags={int(fw.windowFlags()):#x} minimized={fw.isMinimized()} "
                    f"qtstate={int(fw.windowState().value) if hasattr(fw.windowState(), 'value') else fw.windowState()} "
                    f"exposed={fw.windowHandle().isExposed() if fw.windowHandle() else None} "
                    f"x={DockDebug._xstate(fw)}")
        except RuntimeError:
            return "(deleted)"

    @staticmethod
    def _xstate(fw) -> str:
        """Real X11 state of the window: map state and ICCCM WM_STATE."""
        try:
            wid = str(int(fw.winId()))
            info = subprocess.run(["xwininfo", "-id", wid], capture_output=True, text=True,
                                  timeout=1).stdout
            ms = [l.split(":")[-1].strip() for l in info.split("\n") if "Map State" in l]
            wm = subprocess.run(["xprop", "-id", wid, "WM_STATE", "_NET_WM_STATE"],
                                capture_output=True, text=True, timeout=1).stdout
            st = "Iconic" if "Iconic" in wm else "Normal" if "Normal" in wm else "?"
            hidden = "+HIDDEN" if "HIDDEN" in wm else ""
            return f"{ms[0] if ms else '?'}/{st}{hidden}"
        except Exception:  # noqa: BLE001 - diagnostics only
            return "?"

    def poll(self):
        w = self.win
        for dw in w.docks.mgr.dockWidgetsMap().values():
            fc = dw.floatingDockContainer() if dw.isFloating() else None
            state = (f"closed={dw.isClosed()} floating={dw.isFloating()} visible={dw.isVisible()} "
                     f"area={'yes' if dw.dockAreaWidget() else 'NONE'} "
                     f"container={'-' if fc is None else self._fc(fc)}")
            if self.last.get(dw.objectName()) != state:
                self.last[dw.objectName()] = state
                self.log(f"STATE {dw.objectName():12s} {state}")
        self.log_stack()

    def log_stack(self):
        try:
            out = subprocess.run(["xprop", "-root", "_NET_CLIENT_LIST_STACKING"],
                                 capture_output=True, text=True, timeout=1).stdout
            ids = [x.strip() for x in out.split("#")[1].split(",")]
            names = []
            for i in ids:
                p = subprocess.run(["xprop", "-id", i, "WM_NAME", "WM_CLASS", "_NET_WM_STATE"],
                                   capture_output=True, text=True, timeout=1).stdout
                if "flightlogger" not in p.lower() and "FlightLogger" not in p:
                    continue
                nm = p.split("\n")[0].split("=")[-1].strip()
                st = [l for l in p.split("\n") if "_NET_WM_STATE" in l]
                tags = []
                for t in ("HIDDEN", "FULLSCREEN", "MAXIMIZED", "ABOVE", "SKIP_TASKBAR"):
                    if st and t in st[0]:
                        tags.append(t)
                names.append(f"{nm}[{','.join(tags)}]")
            stack = " < ".join(names)
        except Exception as e:  # noqa: BLE001 - diagnostics only
            stack = f"(xprop failed: {e})"
        if stack != self.last_stack:
            self.last_stack = stack
            self.log(f"STACK (bottom -> top) {stack}")


def install(win):
    path = os.environ.get("FLIGHTLOGGER_DEBUG_DOCK")
    if path:
        win._dock_debug = DockDebug(win, path)
