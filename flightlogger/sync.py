"""Mapping between log time and video time.

    video_t = offset + scale * log_t        log_t = (video_t - offset) / scale

``offset`` is set by hand ("Sync here", typed in, or nudged frame by frame).
A second sync point late in the flight sets ``scale`` too, which corrects the
drift between the camera clock and the flight controller clock.
``points`` keeps the (log_t, video_t) pairs the user matched.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MIN_POINT_SPACING = 5.0      # s of log time between the two sync points
MAX_DRIFT = 0.05             # |scale - 1|; more than 5 % is not clock drift


@dataclass
class Sync:
    offset: float = 0.0
    scale: float = 1.0
    points: list = field(default_factory=list)   # [(log_t, video_t), ...]

    def to_video(self, log_t: float) -> float:
        return self.offset + self.scale * log_t

    def to_log(self, video_t: float) -> float:
        return (video_t - self.offset) / self.scale

    def offset_for(self, video_t: float, log_t: float) -> float:
        """Offset that makes video_t and log_t the same instant."""
        return video_t - self.scale * log_t

    @property
    def drift_percent(self) -> float:
        return (self.scale - 1.0) * 100.0

    def set_single_point(self, log_t: float, video_t: float):
        """One matched instant: pure offset, no drift correction."""
        self.scale = 1.0
        self.offset = video_t - log_t
        self.points = [(log_t, video_t)]

    def set_second_point(self, log_t: float, video_t: float):
        """Two matched instants: offset and scale. Raises ValueError if unusable."""
        if not self.points:
            raise ValueError("Set the first sync point first")
        l1, v1 = self.points[0]
        if abs(log_t - l1) < MIN_POINT_SPACING:
            raise ValueError(f"The two sync points must be at least {MIN_POINT_SPACING:.0f} s apart")
        scale = (video_t - v1) / (log_t - l1)
        if not (1 - MAX_DRIFT <= scale <= 1 + MAX_DRIFT):
            raise ValueError(f"The points imply {100 * (scale - 1):+.1f} % drift; "
                             "check that both pairs show the same moments")
        self.scale = scale
        self.offset = v1 - scale * l1
        self.points = [(l1, v1), (log_t, video_t)]

    def shift(self, delta: float):
        """Move the video side by delta seconds (frame nudges, typed offset)."""
        self.offset += delta
        self.points = [(lt, vt + delta) for lt, vt in self.points]

    def reset_drift(self):
        if self.points:
            self.set_single_point(*self.points[0])
        else:
            self.scale = 1.0

    def to_dict(self) -> dict:
        return {"offset_s": self.offset, "scale": self.scale,
                "points": [list(p) for p in self.points]}

    @classmethod
    def from_dict(cls, d: dict) -> "Sync":
        return cls(float(d.get("offset_s", 0.0)), float(d.get("scale", 1.0)),
                   [(float(a), float(b)) for a, b in d.get("points", [])])
