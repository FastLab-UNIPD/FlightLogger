"""Load a PX4 .ulg file and expose its topics as time series in seconds.

Signal keys have the form ``"<topic>:<instance>/<field>"``, e.g.
``"vehicle_attitude:0/roll_deg"`` or ``"sensor_accel:1/x"``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from pyulog import ULog

# PX4 vehicle_status.nav_state names (as shown by Flight Review)
NAV_STATE_NAMES = {
    0: "Manual", 1: "Altitude", 2: "Position", 3: "Mission", 4: "Hold", 5: "RTL",
    6: "Position Slow", 7: "Free5", 8: "Free4", 9: "Free3", 10: "Acro", 11: "Free2",
    12: "Descend", 13: "Termination", 14: "Offboard", 15: "Stabilized", 16: "Free1",
    17: "Takeoff", 18: "Land", 19: "Follow Target", 20: "Precision Land", 21: "Orbit",
    22: "VTOL Takeoff",
}

ARMING_STATE_ARMED = 2


@dataclass(frozen=True)
class SignalKey:
    topic: str
    instance: int
    field: str

    @classmethod
    def parse(cls, key: str) -> "SignalKey":
        head, fld = key.split("/", 1)
        topic, inst = head.rsplit(":", 1)
        return cls(topic, int(inst), fld)

    def __str__(self) -> str:
        return f"{self.topic}:{self.instance}/{self.field}"

    @property
    def label(self) -> str:
        inst = f"[{self.instance}]" if self.instance else ""
        return f"{self.topic}{inst}.{self.field}"


@dataclass
class TopicInfo:
    name: str
    instance: int
    fields: list[str]
    derived: list[str]
    n_samples: int
    rate_hz: float


@dataclass
class LogEvent:
    t: float
    kind: str  # "armed", "disarmed", "takeoff", "landed"
    label: str


@dataclass
class LogMessage:
    t: float
    level: str
    text: str


@dataclass
class ModeSegment:
    t0: float
    t1: float
    nav_state: int

    @property
    def name(self) -> str:
        from .fr_graphs import flight_modes_table  # Flight Review's names
        entry = flight_modes_table.get(self.nav_state)
        return entry[0] if entry else NAV_STATE_NAMES.get(self.nav_state, f"Mode {self.nav_state}")


# --- derived signals ---------------------------------------------------------

def quat_to_euler_deg(q0, q1, q2, q3):
    """PX4 Hamilton quaternion [w, x, y, z] -> roll, pitch, yaw in degrees."""
    roll = np.arctan2(2 * (q0 * q1 + q2 * q3), 1 - 2 * (q1 * q1 + q2 * q2))
    pitch = np.arcsin(np.clip(2 * (q0 * q2 - q3 * q1), -1, 1))
    yaw = np.arctan2(2 * (q0 * q3 + q1 * q2), 1 - 2 * (q2 * q2 + q3 * q3))
    return np.degrees(roll), np.degrees(pitch), np.degrees(yaw)


def _euler_from(prefix: str) -> dict[str, Callable[[dict], np.ndarray]]:
    names = [f"{prefix}[{i}]" for i in range(4)]

    def make(idx):
        def fn(d):
            return quat_to_euler_deg(*(d[n].astype(np.float64) for n in names))[idx]
        return fn

    return {"roll_deg": make(0), "pitch_deg": make(1), "yaw_deg": make(2)}, names


def _deg_from(fields: list[str], out: list[str]):
    return {o: (lambda d, f=f: np.degrees(d[f].astype(np.float64))) for f, o in zip(fields, out)}, fields


# topic -> (derived fields, required source fields)
DERIVED: dict[str, tuple[dict, list[str]]] = {
    "vehicle_attitude": _euler_from("q"),
    "vehicle_attitude_groundtruth": _euler_from("q"),
    "vehicle_attitude_setpoint": _euler_from("q_d"),
    "vehicle_angular_velocity": _deg_from(["xyz[0]", "xyz[1]", "xyz[2]"],
                                          ["roll_rate_deg", "pitch_rate_deg", "yaw_rate_deg"]),
    "vehicle_rates_setpoint": _deg_from(["roll", "pitch", "yaw"],
                                        ["roll_rate_deg", "pitch_rate_deg", "yaw_rate_deg"]),
}


class FlightLog:
    """A parsed .ulg file with time in seconds since log start."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.ulog = ULog(str(self.path))
        self.t0_us = int(self.ulog.start_timestamp)
        self._ds = {(d.name, d.multi_id): d for d in self.ulog.data_list}
        self._cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.duration = self._robust_duration()
        self._graphs = None
        self._track = False

    # --- Flight Review graphs (computed once, on first use) ---
    @property
    def graphs(self) -> dict:
        """Ordered {graph_id: GraphDef} of Flight Review's graphs for this log."""
        if self._graphs is None:
            from .fr_graphs import build_graphs
            self._graphs = {g.id: g for g in build_graphs(self)}
        return self._graphs

    @property
    def track(self):
        if self._track is False:
            from .fr_graphs import build_track
            self._track = build_track(self)
        return self._track

    def _robust_duration(self) -> float:
        """Log length in seconds.

        ``ULog.last_timestamp`` can be garbage (one corrupt message is enough), and
        single topics sometimes carry bogus timestamps. Use the high-rate topics as
        reference and ignore any topic that ends far beyond them.
        """
        lasts = []  # (n_samples, last_t)
        for d in self._ds.values():
            ts = d.data["timestamp"]
            if len(ts):
                lasts.append((len(ts), (int(ts[-1]) - self.t0_us) / 1e6))
        if not lasts:
            return 0.0
        top = sorted(t for _, t in sorted(lasts, reverse=True)[:5])
        ref = top[len(top) // 2]  # median: robust to one garbage topic
        return max(0.0, max(t for _, t in lasts if t <= ref + 5.0))

    # --- metadata ---
    @property
    def info(self) -> dict:
        return self.ulog.msg_info_dict

    @property
    def params(self) -> dict:
        return self.ulog.initial_parameters

    def describe(self) -> str:
        i = self.info
        parts = [self.path.name, i.get("sys_name", ""), i.get("ver_hw", ""),
                 f"{self.duration:.0f} s", f"{len(self._ds)} topics"]
        return " · ".join(p for p in parts if p)

    # --- topics / signals ---
    def topics(self) -> list[TopicInfo]:
        out = []
        for (name, inst), d in sorted(self._ds.items()):
            fields = [f for f in d.data if f != "timestamp"]
            derived = []
            if name in DERIVED and all(f in d.data for f in DERIVED[name][1]):
                derived = list(DERIVED[name][0])
            ts = d.data["timestamp"]
            n = len(ts)
            span = (ts[-1] - ts[0]) / 1e6 if n > 1 else 0
            out.append(TopicInfo(name, inst, fields, derived, n, (n - 1) / span if span > 0 else 0))
        return out

    def has(self, topic: str, instance: int = 0) -> bool:
        return (topic, instance) in self._ds

    def instances(self, topic: str) -> list[int]:
        return sorted(i for (n, i) in self._ds if n == topic)

    def dataset(self, topic: str, instance: int = 0):
        return self._ds[(topic, instance)]

    def times(self, topic: str, instance: int = 0) -> np.ndarray:
        ts = self._ds[(topic, instance)].data["timestamp"]
        return (ts.astype(np.int64) - np.int64(self.t0_us)) / 1e6

    def has_signal(self, key: str | SignalKey) -> bool:
        k = SignalKey.parse(key) if isinstance(key, str) else key
        if not self.has(k.topic, k.instance):
            return False
        d = self._ds[(k.topic, k.instance)].data
        if k.field in d:
            return True
        der = DERIVED.get(k.topic)
        return bool(der and k.field in der[0] and all(f in d for f in der[1]))

    def get(self, key: str | SignalKey) -> tuple[np.ndarray, np.ndarray]:
        """Return (t_seconds, values) as float64 arrays. Raises KeyError if missing."""
        k = SignalKey.parse(key) if isinstance(key, str) else key
        sk = str(k)
        if sk in self._cache:
            return self._cache[sk]
        if not self.has(k.topic, k.instance):
            raise KeyError(sk)
        d = self._ds[(k.topic, k.instance)].data
        if k.field in d:
            y = d[k.field].astype(np.float64)
        else:
            der = DERIVED.get(k.topic)
            if not der or k.field not in der[0]:
                raise KeyError(sk)
            y = der[0][k.field](d)
        res = (self.times(k.topic, k.instance), y)
        self._cache[sk] = res
        return res

    # --- flight structure ---
    def mode_segments(self) -> list[ModeSegment]:
        if not self.has("vehicle_status"):
            return []
        t, nav = self.get("vehicle_status:0/nav_state")
        if len(t) == 0:
            return []
        segs = []
        start = 0
        for i in range(1, len(nav) + 1):
            if i == len(nav) or nav[i] != nav[start]:
                t1 = t[i] if i < len(nav) else self.duration
                segs.append(ModeSegment(float(t[start]), float(t1), int(nav[start])))
                start = i
        return segs

    def events(self) -> list[LogEvent]:
        ev: list[LogEvent] = []
        if self.has_signal("vehicle_status:0/arming_state"):
            t, a = self.get("vehicle_status:0/arming_state")
            armed = a == ARMING_STATE_ARMED
            for i in np.flatnonzero(armed[1:] != armed[:-1]) + 1:
                ev.append(LogEvent(float(t[i]), "armed" if armed[i] else "disarmed",
                                   "Armed" if armed[i] else "Disarmed"))
            if len(armed) and armed[0]:
                ev.append(LogEvent(float(t[0]), "armed", "Armed"))
        if self.has_signal("vehicle_land_detected:0/landed"):
            t, l = self.get("vehicle_land_detected:0/landed")
            landed = l > 0.5
            for i in np.flatnonzero(landed[1:] != landed[:-1]) + 1:
                ev.append(LogEvent(float(t[i]), "landed" if landed[i] else "takeoff",
                                   "Landed" if landed[i] else "Takeoff"))
        return sorted(ev, key=lambda e: e.t)

    def messages(self) -> list[LogMessage]:
        # PX4 appends a tab to messages that are duplicated as (undecoded) events
        return sorted((LogMessage((m.timestamp - self.t0_us) / 1e6, m.log_level_str(),
                                  m.message.rstrip('\t'))
                       for m in self.ulog.logged_messages), key=lambda m: m.t)

    def param_changes(self) -> list[tuple[float, str, object]]:
        """Parameters changed while logging: (t_seconds, name, value)."""
        return [((ts - self.t0_us) / 1e6, name, value)
                for ts, name, value in self.ulog.changed_parameters]

    def default_params(self) -> dict | None:
        """Airframe defaults stored in the log (newer PX4), else system defaults."""
        if not self.ulog.has_default_parameters:
            return None
        sys_def = self.ulog.get_default_parameters(0) or {}
        air_def = self.ulog.get_default_parameters(1) or {}
        return {**sys_def, **air_def}
