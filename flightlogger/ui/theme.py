"""Look and feel: a dark theme modelled on OmniQuadGUI, with a lavender accent.

OmniQuadGUI (GTK) uses warm dark greys, rounded "cards", small upper-case
card titles in the accent colour, gradient buttons and line icons. The same
ideas are expressed here as a Qt stylesheet, with Ubuntu orange replaced by
lavender. Everything that only changes how the app looks lives here.

apply_theme(app) switches the whole application (widgets, docks, graphs) to
this theme; without it FlightLogger keeps the plain system/Flight Review look
(the tests run that way).
"""
from __future__ import annotations

from pathlib import Path

import pyqtgraph as pg
from PySide6.QtGui import QColor, QIcon, QPalette

RESOURCES = Path(__file__).resolve().parents[1] / "resources"
LOGO_SVG = RESOURCES / "flightlogger.svg"

# --- palette -------------------------------------------------------------------------
BG = "#1e1e1e"            # window
SURFACE = "#272727"       # cards (dock areas)
BORDER = "#383838"
WELL = "#171717"          # sunken: inputs, lists, tables
TEXT = "#f2f2f2"
DIM = "#a3a3a3"
DISABLED = "#5f5f5f"

ACCENT = "#9b87e0"        # lavender: selection, focus, primary actions
ACCENT_LIGHT = "#c8bcf2"  # card titles, icons on normal buttons
ACCENT_TOP = "#ad9cec"    # primary button gradient
ACCENT_BOTTOM = "#8670d4"
ACCENT_EDGE = "#6f5bbf"
ACCENT_HOVER = "#8d7fc4"  # hover border on normal buttons

GREEN = "#3ddc84"
AMBER = "#f6b73c"
RED = "#ff5d5d"

PLOT_BG = "#1a1a1a"       # graph background
PLOT_FG = DIM             # axes, ticks
PLOT_TEXT = TEXT          # graph titles

ICON_NEUTRAL = ACCENT_LIGHT
ICON_PRIMARY = "#ffffff"
ICON_DISABLED = "#5a5a5a"

_active = False


def is_dark() -> bool:
    """True once apply_theme() ran (graph code adapts its colours)."""
    return _active


def logo_icon() -> QIcon:
    return QIcon(str(LOGO_SVG))


# --- stylesheet --------------------------------------------------------------------------
QSS = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QToolTip {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 6px;
}}

/* ---- menu bar, menus --------------------------------------------------- */
QMenuBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2e2e2e, stop:1 #262626);
    border-bottom: 1px solid #151515;
}}
QMenuBar::item {{ background: transparent; padding: 4px 10px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: rgba(155, 135, 224, 0.22); }}
QMenu {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{ padding: 5px 22px 5px 22px; border-radius: 6px; }}
QMenu::item:selected {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT_BOTTOM}, stop:1 {ACCENT_TOP});
    color: #ffffff;
}}
QMenu::item:disabled {{ color: {DISABLED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

/* ---- transport toolbar (the header bar) ------------------------------- */
QToolBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2e2e2e, stop:1 #262626);
    border: none;
    border-bottom: 1px solid rgba(155, 135, 224, 0.35);
    padding: 4px 6px;
    spacing: 4px;
}}
QToolBar::separator {{ background: {BORDER}; width: 1px; margin: 4px 6px; }}
QLabel#timeLabel {{
    background-color: rgba(255, 255, 255, 0.04);
    border: 1px solid #3c3c3c;
    border-radius: 10px;
    padding: 3px 12px;
    font-family: monospace;
    font-size: 13px;
}}

