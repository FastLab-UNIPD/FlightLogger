"""Write a small synthetic PX4 ULog file for tests and demos.

The ULog format is documented at https://docs.px4.io/main/en/dev_log/ulog_file_format.html.
Only the message types needed by FlightLogger are produced (B, F, I, P, A, D, L).

Usage:  python tests/synth_ulg.py out.ulg
"""
from __future__ import annotations

import math
import struct
import sys

import numpy as np

_FMT = {
    "uint64_t": "Q", "int64_t": "q", "uint32_t": "I", "int32_t": "i",
    "uint16_t": "H", "int16_t": "h", "uint8_t": "B", "int8_t": "b",
    "float": "f", "double": "d", "bool": "?", "char": "c",
}


class ULogWriter:
    def __init__(self, start_us: int):
        self.buf = bytearray(b"ULog\x01\x12\x35" + bytes([1]) + struct.pack("<Q", start_us))
        self._msg("B", bytes(8) + bytes(8) + bytes(24))
        self._formats: dict[str, list[tuple[str, str, int]]] = {}
        self._subs: dict[tuple[str, int], int] = {}
        self._defs_done = False

    def _msg(self, mtype: str, payload: bytes):
        self.buf += struct.pack("<HB", len(payload), ord(mtype)) + payload

    # --- definitions -------------------------------------------------------
    def format(self, name: str, fields: list[tuple[str, str, int]]):
        """fields: (type, name, array_len or 0). timestamp is added first."""
        fields = [("uint64_t", "timestamp", 0)] + fields
        s = name + ":" + "".join(
            f"{t}[{n}] {f};" if n else f"{t} {f};" for t, f, n in fields)
        self._formats[name] = fields
        self._msg("F", s.encode())

    def info(self, ctype: str, key: str, value, mtype: str = "I"):
        k = f"{ctype} {key}".encode()
        if ctype.startswith("char"):
            v = value.encode()
        else:
            v = struct.pack("<" + _FMT[ctype], value)
        self._msg(mtype, bytes([len(k)]) + k + v)

    def param(self, key: str, value):
        self.info("float" if isinstance(value, float) else "int32_t", key, value, mtype="P")

    # --- data --------------------------------------------------------------
    def subscribe(self, name: str, multi_id: int = 0) -> int:
        msg_id = len(self._subs)
        self._subs[(name, multi_id)] = msg_id
        self._msg("A", struct.pack("<BH", multi_id, msg_id) + name.encode())
        return msg_id

    def data(self, name: str, multi_id: int, values: dict):
        msg_id = self._subs[(name, multi_id)]
        payload = struct.pack("<H", msg_id)
        for t, f, n in self._formats[name]:
            v = values.get(f, [0] * n if n else 0)
            if n:
                payload += struct.pack("<" + _FMT[t] * n, *v)
            else:
                payload += struct.pack("<" + _FMT[t], v)
        self._msg("D", payload)

    def log(self, level: int, t_us: int, text: str):
        self._msg("L", struct.pack("<BQ", ord(str(level)), t_us) + text.encode())


def _quat(roll, pitch, yaw):
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return [cr * cp * cy + sr * sp * sy, sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy]


# Flight script (seconds from log start)
ARM_T, TAKEOFF_T, LAND_T, DISARM_T, END_T = 10.0, 15.0, 100.0, 105.0, 120.0


def roll_of(t):  # degrees -> radians, a 0.5 Hz oscillation during 40-60 s
    return math.radians(15 * math.sin(2 * math.pi * 0.5 * t)) if 40 <= t < 60 else 0.0


def altitude_of(t):
    if t < TAKEOFF_T or t > LAND_T:
        return 0.0
    return min(10.0, (t - TAKEOFF_T) * 2, (LAND_T - t) * 2)


def nav_state_of(t):
    if t < 30:
        return 2   # POSCTL
    if t < 70:
        return 1   # ALTCTL
    if t < 90:
        return 3   # AUTO_MISSION
    return 18      # AUTO_LAND


