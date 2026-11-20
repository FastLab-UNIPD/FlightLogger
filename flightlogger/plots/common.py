"""Shared plotting bits: colors, time axis, time formatting, mode colors."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtGui import QColor

from ..log.fr_graphs import flight_modes_table

MIME_SIGNALS = "application/x-flightlogger-signals"
MIME_GRAPHS = "application/x-flightlogger-graphs"

CURSOR_COLOR = "#ff0000"
# Custom-signal palette: Flight Review's colors8 (black moved to the end)
PALETTE = ['#0072b2', '#d55e00', '#009e73', '#e69f00', '#cc79a7', '#55b4e9', '#f0e442', '#000000']

MODE_ALPHA_PLOT = 23     # Flight Review: fill_alpha 0.09
MODE_ALPHA_TIMELINE = 60


def mode_color(nav_state: int, alpha: int = MODE_ALPHA_PLOT):
    """Flight Review's flight-mode background color, or None if FR draws none."""
    entry = flight_modes_table.get(nav_state)
    if entry is None:
        return None
    c = QColor(entry[1])
    return (c.red(), c.green(), c.blue(), alpha)


def mode_name(nav_state: int) -> str:
    entry = flight_modes_table.get(nav_state)
    return entry[0] if entry else f"Mode {nav_state}"


def fmt_time(t: float, decimals: int = 3) -> str:
    """Seconds -> [H:]MM:SS.sss"""
    sign = "-" if t < 0 else ""
    t = abs(t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    width = 3 + decimals if decimals else 2
    sec = f"{s:0{width}.{decimals}f}"
    if sec.startswith("60"):  # rounding overflow
        s, m = 0.0, m + 1
        sec = f"{s:0{width}.{decimals}f}"
    if h:
        return f"{sign}{int(h)}:{int(m):02d}:{sec}"
    return f"{sign}{int(m)}:{sec}"


class TimeAxis(pg.AxisItem):
    """Bottom axis that shows log seconds as M:SS(.sss)."""

    def tickStrings(self, values, scale, spacing):
        dec = 0 if spacing >= 1 else 1 if spacing >= 0.1 else 2 if spacing >= 0.01 else 3
        return [fmt_time(v * scale, dec) for v in values]


# PX4 logged-message markers (dotted lines); never pure red (that is the cursor)
MESSAGE_LEVELS = ["EMERGENCY", "ALERT", "CRITICAL", "ERROR", "WARNING", "NOTICE", "INFO", "DEBUG"]
MESSAGE_FILTERS = {"off": -1, "errors": MESSAGE_LEVELS.index("ERROR"),
                   "warnings": MESSAGE_LEVELS.index("WARNING"), "all": MESSAGE_LEVELS.index("INFO")}


def message_color(level: str) -> str:
    i = MESSAGE_LEVELS.index(level) if level in MESSAGE_LEVELS else len(MESSAGE_LEVELS)
    if i <= MESSAGE_LEVELS.index("ERROR"):
        return "#8e24aa"   # purple: errors and worse
    if i == MESSAGE_LEVELS.index("WARNING"):
        return "#b8860b"   # dark gold: warnings
    return "#7f7f7f"       # gray: notice / info


def filter_messages(messages, mode: str):
    limit = MESSAGE_FILTERS.get(mode, MESSAGE_FILTERS["warnings"])
    return [m for m in messages
            if m.level in MESSAGE_LEVELS and MESSAGE_LEVELS.index(m.level) <= limit]
