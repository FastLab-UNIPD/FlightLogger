"""Notes, project files and named layouts."""
import json
import shutil

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from flightlogger.notes.model import Note, NotesModel
from flightlogger.project import LayoutStore, load_project, save_project


# --- notes model -------------------------------------------------------------------
def test_notes_model_sorting_spans_and_roundtrip(qapp):
    m = NotesModel()
    fired = []
    m.changed.connect(lambda: fired.append(1))
    b = m.add(Note(t=50.0, title="b"))
    a = m.add(Note(t=10.0, title="a", category="Anomaly"))
    assert [n.title for n in m] == ["a", "b"]
    m.update(b.id, t_end=40.0)            # end before start -> swapped
    assert (m.get(b.id).t, m.get(b.id).t_end) == (40.0, 50.0)
    m2 = NotesModel()
    m2.load_list(json.loads(json.dumps(m.to_list())))
    assert [n.to_dict() for n in m2] == [n.to_dict() for n in m]
    m.remove(a.id)
    assert len(m) == 1 and len(fired) == 4


# --- window fixtures -----------------------------------------------------------------
@pytest.fixture
def make_window(qapp, tmp_path):
    from flightlogger.app import MainWindow
    made = []

    def make():
        w = MainWindow()
        w.interactive = False
        w.layouts = LayoutStore(tmp_path / "layouts")
        w.resize(1300, 850)
        w.show()
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()


@pytest.fixture
def window(make_window, synth_log):
    w = make_window()
    w._on_loaded(synth_log, "")
    return w


def test_n_key_adds_note_at_cursor(window, qapp):
    window.bus.seek(33.0)
    target = window.active_panel.view
    QApplication.sendEvent(target, QKeyEvent(QEvent.KeyPress, Qt.Key_N, Qt.NoModifier))
    notes = window.notes.notes
    assert len(notes) == 1 and notes[0].t == pytest.approx(33.0)
    assert window.notes_panel._current == notes[0].id
    assert window.isWindowModified()


def test_notes_panel_edit_seek_delete(window, qapp):
    panel = window.notes_panel
    window.bus.seek(20.0)
    n = window.add_note()
    panel._edit(title="Yaw wobble", category="Anomaly")
    window.bus.seek(25.0)
    panel.set_end_btn.click()
    note = window.notes.get(n.id)
    assert (note.title, note.category, note.t_end) == ("Yaw wobble", "Anomaly", 25.0)
    assert panel.table.item(0, 3).text() == "Yaw wobble"
    window.bus.seek(90.0)
    panel._on_click(0, 0)
    assert window.bus.t == pytest.approx(20.0)
    panel.delete_current()
    assert len(window.notes) == 0


def test_note_markers_on_graphs_and_timeline(window):
    window.notes.add(Note(t=15.0, title="Takeoff"))
    window.notes.add(Note(t=40.0, t_end=60.0, title="Oscillation"))
    time_plots = [p for p in window.active_panel.plots if p.time_based]
    assert all(len(p.note_items) == 2 for p in time_plots)
    assert all(len(p.note_items) == 0 for p in window.active_panel.plots if not p.time_based)
    assert len(window.timeline._note_items) == 2
    p2 = window.new_panel()                 # new panels get the markers too
    p2.add_plot(["battery_status:0/voltage_v"])
    assert len(p2.plots[0].note_items) == 2
    window.notes.clear()
    assert all(len(p.note_items) == 0 for p in time_plots)


# --- projects ------------------------------------------------------------------------
def _build_session(w):
    w.bus.seek(15.0)
    n = w.add_note()
    w.notes.update(n.id, title="Takeoff", category="Maneuver")
    w.notes.add(Note(t=40.0, t_end=60.0, title="Oscillation", text="0.5 Hz"))
    p2 = w.new_panel()
    p2.add_plot(["battery_status:0/voltage_v"])
    w.track_dw.setFloating()
    w.clock.set_offset(12.5)
    w.bus.set_x_range(30, 70, None)
    w.bus.seek(45.0)
    w.clock.set_speed(0.5)


def _wait_log(qapp, w):
    import time
    t0 = time.time()
    while w.log is None and time.time() - t0 < 10:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()


def test_project_roundtrip(make_window, qapp, tmp_path, synth_path):
    folder = tmp_path / "flight"
    folder.mkdir()
    log = folder / "flight.ulg"
    shutil.copy(synth_path, log)
    w = make_window()
    w.open_log(str(log))
    _wait_log(qapp, w)
    _build_session(w)
    proj = folder / "flight.flproj"
    assert w.save_project(proj)
    assert not w.isWindowModified() and "flight.flproj" in w.windowTitle()
    raw = json.loads(proj.read_text())
    assert raw["log"]["path"] == "flight.ulg" and raw["version"] == 1

    w2 = make_window()
    assert w2.open_project(proj)
    _wait_log(qapp, w2)
    assert [(n.title, n.t, n.t_end) for n in w2.notes] == [
        ("Takeoff", 15.0, None), ("Oscillation", 40.0, 60.0)]
    assert w2.bus.t == pytest.approx(45.0) and w2.bus.x_range == pytest.approx((30, 70))
    assert w2.clock.offset == pytest.approx(12.5) and w2.clock.speed == 0.5
    assert w2.video_panel.offset_spin.value() == pytest.approx(12.5)
    panels = {pid: [pl.to_dict() for pl in p.plots] for pid, (p, _) in w2.panels.items()}
    assert set(panels) == {"graphs_1", "graphs_2"}
    assert panels["graphs_2"] == [{"title": "", "signals": ["battery_status:0/voltage_v"]}]
    assert panels["graphs_1"][0] == {"graph": "roll_angle", "signals": []}
    assert w2.track_dw.isFloating()
    assert not w2.isWindowModified()
    assert w2.recent.list()[0] == str(proj.resolve())


