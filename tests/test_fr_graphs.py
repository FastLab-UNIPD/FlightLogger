import numpy as np
import pytest

import synth_ulg
from flightlogger.log import fr_graphs
from flightlogger.log.fr_graphs import _Ctx, _Plot, build_graphs, slug


def test_synthetic_graph_set(synth_log):
    ids = list(synth_log.graphs)
    assert ids == ["roll_angle", "roll_angular_rate", "pitch_angle", "pitch_angular_rate",
                   "yaw_angle", "yaw_angular_rate", "local_position_x", "local_position_y",
                   "local_position_z", "velocity", "angular_velocity_fft",
                   "angular_velocity_power_spectral_density"]


def test_roll_angle_matches_flight_review_math(synth_log):
    g = synth_log.graphs["roll_angle"]
    est, sp = g.series
    assert (est.label, sp.label) == ("Roll Estimated", "Roll Setpoint")
    assert est.color == fr_graphs.colors3[0] and sp.step and not est.step
    i = np.searchsorted(est.t, 41.0)
    assert est.y[i] == pytest.approx(np.degrees(synth_ulg.roll_of(est.t[i])), abs=1e-3)
    assert g.y_label == "[deg]" and g.mode_background


def test_fft_and_spectrogram(synth_log):
    fft = synth_log.graphs["angular_velocity_fft"]
    assert fft.kind == "fft" and not fft.time_based and fft.y_range == (0, 0.01)
    assert fft.series[0].label.startswith("Rollspeed (mean above 40 Hz")
    assert fft.series[0].t.max() <= 50.0  # Nyquist of the 100 Hz topic
    spec = synth_log.graphs["angular_velocity_power_spectral_density"]
    assert spec.kind == "spectrogram" and spec.time_based and not spec.mode_background
    sp = spec.spectrogram
    assert sp.image.shape[0] == 129 and sp.f1 == pytest.approx(50.0)
    assert 0 <= sp.t0 < sp.t1 <= synth_log.duration + 1
    assert spec.title.endswith("rollspeed pitchspeed yawspeed [dB]")


def test_dataplot_error_semantics(synth_log):
    """Like Flight Review: a missing first topic does not drop the plot."""
    ctx = _Ctx(synth_log)
    p = _Plot(ctx, "no_such_topic", "Test Plot")
    p.add_graph(["x"], ["#000"], ["missing"])
    p.change_dataset("battery_status")
    p.add_graph(["voltage_v"], ["#111"], ["V"])
    p.change_dataset("another_missing")
    p.add_graph(["x"], ["#222"], ["missing too"])
    g = p.finalize()
    assert g is not None and [s.label for s in g.series] == ["V"]
    # nothing at all -> no plot
    p = _Plot(ctx, "no_such_topic", "Empty")
    p.add_graph(["x"], ["#000"], ["missing"])
    assert p.finalize() is None


def test_fifo_expansion_matches_flight_review_loop():
    rng = np.random.default_rng(1)
    n_rows, n_fields = 5, 4
    samples = np.array([4, 2, 3, 4, 1])
    d = {"timestamp_sample": np.arange(n_rows, dtype=np.uint64) * 1000 + 10_000,
         "dt": np.full(n_rows, 250.0), "samples": samples, "scale": rng.uniform(0.5, 2, n_rows)}
    for a in "xyz":
        for s in range(n_fields):
            d[f"{a}[{s}]"] = rng.integers(-100, 100, n_rows).astype(np.int16)
    d["timestamp"] = d["timestamp_sample"]

    class FakeLog:
        t0_us = 0
        def has(self, t, i=0): return t == "sensor_accel_fifo"
        def instances(self, t): return [0] if t == "sensor_accel_fifo" else []
        def dataset(self, t, i=0):
            return type("D", (), {"data": d})()
        ulog = None

    ctx = _Ctx.__new__(_Ctx)
    ctx.fl, ctx.virtual, ctx._cache = FakeLog(), {}, {}
    assert ctx.add_virtual_fifo_topic_data("sensor_accel_fifo")
    out = ctx.virtual[("sensor_accel_fifo_virtual", 0)]
    # reference: Flight Review's original nested loop
    t_ref, x_ref = [], []
    for i in range(n_rows):
        for s in range(samples[i]):
            t_ref.append(d["timestamp_sample"][i] - (samples[i] - s - 1) * d["dt"][i])
            x_ref.append(d[f"x[{s}]"][i] * d["scale"][i])
    np.testing.assert_allclose(out["timestamp"], t_ref)
    np.testing.assert_allclose(out["x"], x_ref)


def test_slug():
    assert slug("Manual Control Inputs (Radio or Joystick)") == "manual_control_inputs_radio_or_joystick"
    assert slug("CPU & RAM") == "cpu_ram"


def test_real_log_graphs(real_log):
    graphs = build_graphs(real_log)
    ids = [g.id for g in graphs]
    assert len(ids) == len(set(ids)) and ids[0] in ("altitude_estimate", "roll_angle")
    for g in graphs:
        assert g.series or g.spectrogram is not None
        for s in g.series:
            assert len(s.t) == len(s.y)