def write(path: str):
    start = 1_000_000
    w = ULogWriter(start)
    w.format("vehicle_attitude", [("float", "q", 4)])
    w.format("vehicle_attitude_setpoint", [("float", "q_d", 4), ("float", "thrust_body", 3)])
    w.format("vehicle_angular_velocity", [("float", "xyz", 3)])
    w.format("vehicle_local_position", [("float", "x", 0), ("float", "y", 0), ("float", "z", 0),
                                        ("float", "vx", 0), ("float", "vy", 0), ("float", "vz", 0)])
    w.format("vehicle_status", [("uint8_t", "arming_state", 0), ("uint8_t", "nav_state", 0),
                                ("uint8_t", "vehicle_type", 0)])
    w.format("vehicle_land_detected", [("bool", "landed", 0)])
    w.format("battery_status", [("float", "voltage_v", 0), ("float", "current_a", 0),
                                ("float", "remaining", 0)])
    w.format("sensor_accel", [("float", "x", 0), ("float", "y", 0), ("float", "z", 0)])
    w.info("char[3]", "sys_name", "PX4")
    w.info("char[10]", "ver_hw", "PX4_FMU_V6")
    w.param("MC_ROLL_P", 6.5)
    w.param("MAV_TYPE", 2)

    for name, mid in [("vehicle_attitude", 0), ("vehicle_attitude_setpoint", 0),
                      ("vehicle_angular_velocity", 0), ("vehicle_local_position", 0),
                      ("vehicle_status", 0), ("vehicle_land_detected", 0),
                      ("battery_status", 0), ("sensor_accel", 0), ("sensor_accel", 1)]:
        w.subscribe(name, mid)

    rng = np.random.default_rng(0)
    events = [(ARM_T, 6, "Armed by RC"), (TAKEOFF_T, 6, "Takeoff detected"),
              (45.0, 4, "High vibration level"), (LAND_T, 6, "Landing detected"),
              (DISARM_T, 6, "Disarmed by auto preflight disarming")]
    n = int(END_T * 100)
    for i in range(n):  # 100 Hz
        t = i / 100.0
        tu = start + int(t * 1e6)
        r = roll_of(t)
        w.data("vehicle_attitude", 0, {"timestamp": tu, "q": _quat(r, 0.0, math.radians(30))})
        w.data("vehicle_attitude_setpoint", 0, {"timestamp": tu,
               "q_d": _quat(r * 0.8, 0.0, math.radians(30)), "thrust_body": [0, 0, -0.5]})
        dr = (roll_of(t + 0.01) - r) / 0.01
        w.data("vehicle_angular_velocity", 0, {"timestamp": tu, "xyz": [dr, 0, 0]})
        if i % 2 == 0:  # 50 Hz
            alt = altitude_of(t)
            w.data("vehicle_local_position", 0, {"timestamp": tu, "z": -alt,
                   "vz": -(altitude_of(t + 0.02) - alt) / 0.02})
            for mid in (0, 1):
                w.data("sensor_accel", mid, {"timestamp": tu, "x": rng.normal(0, 0.3),
                       "y": rng.normal(0, 0.3), "z": -9.81 + rng.normal(0, 0.3)})
        if i % 50 == 0:  # 2 Hz
            armed = ARM_T <= t < DISARM_T
            w.data("vehicle_status", 0, {"timestamp": tu, "arming_state": 2 if armed else 1,
                   "nav_state": nav_state_of(t), "vehicle_type": 1})
            w.data("vehicle_land_detected", 0, {"timestamp": tu,
                   "landed": not (TAKEOFF_T <= t < LAND_T)})
            w.data("battery_status", 0, {"timestamp": tu, "voltage_v": 16.8 - t * 0.01,
                   "current_a": 15.0 if armed else 0.5, "remaining": 1 - t / 300})
        while events and events[0][0] <= t:
            et, lvl, txt = events.pop(0)
            w.log(lvl, start + int(et * 1e6), txt)

    with open(path, "wb") as f:
        f.write(w.buf)


if __name__ == "__main__":
    write(sys.argv[1] if len(sys.argv) > 1 else "synthetic.ulg")
