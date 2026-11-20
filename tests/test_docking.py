"""Docking: graph panels and fixed docks (headless), video docking (real display).

The video part needs a real display: OpenGL does not render on Qt's offscreen
platform. It runs tests/video_dock_check.py in a subprocess on $DISPLAY and is
skipped without one, or when FLIGHTLOGGER_NO_DISPLAY_TESTS is set (it opens
windows on screen for ~15 s).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import PySide6QtAds as QtAds
import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from flightlogger.video.mpv_widget import mpv_available

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def window(qapp, synth_log):
    from flightlogger.app import MainWindow
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    w._on_loaded(synth_log, "")
    qapp.processEvents()
    yield w
    w.close()


def _first(w):
    return next(iter(w.panels.values()))


def test_new_panel_is_side_by_side(window):
    p1, dw1 = _first(window)
    p2 = window.new_panel()
    dw2 = window.panels[p2.panel_id][1]
    assert dw1.dockAreaWidget() is not dw2.dockAreaWidget()
    assert dw1.dockContainer() is dw2.dockContainer()
    assert window.active_panel is p2


def test_floating_panel_follows_cursor_and_linked_range(window, qapp):
    p1, _ = _first(window)
    p2 = window.new_panel()
    p2.add_plot(["vehicle_local_position:0/z"])
    dw2 = window.panels[p2.panel_id][1]
    dw2.setFloating()
    qapp.processEvents()
    assert dw2.isFloating()
    window.bus.seek(33.0)
    assert p2.plots[0].cursor.value() == pytest.approx(33.0)
    window.bus.set_x_range(10, 20, None)
    assert p2.visible_range() == pytest.approx((10, 20))
    # zooming the floating panel moves the docked one too
    p2.plots[0].vb.setXRange(40, 50, padding=0)
    assert p1.visible_range() == pytest.approx((40, 50))


def test_keys_work_in_floating_window(window, qapp):
    p2 = window.new_panel()
    dw2 = window.panels[p2.panel_id][1]
    dw2.setFloating()
    qapp.processEvents()
    target = p2.view
    target.setFocus()
    window.bus.seek(10.0)
    QApplication.sendEvent(target, QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.ShiftModifier))
    assert window.bus.t == pytest.approx(11.0)
    QApplication.sendEvent(target, QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier))
    assert window.clock.playing
    window.clock.pause()


def test_redock_floating_panel(window, qapp):
    p2 = window.new_panel()
    dw2 = window.panels[p2.panel_id][1]
    dw2.setFloating()
    qapp.processEvents()
    window.docks.mgr.addDockWidget(QtAds.RightDockWidgetArea, dw2)
    qapp.processEvents()
    assert not dw2.isFloating()
    window.bus.seek(44.0)
    assert all(pl.cursor.value() == pytest.approx(44.0) for pl in p2.plots if pl.cursor)


def test_tabbed_panels_keep_updating(window, qapp):
    p1, dw1 = _first(window)
    p2 = window.new_panel()
    p2.add_plot(["battery_status:0/voltage_v"])
    dw2 = window.panels[p2.panel_id][1]
    window.docks.mgr.addDockWidgetTabToArea(dw2, dw1.dockAreaWidget())
    dw1.setAsCurrentTab()
    qapp.processEvents()
    assert dw1.dockAreaWidget() is dw2.dockAreaWidget()
    window.bus.seek(55.0)              # hidden tab still follows
    dw2.setAsCurrentTab()
    qapp.processEvents()
    assert p2.plots[0].cursor.value() == pytest.approx(55.0)


def test_closing_panel_removes_it(window, qapp):
    p1, _ = _first(window)
    p2 = window.new_panel()
    dw2 = window.panels[p2.panel_id][1]
    dw2.closeDockWidget()
    qapp.processEvents()
    assert p2.panel_id not in window.panels
    assert window.active_panel is p1
    window.bus.seek(12.0)              # no stale connections to the deleted panel


def test_fixed_docks_close_and_reopen(window, qapp):
    for dw in window._fixed_docks:
        dw.toggleView(False)
        qapp.processEvents()
        assert dw.isClosed()
        dw.toggleView(True)
        qapp.processEvents()
        assert not dw.isClosed()
    window.bus.seek(20.0)
    assert window.timeline.cursor.value() == pytest.approx(20.0)


def test_video_dock_is_not_floatable(window, qapp):
    F = QtAds.CDockWidget.DockWidgetFeature
    dw = window.video_dw
    assert not dw.features() & F.DockWidgetFloatable
    assert dw.features() & F.DockWidgetMovable and dw.features() & F.DockWidgetClosable
    dw.setFloating()
    qapp.processEvents()
    assert not dw.isFloating()
    # it can still move to another side of the main window
    window.docks.mgr.addDockWidget(QtAds.RightDockWidgetArea, dw)
    qapp.processEvents()
    assert not dw.isFloating() and dw.dockContainer() is window.graphs_dw.dockContainer()


def test_panels_in_separate_windows(window, qapp):
    p1, panel_dw = _first(window)
    window.track_dw.setFloating()
    window.messages_dw.setFloating()
    panel_dw.setFloating()
    # two docks sharing one floating window
    window.docks.mgr.addDockWidgetTabToArea(window.params_dw,
                                            window.messages_dw.dockAreaWidget())
    qapp.processEvents()
    tops = {dw.floatingDockContainer() for dw in
            (window.track_dw, window.messages_dw, panel_dw, window.params_dw)}
    assert None not in tops and len(tops) == 3
    assert window.params_dw.floatingDockContainer() is window.messages_dw.floatingDockContainer()
    # everything keeps following the cursor from its own window
    window.bus.seek(61.0)
    assert all(pl.cursor.value() == pytest.approx(61.0) for pl in p1.plots if pl.cursor)
    window.messages._on_click(2, 0)
    assert window.bus.t == pytest.approx(45.0)
    # and the graph checklist still drives the floating panel
    window._on_graph_toggled("velocity", False)
    assert not p1.has_graph("velocity")


def test_layout_state_roundtrip(window, qapp):
    state = window.docks.mgr.saveState()
    window.track_dw.setFloating()
    window.messages_dw.toggleView(False)
    qapp.processEvents()
    assert window.track_dw.isFloating() and window.messages_dw.isClosed()
    assert window.docks.mgr.restoreState(state)
    qapp.processEvents()
    assert not window.track_dw.isFloating() and not window.messages_dw.isClosed()


# --- video docking on a real display -------------------------------------------------
def _display_ok():
    if os.environ.get("FLIGHTLOGGER_NO_DISPLAY_TESTS"):
        return False, "FLIGHTLOGGER_NO_DISPLAY_TESTS is set"
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False, "no display"
    ok, msg = mpv_available()
    return ok, msg


def test_video_survives_docking_moves(clips, test_data_dir_optional):
    ok, why = _display_ok()
    if not ok:
        pytest.skip(why)
    vids = sorted(test_data_dir_optional.glob("*.mp4")) if test_data_dir_optional else []
    logs = sorted(test_data_dir_optional.glob("*.ulg")) if test_data_dir_optional else []
    video = str(vids[0] if vids else clips["h264.mp4"])
    args = [video] + ([str(logs[0])] if logs and vids else [])
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)  # real display platform (xcb / wayland)
    r = subprocess.run([sys.executable, str(ROOT / "tests" / "video_dock_check.py"), *args],
                       capture_output=True, text=True, timeout=300, env=env)
    steps = {s["step"]: s for s in (json.loads(l) for l in r.stdout.splitlines()
                                    if l.startswith("{"))}
    expected = ["docked", "resized", "moved right", "moved back left", "tab hidden and shown",
                "closed and reopened", "float refused", "others floating", "fullscreen",
                "fullscreen exited"]
    assert list(steps) == expected, r.stdout + r.stderr
    for s in steps.values():
        assert not s["black"], s          # picture on screen
        assert s["changing"], s           # frames keep being drawn while playing
        assert not s["floating"], s       # the video dock never tears off
        if args[1:]:
            # cursor sits on the shown frame (log = video - offset)
            assert s["video"] - s["offset"] == pytest.approx(s["log"], abs=0.05), s
    assert steps["resized"]["size"] != steps["docked"]["size"]