/* ---- buttons ----------------------------------------------------------- */
QPushButton, QToolButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3a3a3a, stop:1 #313131);
    border: 1px solid #474747;
    border-radius: 8px;
    color: {TEXT};
    padding: 4px 10px;
    min-height: 20px;
}}
QToolButton {{ padding: 3px 7px; }}
QPushButton:hover, QToolButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #444444, stop:1 #393939);
    border-color: {ACCENT_HOVER};
}}
QPushButton:pressed, QToolButton:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2a2a2a, stop:1 #333333);
}}
QPushButton:disabled, QToolButton:disabled {{
    background: #252525;
    border-color: #303030;
    color: {DISABLED};
}}
/* toggles (Link X, Follow, Linked, ...) look "primary" when on */
QToolButton:checked, QPushButton:checked,
QPushButton[variant="primary"], QToolButton[variant="primary"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {ACCENT_TOP}, stop:1 {ACCENT_BOTTOM});
    border-color: {ACCENT_EDGE};
    color: #ffffff;
}}
QToolButton:checked:hover, QPushButton:checked:hover,
QPushButton[variant="primary"]:hover, QToolButton[variant="primary"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #bbadf2, stop:1 #927ddc);
    border-color: {ACCENT_EDGE};
}}
QPushButton[variant="primary"]:disabled, QToolButton[variant="primary"]:disabled {{
    background: #252525; border-color: #303030; color: {DISABLED};
}}
QPushButton[variant="danger"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #e8434e, stop:1 #b8202c);
    border-color: #9c1a25;
    color: #ffffff;
}}
QPushButton[variant="danger"]:disabled {{ background: #252525; border-color: #303030; color: {DISABLED}; }}
QToolBar QToolButton {{ min-width: 26px; }}
QToolButton#playButton {{ min-width: 40px; }}
QToolButton::menu-indicator {{ image: none; }}

/* ---- inputs ----------------------------------------------------------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox {{
    background-color: {WELL};
    border: 1px solid #3a3a3a;
    border-radius: 8px;
    padding: 3px 8px;
    color: {TEXT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QAbstractSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {{ color: {DISABLED}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    selection-background-color: {ACCENT};
}}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ border: none; width: 14px; }}

/* ---- lists, trees, tables -------------------------------------------------------------- */
QAbstractItemView {{
    background-color: {WELL};
    alternate-background-color: #1c1c1c;
    border: 1px solid #323232;
    border-radius: 8px;
    outline: 0;
}}
QAbstractItemView::item {{ padding: 2px 4px; border-radius: 4px; }}
QAbstractItemView::item:hover {{ background-color: #2a2a2a; }}
QAbstractItemView::item:selected {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT_BOTTOM}, stop:1 {ACCENT_TOP});
    color: #ffffff;
}}
QHeaderView::section {{
    background-color: {SURFACE};
    color: {ACCENT_LIGHT};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 4px 6px;
    font-size: 11px;
    font-weight: bold;
}}
QTableCornerButton::section {{ background-color: {SURFACE}; border: none; }}
QCheckBox::indicator, QAbstractItemView::indicator {{
    width: 14px; height: 14px;
    border: 1px solid #555555;
    border-radius: 4px;
    background-color: {WELL};
}}
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT_EDGE};
    image: url({RESOURCES.as_posix()}/check.svg);
}}

/* ---- sliders, scroll bars, splitters ---------------------------------------- */
QSlider::groove:horizontal {{ height: 6px; background: #333333; border-radius: 3px; }}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT_BOTTOM}, stop:1 {ACCENT_TOP});
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: #f2f2f2; border: 2px solid {ACCENT};
    width: 12px; height: 12px; margin: -5px 0; border-radius: 7px;
}}
QSlider::handle:horizontal:disabled {{ background: #444444; border-color: #3a3a3a; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: #3d3d3d; border-radius: 5px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {ACCENT_HOVER}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSplitter::handle {{ background: {BG}; }}

/* ---- status bar ------------------------------------------------------------- */
QStatusBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #262626, stop:1 #222222);
    border-top: 1px solid {BORDER};
    color: {DIM};
}}
QStatusBar QLabel {{ background: transparent; color: {DIM}; }}

