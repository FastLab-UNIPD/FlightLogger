import numpy as np
import pytest

import synth_ulg
from flightlogger.log.ulog_loader import SignalKey, quat_to_euler_deg
from flightlogger.plots.common import fmt_time


def test_signal_key_roundtrip():
    k = SignalKey.parse("sensor_accel:1/x")
    assert (k.topic, k.instance, k.field) == ("sensor_accel", 1, "x")
    assert str(k) == "sensor_accel:1/x"
    assert k.label == "sensor_accel[1].x"
    assert SignalKey.parse("vehicle_attitude:0/q[2]").field == "q[2]"


def test_quat_to_euler():
    q = synth_ulg._quat(np.radians(10), np.radians(-20), np.radians(30))
    r, p, y = quat_to_euler_deg(*map(np.float64, q))
    assert (r, p, y) == pytest.approx((10, -20, 30), abs=1e-6)


def test_duration_and_time_base(synth_log):
    assert synth_log.duration == pytest.approx(synth_ulg.END_T, abs=0.02)
    t, _ = synth_log.get("vehicle_attitude:0/q[0]")
    assert t[0] == pytest.approx(0.0)
    assert np.all(np.diff(t) > 0)


def test_derived_roll_matches_script(synth_log):
    t, roll = synth_log.get("vehicle_attitude:0/roll_deg")
    i = np.searchsorted(t, 40.5)
    assert roll[i] == pytest.approx(np.degrees(synth_ulg.roll_of(t[i])), abs=1e-3)
    assert synth_log.has_signal("vehicle_attitude_setpoint:0/roll_deg")
    assert not synth_log.has_signal("vehicle_attitude:0/nope")
    with pytest.raises(KeyError):
        synth_log.get("missing_topic:0/x")


def test_multi_instance(synth_log):
    assert synth_log.instances("sensor_accel") == [0, 1]
    assert len(synth_log.get("sensor_accel:1/z")[0]) == 6000


def test_events_and_modes(synth_log):
    ev = [(e.kind, round(e.t)) for e in synth_log.events()]
    assert ev == [("armed", 10), ("takeoff", 15), ("landed", 100), ("disarmed", 105)]
    modes = [(s.name, round(s.t0)) for s in synth_log.mode_segments()]
    assert modes == [("Position", 0), ("Altitude", 30), ("Mission", 70), ("Land", 90)]


def test_messages_and_params(synth_log):
    msgs = synth_log.messages()
    assert msgs[2].level == "WARNING" and msgs[2].t == pytest.approx(45.0)
    assert synth_log.params["MC_ROLL_P"] == pytest.approx(6.5)


def test_fmt_time():
    assert fmt_time(0) == "0:00.000"
    assert fmt_time(83.25, 1) == "1:23.2"
    assert fmt_time(3725.5, 0) == "1:02:06"
    assert fmt_time(59.9996) == "1:00.000"
    assert fmt_time(-1.5, 1) == "-0:01.5"


def test_real_log_duration_is_sane(real_log):
    # last_timestamp of real logs can be corrupt; duration must stay plausible
    assert 0 < real_log.duration < 24 * 3600
    assert real_log.topics()
