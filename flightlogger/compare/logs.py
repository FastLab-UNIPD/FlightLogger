"""The logs of a comparison: path, name, colour, time offset, active flag and the loaded FlightLog."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from ..log.ulog_loader import FlightLog

MAX_ACTIVE = 3
PALETTE = ["#1f77b4", "#d55e00", "#2ca02c", "#9467bd", "#e69f00", "#17becf", "#e377c2", "#8c564b"]


class _LoadThread(QThread):
    """Loads one log off the UI thread (parse only: comparison panels build what they need)."""
    loaded = Signal(object, str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self):
        try:
            log = FlightLog(self.path)
            log.takeoff_time()
            self.loaded.emit(log, "")
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.loaded.emit(None, f"{type(e).__name__}: {e}")


class _GraphsThread(QThread):
    """Computes the Flight Review graphs of one log off the UI thread (seconds per log)."""
    done = Signal(str)

    def __init__(self, log: FlightLog):
        super().__init__()
        self.log = log

    def run(self):
        try:
            self.log.graphs
            self.done.emit("")
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.done.emit(f"{type(e).__name__}: {e}")


@dataclass(eq=False)
class LogEntry:
    path: str
    name: str
    color: str
    offset_s: float = 0.0
    active: bool = False
    log: FlightLog | None = None
    takeoff: float = 0.0
    error: str = ""
    loading: bool = False
    graphs_loading: bool = False
    graph_seconds: float = 0.0      # how long the graphs took (information)
    relinked: bool = False


class LogSet(QObject):
    changed = Signal()              # entries added/removed/edited, active or offset changed
    loaded = Signal(object)         # LogEntry
    failed = Signal(object, str)    # LogEntry, error text
    graphs_ready = Signal(object)   # LogEntry: its Flight Review graphs are computed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[LogEntry] = []
        self._threads: set = set()

    # --- access ---
    def __len__(self):
        return len(self.entries)

    def active_entries(self) -> list[LogEntry]:
        return [e for e in self.entries if e.active]

    def _next_color(self) -> str:
        used = {e.color for e in self.entries}
        for c in PALETTE:
            if c not in used:
                return c
        return PALETTE[len(self.entries) % len(PALETTE)]

    # --- editing ---
    def add(self, path: str | Path, name: str | None = None, color: str | None = None,
            offset_s: float = 0.0, active: bool | None = None) -> LogEntry:
        """Add and start loading a log. Active by default while fewer than MAX_ACTIVE are active."""
        path = str(Path(path))
        if active is None:
            active = len(self.active_entries()) < MAX_ACTIVE
        elif active and len(self.active_entries()) >= MAX_ACTIVE:
            active = False
        e = LogEntry(path, name or Path(path).stem, color or self._next_color(),
                     float(offset_s), bool(active))
        self.entries.append(e)
        self._start_load(e)
        self.changed.emit()
        return e

    def remove(self, entry: LogEntry):
        if entry in self.entries:
            self.entries.remove(entry)
            entry.log = None
            self.changed.emit()

    def clear(self):
        for e in self.entries:
            e.log = None
        self.entries.clear()
        self.changed.emit()

    def set_active(self, entry: LogEntry, on: bool) -> tuple[bool, str]:
        """(ok, reason). Refuses a 4th active log."""
        if on and not entry.active and len(self.active_entries()) >= MAX_ACTIVE:
            return False, f"At most {MAX_ACTIVE} logs can be shown at once: hide another one first."
        if entry.active != bool(on):
            entry.active = bool(on)
            self.changed.emit()
        return True, ""

    def set_offset(self, entry: LogEntry, offset_s: float):
        if entry.offset_s != float(offset_s):
            entry.offset_s = float(offset_s)
            self.changed.emit()

    def set_name(self, entry: LogEntry, name: str):
        name = name.strip() or Path(entry.path).stem
        if entry.name != name:
            entry.name = name
            self.changed.emit()

    def set_color(self, entry: LogEntry, color: str):
        if entry.color != color:
            entry.color = color
            self.changed.emit()

    def aligned_time(self, entry: LogEntry, t):
        """Log time (s) -> comparison time: takeoff = 0, plus the manual offset. Works on arrays."""
        return t - entry.takeoff + entry.offset_s

    # --- Flight Review graphs (lazy, background thread) ---
    @staticmethod
    def has_graphs(entry: LogEntry) -> bool:
        return entry.log is not None and entry.log._graphs is not None

    def ensure_graphs(self, entry: LogEntry):
        """Start computing the entry's graphs unless done / running (graphs_ready follows)."""
        if entry.log is None or entry.graphs_loading or self.has_graphs(entry) or entry.error:
            return
        import time
        entry.graphs_loading = True
        t0 = time.perf_counter()
        th = _GraphsThread(entry.log)
        self._threads.add(th)
        th.finished.connect(lambda t=th: self._threads.discard(t))
        th.done.connect(lambda err, e=entry, t0=t0: self._on_graphs(e, err, time.perf_counter() - t0))
        th.start()

    def _on_graphs(self, e: LogEntry, err: str, secs: float):
        e.graphs_loading = False
        e.graph_seconds = secs
        if e not in self.entries:
            return
        if err:
            e.error = err
            self.failed.emit(e, err)
            self.changed.emit()
        else:
            self.graphs_ready.emit(e)

    # --- loading ---
    def _start_load(self, e: LogEntry):
        e.loading, e.error = True, ""
        th = _LoadThread(e.path)
        self._threads.add(th)
        th.finished.connect(lambda t=th: self._threads.discard(t))
        th.loaded.connect(lambda log, err, e=e: self._on_loaded(e, log, err))
        th.start()

    def _on_loaded(self, e: LogEntry, log, err: str):
        e.loading = False
        if e not in self.entries:
            return
        if log is None:
            e.error = err
            self.failed.emit(e, err)
        else:
            e.log, e.takeoff = log, log.takeoff_time()
            self.loaded.emit(e)
        self.changed.emit()

    def wait_all(self, timeout_ms: int = 120000):
        """Block until every load finished and its result was delivered (tests, scripts)."""
        from PySide6.QtCore import QCoreApplication
        for _ in range(20):          # a delivered log may start its graphs thread
            threads = list(self._threads)
            for th in threads:
                th.wait(timeout_ms)
            QCoreApplication.processEvents()
            if not threads:
                break

    # --- file format ---
    def to_dict(self) -> list[dict]:
        return [{"path": e.path, "name": e.name, "color": e.color, "offset_s": e.offset_s,
                 "active": e.active} for e in self.entries]

    def load_dict(self, logs: list[dict]):
        """Replace all entries with the given ones (missing files are kept, with an error)."""
        self.clear()
        n_active = 0
        failed = []
        for d in logs or []:
            path = d.get("path")
            if not path:
                continue
            active = bool(d.get("active", True)) and n_active < MAX_ACTIVE
            n_active += active
            e = LogEntry(str(path), d.get("name") or Path(path).stem,
                         d.get("color") or self._next_color(), float(d.get("offset_s", 0.0)), active,
                         relinked=bool(d.get("relinked")))
            self.entries.append(e)
            if Path(path).is_file():
                self._start_load(e)
            else:
                e.error = "File not found"
                failed.append(e)
        self.changed.emit()
        for e in failed:
            self.failed.emit(e, e.error)