/* ---- small text roles --------------------------------------------------------- */
QLabel[role="dim"] {{ color: {DIM}; }}
QLabel[role="info"] {{ color: #8f8f8f; font-size: 11px; }}
QLabel[role="banner"] {{
    background-color: rgba(246, 183, 60, 0.12);
    border: 1px solid #c98f22;
    border-radius: 8px;
    color: #f8c766;
    padding: 4px 8px;
}}
QProgressBar {{
    background-color: {WELL}; border: 1px solid #3a3a3a; border-radius: 6px; text-align: center;
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 5px; }}
"""

# Qt Advanced Docking System: dock areas become rounded "cards" with
# OmniQuad-style upper-case titles; the active tab gets a lavender underline.
R = RESOURCES.as_posix()
ADS_QSS = f"""
ads--CDockContainerWidget {{ background: {BG}; }}
ads--CDockContainerWidget > QSplitter {{ padding: 2px; }}
ads--CDockContainerWidget ads--CDockSplitter::handle {{ background: {BG}; }}
ads--CDockAreaWidget {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
ads--CDockAreaTitleBar {{
    background: transparent;
    border-bottom: 1px solid {BORDER};
    padding: 2px 4px 0 4px;
}}
ads--CDockAreaWidget[focused="true"] ads--CDockAreaTitleBar {{
    border-bottom: 1px solid rgba(155, 135, 224, 0.55);
}}
ads--CDockWidgetTab {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 2px 6px;
}}
ads--CDockWidgetTab[activeTab="true"] {{ border-bottom: 2px solid {ACCENT}; }}
ads--CDockWidgetTab QLabel {{
    background: transparent;
    color: {DIM};
    font-size: 11px;
    font-weight: bold;
}}
ads--CDockWidgetTab[activeTab="true"] QLabel {{ color: {ACCENT_LIGHT}; }}
ads--CDockWidgetTab:hover QLabel {{ color: {TEXT}; }}
ads--CDockWidget {{ background: {SURFACE}; border: none; }}
ads--CDockWidget > QScrollArea {{ background: {SURFACE}; border: none; }}
ads--CTitleBarButton {{ background: transparent; border: none; padding: 0 2px; }}
ads--CTitleBarButton:hover {{ background: rgba(255, 255, 255, 0.10); border-radius: 6px; }}
#tabCloseButton {{
    background: none; border: none; margin-top: 2px; padding: 0 -2px;
    qproperty-icon: url({R}/ads-close.svg), url({R}/ads-close-disabled.svg) disabled;
    qproperty-iconSize: 12px;
}}
#dockAreaCloseButton {{
    qproperty-icon: url({R}/ads-close.svg), url({R}/ads-close-disabled.svg) disabled;
    qproperty-iconSize: 14px;
}}
#detachGroupButton {{
    qproperty-icon: url({R}/ads-detach.svg), url({R}/ads-detach-disabled.svg) disabled;
    qproperty-iconSize: 14px;
}}
#tabsMenuButton {{ qproperty-icon: url({R}/ads-menu.svg); qproperty-iconSize: 14px; }}
#tabsMenuButton::menu-indicator {{ image: none; }}
#tabCloseButton:hover {{ background: rgba(255, 255, 255, 0.10); border-radius: 4px; }}
ads--CFloatingDockContainer {{ background: {BG}; }}
ads--CFloatingWidgetTitleBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2e2e2e, stop:1 #262626);
    border-bottom: 1px solid rgba(155, 135, 224, 0.35);
}}
"""


def _palette() -> QPalette:
    p = QPalette()
    roles = {
        QPalette.Window: BG, QPalette.WindowText: TEXT, QPalette.Base: WELL,
        QPalette.AlternateBase: "#1c1c1c", QPalette.Text: TEXT, QPalette.Button: SURFACE,
        QPalette.ButtonText: TEXT, QPalette.Highlight: ACCENT, QPalette.HighlightedText: "#ffffff",
        QPalette.ToolTipBase: SURFACE, QPalette.ToolTipText: TEXT, QPalette.Link: ACCENT_LIGHT,
        QPalette.PlaceholderText: "#6f6f6f", QPalette.Light: "#3a3a3a", QPalette.Midlight: "#333333",
        QPalette.Mid: "#2e2e2e", QPalette.Dark: "#141414", QPalette.Shadow: "#000000",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(DISABLED))
    return p


def apply_theme(app):
    """Switch the application to the lavender dark theme (call before windows exist)."""
    global _active
    _active = True
    app.setStyle("Fusion")
    app.setPalette(_palette())
    app.setStyleSheet(QSS)
    app.setWindowIcon(logo_icon())
    pg.setConfigOptions(background=PLOT_BG, foreground=PLOT_FG, antialias=False)


# --- graph colours on a dark background ----------------------------------------------------
# Flight Review's palette is made for white plots: its black and dark-grey
# lines would vanish on a dark one, so only those two are lifted.
_DARK_SWAPS = {"#000000": "#e6e6e6", "#464646": "#a0a0a0", "k": "#d0d0d0", "#555": "#8a8a8a",
               "#8e24aa": "#e07ad8", "#b8860b": "#e3b341"}   # message markers


def plot_color(color: str) -> str:
    if not _active:
        return color
    return _DARK_SWAPS.get(color.lower() if isinstance(color, str) else color, color)


def legend_style() -> dict:
    if _active:
        return {"brush": pg.mkBrush(30, 30, 30, 225), "pen": pg.mkPen(BORDER),
                "labelTextColor": TEXT}
    return {"brush": pg.mkBrush(255, 255, 255, 215), "pen": pg.mkPen(200, 200, 200)}


def title_color() -> str | None:
    return PLOT_TEXT if _active else None


def style_role(widget, role: str, fallback_css: str = ""):
    """Small text roles ('dim', 'info', 'banner'): styled by the theme when it
    is active, otherwise by the given plain stylesheet."""
    if _active:
        widget.setProperty("role", role)
        widget.setStyleSheet("")
    elif fallback_css:
        widget.setStyleSheet(fallback_css)


def mode_alpha(base: int) -> int:
    """Flight-mode shading: colours look much stronger on a dark background."""
    return int(base * 0.55) if _active else base


def row_tint(color) -> "QColor":
    """Table row highlight (warnings/errors): translucent on the dark theme."""
    c = QColor(color)
    if _active:
        c.setAlpha(45)
    return c
