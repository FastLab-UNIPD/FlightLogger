import pytest
from PySide6.QtCore import Qt


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


def panel_of(w):
    return w.active_panel


def test_default_page_is_flight_review(window, synth_log):
    p = panel_of(window)
    assert [pl.graph.id for pl in p.plots] == list(synth_log.graphs)
    checked = [window.graph_list.list.item(i).checkState() == Qt.Checked
               for i in range(window.graph_list.list.count())]
    assert all(checked) and len(checked) == len(synth_log.graphs)


def test_cursor_on_time_plots_only(window):
    window.bus.seek(42.0)
    for pl in panel_of(window).plots:
        if pl.time_based:
            assert pl.cursor.value() == pytest.approx(42.0)
        else:
            assert pl.cursor is None
    assert window.timeline.cursor.value() == pytest.approx(42.0)


def test_seek_is_clamped(window):
    window.bus.seek(-5)
    assert window.bus.t == 0
    window.bus.seek(1e6)
    assert window.bus.t == pytest.approx(window.log.duration)


def test_graph_toggle_keeps_flight_review_order(window, synth_log):
    p = panel_of(window)
    window._on_graph_toggled("velocity", False)
    assert not p.has_graph("velocity")
    window._on_graph_toggled("velocity", True)
    assert [pl.graph.id for pl in p.plots] == list(synth_log.graphs)
    window._on_all_graphs(False)
    assert p.plots == []
    window._on_all_graphs(True)
    assert len(p.plots) == len(synth_log.graphs)


def test_linked_panels_share_range(window):
    p2 = window.new_panel()
    p2.add_plot(["vehicle_local_position:0/z"])
    window.bus.set_x_range(20, 30, None)
    for panel, _ in window.panels.values():
        assert panel.visible_range() == pytest.approx((20, 30))
    p2.link_btn.setChecked(False)
    window.bus.set_x_range(50, 60, None)
    assert p2.visible_range() == pytest.approx((20, 30))


def test_offscreen_plots_catch_up_when_scrolled(window, qapp):
    p = panel_of(window)
    window.bus.set_x_range(33, 44, None)
    qapp.processEvents()
    p.scroll.verticalScrollBar().setValue(p.scroll.verticalScrollBar().maximum())
    qapp.processEvents()
    vis = p._visible_plots()
    assert vis and all(pl.vb.viewRange()[0] == pytest.approx([33, 44]) for pl in vis)


def test_follow_pages_view(window):
    window.bus.set_x_range(0, 10, None)
    window.bus.seek(55.0)
    x0, x1 = panel_of(window).visible_range()
    assert x0 <= 55.0 <= x1 and x1 - x0 == pytest.approx(10)


def test_signals_on_flight_review_graph_roundtrip(window):
    p = panel_of(window)
    roll = p.plots[0]
    p.drop_signals(["battery_status:0/voltage_v"], roll)
    assert roll.signals == ["battery_status:0/voltage_v"]
    d = p.to_dict()
    assert d["plots"][0] == {"graph": "roll_angle", "signals": ["battery_status:0/voltage_v"]}
    p2 = window.new_panel()
    p2.load_dict(d)
    assert [pl.to_dict() for pl in p2.plots] == d["plots"]


def test_custom_plots(window):
    p = panel_of(window)
    n = len(p.plots)
    p.drop_signals(["battery_status:0/voltage_v"], None)
    assert len(p.plots) == n + 1 and p.plots[-1].graph is None
    p.drop_signals(["battery_status:0/current_a"], p.plots[-1])
    assert p.plots[-1].signals == ["battery_status:0/voltage_v", "battery_status:0/current_a"]
    p.remove_plot(p.plots[-1])
    assert len(p.plots) == n


def test_messages_click_seeks(window):
    m = window.messages
    assert m.table.rowCount() == 5
    m._on_click(2, 0)  # "High vibration level" at 45 s
    assert window.bus.t == pytest.approx(45.0)
    assert m.table.item(2, 1).text() == "WARNING"


def test_params_table(window):
    t = window.params.table
    names = [t.item(r, 0).text() for r in range(t.rowCount())]
    assert names == ["MAV_TYPE", "MC_ROLL_P"]
    assert not window.params.changed_only.isEnabled()  # synthetic log has no defaults


def test_speed_presets(window):
    c = window.clock
    c.set_speed(1.0)
    c.slower(); c.slower()
    assert c.speed == 0.25
    c.faster()
    assert c.speed == 0.5
    assert window.speed_box.currentData() == 0.5


def test_real_log_window(qapp, real_log):
    from flightlogger.app import MainWindow
    w = MainWindow()
    w._on_loaded(real_log, "")
    assert len(w.active_panel.plots) == len(real_log.graphs)
    w.bus.seek(60.0)
    w.close()
