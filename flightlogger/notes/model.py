"""Timeline notes: comments attached to an instant (or a span) of the log."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field

from PySide6.QtCore import QObject, Signal

# category -> marker color (no pure red: that is the time cursor)
CATEGORIES = {
    "Info": "#009e73",
    "Maneuver": "#0072b2",
    "Anomaly": "#e69f00",
    "Crash": "#7b2d26",
    "Question": "#cc79a7",
}
DEFAULT_CATEGORY = "Info"


def category_color(category: str) -> str:
    return CATEGORIES.get(category, "#555555")


@dataclass
class Note:
    t: float
    title: str = ""
    text: str = ""
    category: str = DEFAULT_CATEGORY
    t_end: float | None = None          # set for a span (e.g. an oscillation)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def color(self) -> str:
        return category_color(self.category)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        t_end = d.get("t_end")
        return cls(t=float(d["t"]), title=str(d.get("title", "")), text=str(d.get("text", "")),
                   category=str(d.get("category", DEFAULT_CATEGORY)),
                   t_end=None if t_end is None else float(t_end),
                   id=str(d.get("id") or uuid.uuid4().hex[:12]))


class NotesModel(QObject):
    """Ordered (by time) list of notes. ``changed`` fires on any modification."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._notes: list[Note] = []

    def __iter__(self):
        return iter(self._notes)

    def __len__(self):
        return len(self._notes)

    @property
    def notes(self) -> list[Note]:
        return list(self._notes)

    def get(self, note_id: str) -> Note | None:
        return next((n for n in self._notes if n.id == note_id), None)

    def _sort(self):
        self._notes.sort(key=lambda n: (n.t, n.id))

    def add(self, note: Note) -> Note:
        self._notes.append(note)
        self._sort()
        self.changed.emit()
        return note

    def update(self, note_id: str, **fields) -> Note | None:
        n = self.get(note_id)
        if n is None:
            return None
        for k, v in fields.items():
            setattr(n, k, v)
        if n.t_end is not None and n.t_end < n.t:
            n.t, n.t_end = n.t_end, n.t
        self._sort()
        self.changed.emit()
        return n

    def remove(self, note_id: str):
        before = len(self._notes)
        self._notes = [n for n in self._notes if n.id != note_id]
        if len(self._notes) != before:
            self.changed.emit()

    def clear(self):
        if self._notes:
            self._notes = []
            self.changed.emit()

    def to_list(self) -> list[dict]:
        return [n.to_dict() for n in self._notes]

    def load_list(self, items: list[dict]):
        self._notes = [Note.from_dict(d) for d in items]
        self._sort()
        self.changed.emit()
