"""Line references, the app-wide line clipboard and the right-axis scale math (no Qt here).

A *line reference* names a line so it can be rebuilt from any log (also a different one, which the
comparison mode needs):

    {"graph": "velocity", "series": "Z Setpoint"}   a series of a Flight Review graph
    {"signal": "vehicle_local_position:0/vz"}       a raw signal

A pasted line (an *overlay* of a plot) is a reference plus ``"axis": "left" | "right"`` and an
optional ``"style"``. The right-axis settings of a plot are a dict
``{"mode": "auto"|"zero"|"manual"|"gain"|"match", "min", "max", "gain", "offset", "match"}``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

MODES = ("auto", "zero", "manual", "gain", "match")
MODE_NAMES = {"auto": "Auto", "zero": "Zero aligned", "manual": "Manual range", "gain": "Gain / offset",
              "match": "Match to a left line"}
PAD = 0.05


@dataclass
class Line:
    """A resolved line: data plus how Flight Review draws it."""
    label: str
    t: np.ndarray
    y: np.ndarray
    color: str | None = None      # Flight Review color letter / hex; None for raw signals
    step: bool = False
    scatter: bool = False
    units: str = ""


# --- references ---------------------------------------------------------------------------
def make_ref(*, graph: str | None = None, series: str | None = None, signal: str | None = None) -> dict:
    return {"signal": signal} if signal is not None else {"graph": graph, "series": series or ""}


def norm_ref(d: dict) -> dict | None:
    """The reference part of a dict (drops axis / style), or None if it is not one."""
    if not isinstance(d, dict):
        return None
    if d.get("signal"):
        return {"signal": str(d["signal"])}
    if d.get("graph") is not None and "series" in d:
        return {"graph": str(d["graph"]), "series": str(d["series"])}
    return None


def ref_id(ref: dict) -> tuple:
    r = norm_ref(ref)
    return ("s", r["signal"]) if "signal" in r else ("g", r["graph"], r["series"])


def ref_label(ref: dict) -> str:
    r = norm_ref(ref)
    if "signal" in r:
        from ..log.ulog_loader import SignalKey
        try:
            return SignalKey.parse(r["signal"]).label
        except ValueError:
            return r["signal"]
    return r["series"]


def graph_units(y_label: str) -> str:
    """'[m/s]' -> 'm/s' (Flight Review's y labels); anything else: unknown."""
    m = re.fullmatch(r"\s*\[([^\]]+)\]\s*", y_label or "")
    return m.group(1) if m and not re.fullmatch(r"[-+\d.,\s]+", m.group(1)) else ""


def resolve(ref: dict, log) -> Line | None:
    """The data of a reference in this log, or None when the log has no such line."""
    r = norm_ref(ref)
    if r is None or log is None:
        return None
    if "signal" in r:
        try:
            if not log.has_signal(r["signal"]):
                return None
            t, y = log.get(r["signal"])
        except (KeyError, ValueError):
            return None
        return Line(ref_label(r), t, y)
    g = log.graphs.get(r["graph"])
    if g is None or not g.time_based:
        return None
    for i, s in enumerate(g.series, 1):
        if (s.label or f"Series {i}") == r["series"]:
            return Line(r["series"], s.t, s.y, s.color, s.step, s.scatter, graph_units(g.y_label))
    return None


# --- the line clipboard (one for the whole app, shared by every panel and floating window) ---
class _Clipboard:
    def __init__(self):
        self.ref: dict | None = None

    def set(self, ref: dict) -> None:
        self.ref = norm_ref(ref)

    def get(self) -> dict | None:
        return dict(self.ref) if self.ref else None

    def clear(self) -> None:
        self.ref = None


clipboard = _Clipboard()


# --- scale math for the right axis ---------------------------------------------------------
def visible_span(t, y, x0: float, x1: float) -> tuple[float, float] | None:
    """(min, max) of y for t in [x0, x1] (NaN / inf ignored); None when nothing is visible."""
    if t is None or len(t) == 0:
        return None
    i0 = int(np.searchsorted(t, x0, "left"))
    i1 = int(np.searchsorted(t, x1, "right"))
    seg = np.asarray(y[i0:i1], dtype=float)
    seg = seg[np.isfinite(seg)]
    if seg.size == 0:
        return None
    return float(seg.min()), float(seg.max())


def union_span(spans) -> tuple[float, float] | None:
    spans = [s for s in spans if s is not None]
    if not spans:
        return None
    return min(s[0] for s in spans), max(s[1] for s in spans)


def pad_span(lo: float, hi: float, frac: float = PAD) -> tuple[float, float]:
    if hi - lo < 1e-12 * max(1.0, abs(lo), abs(hi)):
        d = max(abs(lo) * 0.1, 1.0)
        return lo - d, hi + d
    d = (hi - lo) * frac
    return lo - d, hi + d


def affine_range(view: tuple[float, float], gain: float, offset: float) -> tuple[float, float]:
    """Right-axis range so that a value p is drawn at the left height gain*p + offset."""
    g = gain if abs(gain) > 1e-12 else 1.0
    a, b = (view[0] - offset) / g, (view[1] - offset) / g
    return (a, b) if a <= b else (b, a)


def match_affine(data: tuple[float, float] | None, target: tuple[float, float] | None):
    """(gain, offset) that maps the pasted line's visible min/max onto the target's, or None."""
    if data is None or target is None:
        return None
    d0, d1 = data
    t0, t1 = target
    if t1 - t0 < 1e-12:
        return None
    if abs(d1 - d0) < 1e-12:                      # flat pasted line: sit on the target's middle
        return 1.0, (t0 + t1) / 2 - (d0 + d1) / 2
    g = (t1 - t0) / (d1 - d0)
    return g, t0 - g * d0


def zero_range(view: tuple[float, float], data: tuple[float, float] | None):
    """Right range with 0 at the same height as the left axis's 0 and all of `data` visible.
    None when that is impossible (0 is off the left axis, or the left range is empty)."""
    if data is None:
        return None
    L0, L1 = view
    if L1 - L0 <= 0:
        return None
    f = (0.0 - L0) / (L1 - L0)               # height of 0, 0 = bottom, 1 = top
    if not 0.0 < f < 1.0:
        return None
    lo, hi = min(data[0], 0.0), max(data[1], 0.0)
    need = [1e-12]
    if hi > 0:
        need.append(hi / (1.0 - f))
    if lo < 0:
        need.append(-lo / f)
    s = max(need) * (1 + PAD)
    return -f * s, (1.0 - f) * s


def right_range(settings: dict, view: tuple[float, float], data, target):
    """The right axis's Y range for its mode.

    settings: the plot's right-axis dict; view: the left axis's current (min, max); data: visible
    (min, max) of the right-axis lines; target: visible (min, max) of the match line.
    Returns (lo, hi), or None when there is nothing to show."""
    mode = settings.get("mode", "auto")
    auto = pad_span(*data) if data else None
    if mode == "manual":
        lo, hi = settings.get("min"), settings.get("max")
        if lo is not None and hi is not None and hi > lo:
            return float(lo), float(hi)
        return auto
    if mode == "gain":
        return affine_range(view, float(settings.get("gain", 1.0)), float(settings.get("offset", 0.0)))
    if mode == "match":
        aff = match_affine(data, target)
        return affine_range(view, *aff) if aff else auto
    if mode == "zero":
        return zero_range(view, data) or auto
    return auto


def clean_settings(s: dict | None) -> dict:
    """A right-axis dict as saved: unknown modes become auto, only the keys its mode uses."""
    s = s or {}
    mode = s.get("mode") if s.get("mode") in MODES else "auto"
    out = {"mode": mode}
    keys = {"manual": ("min", "max"), "gain": ("gain", "offset"), "match": ("match",)}.get(mode, ())
    for k in keys:
        if s.get(k) is not None:
            out[k] = s[k]
    return out
