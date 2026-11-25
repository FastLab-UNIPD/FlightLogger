"""Shared time state every panel subscribes to.

* ``timeChanged(t)``  - the current instant (log time, seconds) moved.
* ``seekRequested(t)`` - the user asked to jump somewhere; the playback master
  (video or log clock) follows. ``seek`` also moves the cursor immediately.
* ``xRangeChanged(x0, x1, source)`` - visible time window of linked graph panels.
* ``trimChanged(trim)`` - the hidden-time-range setting changed: ``(t0, t1)`` is the part of the
  flight that is shown, ``None`` the whole flight.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class TimeBus(QObject):
    timeChanged = Signal(float)
    seekRequested = Signal(float)
    xRangeChanged = Signal(float, float, object)
    trimChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._t = 0.0
        self.duration = 0.0
        self.x_range: tuple[float, float] | None = None
        self.trim: tuple[float, float] | None = None

    @property
    def t(self) -> float:
        return self._t

    def clamp(self, t: float) -> float:
        return min(max(t, 0.0), self.duration) if self.duration > 0 else max(t, 0.0)

    def set_time(self, t: float):
        """Called by the playback master; does not trigger a seek."""
        t = self.clamp(t)
        if t != self._t:
            self._t = t
            self.timeChanged.emit(t)

    def seek(self, t: float):
        """User-initiated jump (click on a plot, keyboard step, note...)."""
        t = self.clamp(t)
        if self.trim:                      # a hidden time range: land on the nearest visible edge
            t = min(max(t, self.trim[0]), self.trim[1])
        self.seekRequested.emit(t)
        self.set_time(t)

    def set_x_range(self, x0: float, x1: float, source=None):
        if self.trim:
            x0, x1 = self._clamp_to_trim(x0, x1)
        elif self.duration > 0:
            x0, x1 = max(x0, 0.0), min(x1, self.duration)
            if x1 <= x0:
                x0, x1 = 0.0, self.duration
        if self.x_range and abs(self.x_range[0] - x0) < 1e-9 and abs(self.x_range[1] - x1) < 1e-9:
            return
        self.x_range = (x0, x1)
        self.xRangeChanged.emit(x0, x1, source)

    def _clamp_to_trim(self, x0: float, x1: float) -> tuple[float, float]:
        t0, t1 = self.trim
        x0, x1 = max(x0, t0), min(x1, t1)
        return (x0, x1) if x1 > x0 else (t0, t1)

    # --- hidden time range ---
    def set_trim(self, t0: float | None = None, t1: float | None = None) -> bool:
        """Show only [t0, t1] of the flight (None keeps that edge of the current trim, or the
        flight's start / end). Returns False when the result would be empty."""
        cur0, cur1 = self.trim or (0.0, self.duration if self.duration > 0 else float("inf"))
        t0 = cur0 if t0 is None else float(t0)
        t1 = cur1 if t1 is None else float(t1)
        t0 = max(t0, 0.0)
        if self.duration > 0:
            t1 = min(t1, self.duration)
        if not t1 - t0 > 1e-6:
            return False
        if self.duration > 0 and t0 <= 0.0 and t1 >= self.duration:
            self.clear_trim()      # the whole flight: nothing hidden
            return True
        if self.trim == (t0, t1):
            return True
        self.trim = (t0, t1)
        self.trimChanged.emit(self.trim)
        if self.x_range:
            self.set_x_range(*self.x_range, None)   # clamps to the new trim
        if self._t < t0 or self._t > t1:
            self.seek(min(max(self._t, t0), t1))
        return True

    def clear_trim(self):
        if self.trim is not None:
            self.trim = None
            self.trimChanged.emit(None)

    def reset(self, duration: float):
        self.duration = duration
        self.x_range = None
        if self.trim is not None:
            self.trim = None
            self.trimChanged.emit(None)
        self._t = -1.0
        self.set_time(0.0)