def test_project_survives_moving(make_window, qapp, tmp_path, synth_path):
    a = tmp_path / "a"
    a.mkdir()
    shutil.copy(synth_path, a / "flight.ulg")
    w = make_window()
    w.open_log(str(a / "flight.ulg"))
    _wait_log(qapp, w)
    w.notes.add(Note(t=5.0, title="x"))
    assert w.save_project(a / "p.flproj")
    # the whole folder moved: relative path
    b = tmp_path / "b"
    shutil.copytree(a, b)
    assert load_project(b / "p.flproj")["log"]["path"] == str((b / "flight.ulg").resolve())
    # only the project moved: absolute path fallback
    c = tmp_path / "c"
    c.mkdir()
    shutil.copy(a / "p.flproj", c / "p.flproj")
    assert load_project(c / "p.flproj")["log"]["path"] == str((a / "flight.ulg").resolve())


def test_project_with_missing_log_still_opens(make_window, tmp_path):
    proj = tmp_path / "lost.flproj"
    save_project(proj, {"log": {"path": str(tmp_path / "gone.ulg")}, "video": None,
                        "notes": [Note(t=3.0, title="kept").to_dict()], "layout": {}})
    w = make_window()
    assert w.open_project(proj)
    assert w.log is None and [n.title for n in w.notes] == ["kept"]


def test_project_restores_video_and_sync(make_window, qapp, tmp_path, synth_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not decoded in this test")
    shutil.copy(synth_path, tmp_path / "f.ulg")
    w = make_window()
    w.open_log(str(tmp_path / "f.ulg"))
    _wait_log(qapp, w)
    w.video_panel._pending_path = str(video)   # as if the video were open
    w.clock.set_offset(43.6)
    assert w.save_project(tmp_path / "v.flproj")
    opened = []
    w2 = make_window()
    monkeypatch.setattr(w2, "open_video", lambda p: opened.append(p))
    w2.open_project(tmp_path / "v.flproj")
    _wait_log(qapp, w2)
    assert opened == [str(video.resolve())] and w2.clock.offset == pytest.approx(43.6)


def test_not_a_project(make_window, tmp_path):
    bad = tmp_path / "x.flproj"
    bad.write_text("{}")
    w = make_window()
    assert not w.open_project(bad)


# --- layouts ---------------------------------------------------------------------------
def test_layout_store(tmp_path):
    st = LayoutStore(tmp_path / "L")
    assert st.names() == [] and st.startup is None
    st.save("Dual screen", {"panels": []})
    st.save("b/ad:name", {"panels": [1]})
    assert st.names() == ["b_ad_name", "Dual screen"]
    st.startup = "Dual screen"
    assert st.startup == "Dual screen"
    st.delete("Dual screen")
    assert st.startup is None and st.names() == ["b_ad_name"]


def test_named_layout_apply_and_reset(window, qapp):
    p2 = window.new_panel()
    p2.add_plot(["battery_status:0/voltage_v"])
    window.track_dw.setFloating()
    window.layouts.save("Mine", window.capture_layout())
    window.apply_layout(window._default_layout)
    assert list(window.panels) == ["graphs_1"] and not window.track_dw.isFloating()
    assert len(window.active_panel.plots) == len(window.log.graphs)  # Flight Review page
    window.apply_named_layout("Mine")
    assert list(window.panels) == ["graphs_1", "graphs_2"] and window.track_dw.isFloating()
    assert window.panels["graphs_2"][0].plots[0].signals == ["battery_status:0/voltage_v"]
    window.bus.seek(50.0)                 # restored panels are live
    assert window.panels["graphs_2"][0].plots[0].cursor.value() == pytest.approx(50.0)


def test_layout_applied_before_log_waits_for_it(make_window, synth_log, tmp_path):
    w = make_window()
    layout = {"panels": [{"id": "graphs_7", "title": "Attitude",
                          "plots": [{"graph": "roll_angle", "signals": []},
                                    {"graph": "no_such_graph", "signals": []},
                                    {"title": "", "signals": ["battery_status:0/voltage_v"]}]}]}
    w.apply_layout(layout)
    assert w.panels["graphs_7"][0].has_content
    w._on_loaded(synth_log, "")
    panel = w.panels["graphs_7"][0]
    assert [pl.to_dict() for pl in panel.plots] == [
        {"graph": "roll_angle", "signals": []},
        {"title": "", "signals": ["battery_status:0/voltage_v"]}]   # missing graph skipped
    assert w.new_panel().panel_id == "graphs_8"


def test_startup_layout(qapp, tmp_path, monkeypatch, synth_log):
    from flightlogger import app as app_mod
    store = LayoutStore(tmp_path / "S")
    store.save("Start", {"panels": [{"id": "graphs_1", "title": "Only rates",
                                     "plots": [{"graph": "roll_angular_rate", "signals": []}]}]})
    store.startup = "Start"
    monkeypatch.setattr(app_mod, "LayoutStore", lambda: store)
    w = app_mod.MainWindow()
    w.interactive = False
    w._on_loaded(synth_log, "")
    assert [pl.graph.id for pl in w.active_panel.plots] == ["roll_angular_rate"]
    w.close()


def test_floating_window_on_missing_screen_comes_back(window, qapp):
    window.track_dw.setFloating()
    qapp.processEvents()
    fw = window.track_dw.floatingDockContainer()
    fw.setGeometry(50000, 50000, 400, 300)     # e.g. a monitor that is gone now
    window._rescue_offscreen_windows()
    screen = qapp.primaryScreen().availableGeometry()
    assert screen.intersects(fw.frameGeometry())
