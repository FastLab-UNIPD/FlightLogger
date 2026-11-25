"""Timeline notes: comments attached to an instant (or a span) of the log."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field

from PySide6.QtCore import QObject, Signal

# category -> marker color (no pure red: that is the time cursor)
CATEGORIES = {
    "Info": "#009e73",
    "Maneuver": "#0072b2",
    "Anomaly": "#e69f00",
    "Crash": "#c0504d",
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
    general: bool = False               # untimed note about the whole flight
    custom_color: str | None = None     # overrides the category color when set
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    set: str = ""                       # note set (group) name; "" = no set

    @property
    def color(self) -> str:
        return self.custom_color or category_color(self.category)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        t_end = d.get("t_end")
        return cls(t=float(d["t"]), title=str(d.get("title", "")), text=str(d.get("text", "")),
                   category=str(d.get("category", DEFAULT_CATEGORY)),
                   t_end=None if t_end is None else float(t_end),
                   general=bool(d.get("general", False)),
                   custom_color=d.get("custom_color") or None,
                   id=str(d.get("id") or uuid.uuid4().hex[:12]),
                   set=str(d.get("set") or ""))


class NotesModel(QObject):
    """Ordered (by time) list of notes. ``changed`` fires on any modification."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._notes: list[Note] = []
        self._undo: list[list[dict]] = []
        self._redo: list[list[dict]] = []
        self._last_edit: tuple[str, float] | None = None   # (note id, time) to merge typing

    def _push(self, merge_key: str | None = None):
        """Remember the current state before a change (typing in one note merges)."""
        now = time.monotonic()
        if merge_key and self._last_edit and self._last_edit[0] == merge_key \
                and now - self._last_edit[1] < 1.5 and self._undo:
            self._last_edit = (merge_key, now)
            return
        self._last_edit = (merge_key, now) if merge_key else None
        self._undo.append(self.to_list())
        del self._undo[:-100]
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        return self._restore(self._undo, self._redo)

    def redo(self) -> bool:
        return self._restore(self._redo, self._undo)

    def _restore(self, src: list, dst: list) -> bool:
        if not src:
            return False
        dst.append(self.to_list())
        self._notes = [Note.from_dict(d) for d in src.pop()]
        self._last_edit = None
        self._sort()
        self.changed.emit()
        return True

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
        self._notes.sort(key=lambda n: (not n.general, n.t, n.id))

    def add(self, note: Note) -> Note:
        self._push()
        self._notes.append(note)
        self._sort()
        self.changed.emit()
        return note

    def update(self, note_id: str, **fields) -> Note | None:
        n = self.get(note_id)
        if n is None:
            return None
        self._push(merge_key=note_id)
        for k, v in fields.items():
            setattr(n, k, v)
        if n.t_end is not None and n.t_end < n.t:
            n.t, n.t_end = n.t_end, n.t
        self._sort()
        self.changed.emit()
        return n

    def remove(self, note_id: str):
        before = len(self._notes)
        if self.get(note_id) is not None:
            self._push()
        self._notes = [n for n in self._notes if n.id != note_id]
        if len(self._notes) != before:
            self.changed.emit()

    def clear(self):
        if self._notes:
            self._push()
            self._notes = []
            self.changed.emit()

    def to_list(self) -> list[dict]:
        return [n.to_dict() for n in self._notes]

    def load_list(self, items: list[dict]):
        self._notes = [Note.from_dict(d) for d in items]
        self._undo.clear()
        self._redo.clear()
        self._sort()
        self.changed.emit()
