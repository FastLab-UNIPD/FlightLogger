"""Run the real app on a real display and exercise video docking.

Called by test_docking.py in a subprocess (OpenGL needs a real display; the
offscreen Qt platform cannot render it). Prints one JSON object per step:
  {"step": ..., "black": bool, "changing": bool, "presented": n,
   "video": t, "log": t, "offset": s, "floating": bool, ...}

"black" comes from the pixels on screen under the video; "changing" from the
number of frames mpv actually drew during 0.9 s of playback.

Usage: python tests/video_dock_check.py VIDEO [LOG]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pyqtgraph as pg  # noqa: E402
import PySide6QtAds as QtAds  # noqa: E402
from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def main(video: str, log: str | None):
    pg.setConfigOptions(background="w", foreground="k")
    app = QApplication(sys.argv)
    from flightlogger.app import MainWindow
    from flightlogger.log.ulog_loader import FlightLog

    w = MainWindow()
    scr = app.primaryScreen().availableGeometry()
    w.setGeometry(scr.x(), scr.y(), int(scr.width() * 0.6), int(scr.height() * 0.85))
    # keep our window above other apps (a window opened by a script may be
    # stacked behind them, which would hide it from the screen capture)
    w.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    w.show()

    def wait(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    if log:
        w._on_loaded(FlightLog(log), "")
    w.open_video(video)
    for _ in range(80):
        wait(100)
        if w.clock.has_video:
            break
    v = w.video_panel.video
    pb = w.clock
    start = min(30.0, v.info.duration / 3)

    def frame():
        # the pixels actually on screen under the video surface
        app.processEvents()
        top_left = v.mapToGlobal(v.rect().topLeft())
        screen = app.screenAt(top_left) or app.primaryScreen()
        g = screen.geometry()
        pm = screen.grabWindow(0, top_left.x() - g.x(), top_left.y() - g.y(), v.width(), v.height())
        img = pm.toImage().convertToFormat(QImage.Format.Format_RGB32)
        a = np.frombuffer(img.constBits(), np.uint8, img.sizeInBytes()).reshape(
            img.height(), img.bytesPerLine())
        # copy: the array must not outlive the QImage's buffer
        return a[:, : img.width() * 4].reshape(img.height(), img.width(), 4)[..., :3].copy()

    def report(step, settle_ms=700):
        top = v.window()
        if top is not w and not top.windowFlags() & Qt.WindowStaysOnTopHint:
            top.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            top.show()
        top.raise_()
        pb.seek_video(start)
        wait(settle_ms)
        f1 = frame()
        n0 = v.frames_presented
        pb.play()
        wait(900)
        pb.pause()
        wait(250)
        presented = v.frames_presented - n0
        out = {"step": step, "black": bool(f1.mean() < 8),
               # frames keep being drawn (a frozen/black surface draws none); a
               # full page of graphs can halve the drawn rate, so no fps target
               "changing": presented >= max(4, 0.15 * 0.9 * v.info.fps), "presented": presented,
               "video": v.time_pos, "log": w.bus.t, "offset": pb.offset,
               "floating": w.video_dw.isFloating(), "size": [v.width(), v.height()]}
        print(json.dumps(out), flush=True)

    mgr = w.docks.mgr
    report("docked")
    # resize the video dock (splitter between it and the panel next to it)
    mgr.setSplitterSizes(w.video_dw.dockAreaWidget(), [600, 300])
    report("resized")
    # move it to the right side of the main window and back
    mgr.addDockWidget(QtAds.RightDockWidgetArea, w.video_dw)
    report("moved right")
    mgr.addDockWidget(QtAds.LeftDockWidgetArea, w.video_dw)
    report("moved back left")
    # tabbed behind another dock, then shown again
    mgr.addDockWidgetTabToArea(w.track_dw, w.video_dw.dockAreaWidget())
    w.track_dw.setAsCurrentTab()
    wait(300)
    w.video_dw.setAsCurrentTab()
    report("tab hidden and shown")
    # closed and reopened from the View menu
    w.video_dw.toggleView(False)
    wait(300)
    w.video_dw.toggleView(True)
    report("closed and reopened")
    # tearing the video off is refused (not floatable)
    w.video_dw.setFloating()
    wait(300)
    report("float refused")
    # other panels in separate windows; the video keeps playing in the main one
    w.track_dw.setFloating()
    w.messages_dw.setFloating()
    next(iter(w.panels.values()))[1].setFloating()
    mgr.addDockWidgetTabToArea(w.params_dw, w.messages_dw.dockAreaWidget())
    report("others floating")
    # fullscreen and back (GL context is recreated twice: decoder restart)
    w.video_panel.toggle_fullscreen()
    report("fullscreen", settle_ms=1500)
    w.video_panel.exit_fullscreen()
    report("fullscreen exited", settle_ms=1500)
    w.close()
    app.quit()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
