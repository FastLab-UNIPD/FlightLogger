"""Shared plotting bits: colors, time axis, time formatting, mode colors."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtGui import QColor

from ..log.fr_graphs import flight_modes_table

MIME_SIGNALS = "application/x-flightlogger-signals"
MIME_GRAPHS = "application/x-flightlogger-graphs"
MIME_LINE = "application/x-flightlogger-line"      # JSON line reference (a legend entry being dragged)

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


def note_at(notes, x: float, tol: float):
    """The timed note under time ``x`` (span containing it, or instant within ``tol`` s)."""
    best, best_d = None, None
    for n in notes:
        if n.general:
            continue
        if n.t_end is not None and n.t - tol <= x <= n.t_end + tol:
            d = 0.0 if n.t <= x <= n.t_end else min(abs(x - n.t), abs(x - n.t_end))
        elif n.t_end is None and abs(x - n.t) <= tol:
            d = abs(x - n.t)
        else:
            continue
        if best is None or d < best_d:
            best, best_d = n, d
    return best


def msg_at(messages, x: float, tol: float):
    best, best_d = None, None
    for m in messages:
        d = abs(m.t - x)
        if d <= tol and (best is None or d < best_d):
            best, best_d = m, d
    return best


def show_message_preview(m, global_pos) -> None:
    from html import escape
    from PySide6.QtWidgets import QToolTip
    color = {"error": "#ff7b72", "warning": "#f0c04c"}.get(m.level.lower(), "#999999")
    QToolTip.showText(global_pos, f'<b style="color:{color}">{escape(m.level)}</b> {escape(m.text)}')


def show_preview_at(notes, messages, x: float, tol: float, global_pos) -> bool:
    """One preview only: a note wins over a message. Returns whether something was shown."""
    n = note_at(notes, x, tol)
    if n is not None:
        show_note_preview(n, global_pos)
        return True
    m = msg_at(messages, x, tol)
    if m is not None:
        show_message_preview(m, global_pos)
        return True
    return False


def show_note_preview(n, global_pos) -> None:
    from html import escape
    from PySide6.QtWidgets import QToolTip
    body = f"<br>{escape(n.text.strip())}".replace("\n", "<br>") if n.text.strip() else ""
    QToolTip.showText(global_pos, f'<b style="color:{n.color}">■ {escape(n.title)}</b> '
                                  f'<i>({escape(n.category)})</i>{body}')


def _patch_export_dialog():
    """pyqtgraph's export dialog opens at its tiny designer size with most controls hidden."""
    from pyqtgraph.GraphicsScene import exportDialog
    orig = exportDialog.ExportDialog.show
    if getattr(orig, "_fl_patched", False):
        return

    def white_bg(self):
        exp = self.currentExporter
        params = exp.parameters() if exp is not None else None
        if params is not None and "background" in [c.name() for c in params.children()]:
            from PySide6.QtGui import QColor
            params["background"] = QColor("white")

    def show(self, item=None):
        first = not self.shown
        if not hasattr(self, "_white_btn"):
            from PySide6.QtWidgets import QPushButton
            self._white_btn = QPushButton("White background (image / SVG)")
            self._white_btn.setToolTip("Set the export background to white, e.g. for papers")
            self._white_btn.clicked.connect(self.white_bg)
            self.ui.gridLayout.addWidget(self._white_btn, 7, 0, 1, 3)
        orig(self, item)
        if first:
            self.setMinimumSize(640, 480)
            self.resize(780, 560)
            frame = self.frameGeometry()
            frame.moveCenter(self.screen().availableGeometry().center())
            self.move(frame.topLeft())
    show._fl_patched = True
    exportDialog.ExportDialog.white_bg = white_bg
    exportDialog.ExportDialog.show = show


_patch_export_dialog()
