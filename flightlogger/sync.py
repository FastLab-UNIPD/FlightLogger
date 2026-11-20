"""Mapping between log time and video time.

    video_t = offset + scale * log_t        log_t = (video_t - offset) / scale

``offset`` is set by hand ("Sync here", typed in, or nudged frame by frame).
``scale`` stays 1.0 until two-point sync is added (clock drift on long flights).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Sync:
    offset: float = 0.0
    scale: float = 1.0

    def to_video(self, log_t: float) -> float:
        return self.offset + self.scale * log_t

    def to_log(self, video_t: float) -> float:
        return (video_t - self.offset) / self.scale

    def offset_for(self, video_t: float, log_t: float) -> float:
        """Offset that makes video_t and log_t the same instant."""
        return video_t - self.scale * log_t

    def to_dict(self) -> dict:
        return {"offset_s": self.offset, "scale": self.scale}

    @classmethod
    def from_dict(cls, d: dict) -> "Sync":
        return cls(float(d.get("offset_s", 0.0)), float(d.get("scale", 1.0)))
