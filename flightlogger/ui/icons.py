"""Line icons in the OmniQuadGUI style (24 px grid, 2 px round strokes).

Every icon is drawn in ``currentColor`` and recoloured per state: soft
lavender on normal buttons, white on primary (lavender) buttons and on
toggles that are on, grey when disabled.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme

_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
         'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
         'stroke-linejoin="round">')
_FILL = 'fill="currentColor"'

SVG = {
    # transport
    "play": f'<path d="M7.5 4.8v14.4a.8.8 0 0 0 1.2.7l11.4-7.2a.8.8 0 0 0 0-1.4L8.7 4.1a.8.8 0 0 0-1.2.7z" {_FILL}/>',
    "pause": f'<rect x="6.5" y="5" width="4" height="14" rx="1.2" {_FILL}/>'
             f'<rect x="13.5" y="5" width="4" height="14" rx="1.2" {_FILL}/>',
    "to_start": f'<path d="M6 5v14"/><path d="M18.5 6.4v11.2a.7.7 0 0 1-1.1.6l-7.9-5.6a.7.7 0 0 1 0-1.2l7.9-5.6a.7.7 0 0 1 1.1.6z" {_FILL}/>',
    "rewind": f'<path d="M11 6.6v10.8a.6.6 0 0 1-1 .5l-6.1-5.4a.6.6 0 0 1 0-1L10 6.1a.6.6 0 0 1 1 .5z" {_FILL}/>'
              f'<path d="M20 6.6v10.8a.6.6 0 0 1-1 .5l-6.1-5.4a.6.6 0 0 1 0-1L19 6.1a.6.6 0 0 1 1 .5z" {_FILL}/>',
    "forward": f'<path d="M4 6.6v10.8a.6.6 0 0 0 1 .5l6.1-5.4a.6.6 0 0 0 0-1L5 6.1a.6.6 0 0 0-1 .5z" {_FILL}/>'
               f'<path d="M13 6.6v10.8a.6.6 0 0 0 1 .5l6.1-5.4a.6.6 0 0 0 0-1L14 6.1a.6.6 0 0 0-1 .5z" {_FILL}/>',
    "step_back": '<path d="M7 6v12"/><path d="M17 6l-7 6 7 6"/>',
    "step_forward": '<path d="M17 6v12"/><path d="M7 6l7 6-7 6"/>',
    # video
    "volume": f'<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" {_FILL}/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
    "mute": f'<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" {_FILL}/><path d="M16 9.5l5 5M21 9.5l-5 5"/>',
    "fullscreen": '<path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1"/>'
            '<path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
    "sync": '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/><path d="M4 4v4h4"/>'
            '<path d="M4 13a8 8 0 0 0 14.3 4.9L20 16"/><path d="M20 20v-4h-4"/>',
    "target": f'<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3" {_FILL}/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4L7 17M17 7l1.4-1.4"/>',
    "moon": '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
    "redo": '<path d="M20 10H9a5 5 0 0 0 0 10h6"/><path d="M16 6l4 4-4 4"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13 7l4 4"/>',
    "undo": '<path d="M4 10h11a5 5 0 0 1 0 10H9"/><path d="M8 6l-4 4 4 4"/>',
    # graphs
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "follow": f'<circle cx="12" cy="12" r="7"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>'
              f'<circle cx="12" cy="12" r="1.6" {_FILL}/>',
    "fit": '<path d="M3 12h18"/><path d="M7 8l-4 4 4 4M17 8l4 4-4 4"/>',
    "graph": '<path d="M4 4v16h16"/><path d="M7 14l4-4 3 3 5-6"/>',
    "check_all": '<path d="M4 12.5l5 5L20 6.5"/>',
    "clear": '<path d="M6 6l12 12M18 6L6 18"/>',
    # notes / files
    "note": '<path d="M5 4h14v10l-6 6H5z"/><path d="M13 20v-6h6"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/>'
             '<path d="M9 7V4h6v3"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "save": '<path d="M5 4h11l3 3v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1z"/><path d="M8 4v5h7V4M8 20v-6h8v6"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    "film": '<rect x="3" y="5" width="18" height="14" rx="2"/>'
            '<path d="M7 5v14M17 5v14M3 9.5h4M3 14.5h4M17 9.5h4M17 14.5h4"/>',
    "layout": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M9 11h12"/>',
    "log": '<path d="M7 3h7l5 5v11.5a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 6 19.5v-15A1.5 1.5 0 0 1 7.5 3z"/>'
           '<path d="M9.5 12h5M9.5 16h5"/>',
}

_cache: dict = {}


def _render(name: str, color: str, px: int) -> QPixmap:
    svg = (_HEAD + SVG[name] + "</svg>").replace("currentColor", color)
    r = QSvgRenderer(QByteArray(svg.encode()))
    img = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p, QRectF(0, 0, px, px))
    p.end()
    return QPixmap.fromImage(img)


def _colors(variant: str) -> tuple[str, str]:
    if theme.is_themed():
        normal = theme.ICON_PRIMARY if variant == "primary" else theme.ICON_NEUTRAL
        return normal, theme.ICON_DISABLED
    return ("#ffffff" if variant == "primary" else "#3c3c3c"), "#a8a8a8"


def icon(name: str, variant: str = "neutral", checkable: bool = False) -> QIcon:
    """variant 'neutral' or 'primary'; checkable: white icon while checked."""
    key = (name, variant, checkable, theme.is_themed(), theme.ICON_NEUTRAL)
    if key in _cache:
        return _cache[key]
    normal, disabled = _colors(variant)
    ic = QIcon()
    for px in (16, 32, 48):
        ic.addPixmap(_render(name, normal, px), QIcon.Normal, QIcon.Off)
        ic.addPixmap(_render(name, disabled, px), QIcon.Disabled, QIcon.Off)
        if checkable:
            on = theme.ICON_PRIMARY if theme.is_themed() else "#ffffff"
            ic.addPixmap(_render(name, on, px), QIcon.Normal, QIcon.On)
            ic.addPixmap(_render(name, disabled, px), QIcon.Disabled, QIcon.On)
    _cache[key] = ic
    return ic


def decorate(button, name: str, variant: str = "neutral", size: int = 16):
    """Give a QPushButton/QToolButton an icon, and the variant style (see theme.QSS)."""
    button.setIcon(icon(name, variant, checkable=button.isCheckable()))
    button.setIconSize(QSize(size, size))
    if variant != "neutral":
        button.setProperty("variant", variant)
    return button
