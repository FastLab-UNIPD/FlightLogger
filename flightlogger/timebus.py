"""Shared time state every panel subscribes to.

* ``timeChanged(t)``  - the current instant (log time, seconds) moved.
* ``seekRequested(t)`` - the user asked to jump somewhere; the playback master
  (video or log clock) follows. ``seek`` also moves the cursor immediately.
* ``xRangeChanged(x0, x1, source)`` - visible time window of linked graph panels.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class TimeBus(QObject):
    timeChanged = Signal(float)
    seekRequested = Signal(float)
    xRangeChanged = Signal(float, float, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._t = 0.0
        self.duration = 0.0
        self.x_range: tuple[float, float] | None = None

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
        self.seekRequested.emit(t)
        self.set_time(t)

    def set_x_range(self, x0: float, x1: float, source=None):
        if self.x_range and abs(self.x_range[0] - x0) < 1e-9 and abs(self.x_range[1] - x1) < 1e-9:
            return
        self.x_range = (x0, x1)
        self.xRangeChanged.emit(x0, x1, source)

    def reset(self, duration: float):
        self.duration = duration
        self.x_range = None
        self._t = -1.0
        self.set_time(0.0)
