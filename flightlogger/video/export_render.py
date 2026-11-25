"""Draws the notes / messages overlay of each export state to a transparent PNG.

The boxes are real QLabels styled with ``box_style`` and laid out like the live overlay in
``mpv_widget.MpvWidget`` (same size fractions, margins, gap and placement rules), then painted
into an ARGB image of the output video size. Works without a screen (QT_QPA_PLATFORM=offscreen).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QLabel, QWidget

from .mpv_widget import MpvWidget
from .overlay_model import OV_MAX_BOXES, admit, box_style, flash_level, place_boxes

OV_FONT_FRAC = MpvWidget.OV_FONT_FRAC
OV_WIDTH_FRAC = MpvWidget.OV_WIDTH_FRAC


class _Renderer:
    def __init__(self, out_size: tuple[int, int], scale: float):
        self.W, self.H = int(out_size[0]), int(out_size[1])
        self.px = max(8, round(self.H * OV_FONT_FRAC * scale))
        self.w = max(120, min(int(self.W * 0.5), int(self.W * OV_WIDTH_FRAC * max(scale, 1.0))))
        self.gap = max(2, round(self.px * 0.3))
        self.margin = max(6, round(self.px * 0.77))
        self.overlay = QWidget()
        self.overlay.setAttribute(Qt.WA_TranslucentBackground)
        self.overlay.setObjectName("videoOverlay")
        self.overlay.setStyleSheet("#videoOverlay { background: transparent; }")
        self.boxes: dict = {}      # key -> QLabel currently shown
        self.ys: dict = {}         # key -> y of its box

    def _box(self) -> QLabel:
        box = QLabel(self.overlay)
        box.setTextFormat(Qt.RichText)
        box.setWordWrap(True)
        return box

    def draw(self, items, image: QImage) -> None:
        """Lay out ``items`` ((html, flash, key) each) and paint them onto ``image``."""
        keyed = {it[2] if len(it) > 2 else it[0]: (it[0], it[1]) for it in items}
        shown = admit(self.boxes, keyed, OV_MAX_BOXES)
        for key in [k for k in self.boxes if k not in shown]:
            self.boxes.pop(key).deleteLater()
            self.ys.pop(key, None)
        for key in shown:
            html, flash = keyed[key]
            box = self.boxes.get(key)
            if box is None:
                box = self.boxes[key] = self._box()
            box.setStyleSheet(box_style(flash_level(flash), self.px))
            box.setText(html)
            box.show()
        order = sorted((k for k in self.boxes if self.ys.get(k) is not None), key=lambda k: self.ys[k])
        order += [k for k in self.boxes if self.ys.get(k) is None]
        for k in order:
            box = self.boxes[k]
            box.ensurePolished()
            h = box.heightForWidth(self.w)
            box.resize(self.w, h if h > 0 else box.sizeHint().height())
        ys = place_boxes({k: self.ys.get(k) for k in order},
                         {k: self.boxes[k].height() for k in order}, self.gap)
        for k in order:
            self.ys[k] = ys[k]
            self.boxes[k].move(0, ys[k])
        painter = QPainter(image)
        try:
            if self.boxes:
                height = max(self.ys[k] + self.boxes[k].height() for k in order)
                self.overlay.setFixedSize(self.w, height)
                self.overlay.render(painter, QPoint(self.W - self.w - self.margin, self.margin))
        finally:
            painter.end()

    def close(self):
        self.overlay.deleteLater()


def _signature(r: _Renderer, items) -> tuple:
    """What a state looks like, to reuse identical PNGs: items with flash level (placement is
    deterministic given the sequence, so equal items right after each other draw the same;
    the cache is only used for exact repeats of the previous placement)."""
    return tuple((it[2] if len(it) > 2 else it[0], it[0], flash_level(it[1])) for it in items)


def render_states(states, out_size: tuple[int, int], scale: float, tmp_dir,
                  progress: Callable[[int, int], None] | None = None,
                  cancel: Callable[[], bool] | None = None):
    """Render each state to a PNG in ``tmp_dir``.

    Returns (png_paths, empty_png): one path per state (None for a state with no boxes) and the
    path of a fully transparent image of the output size (for those). ``progress(done, total)``
    after each state; if ``cancel()`` turns true, stops and returns what it has (callers check
    their own cancel flag)."""
    tmp = Path(tmp_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    W, H = int(out_size[0]), int(out_size[1])
    empty = tmp / "empty.png"
    img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(0, 0, 0, 0))
    img.save(str(empty), "PNG")

    r = _Renderer((W, H), scale)
    paths: list = []
    cache: dict = {}
    try:
        for i, st in enumerate(states):
            if cancel is not None and cancel():
                break
            if not st.items:
                r.draw([], img)       # keeps the placement state in step (all boxes dropped)
                paths.append(None)
            else:
                img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
                img.fill(QColor(0, 0, 0, 0))
                r.draw(st.items, img)
                ys = tuple(sorted((str(k), y) for k, y in r.ys.items()))
                sig = (_signature(r, st.items), ys)
                p = cache.get(sig)
                if p is None:
                    p = str(tmp / f"state_{i:05d}.png")
                    img.save(p, "PNG")
                    cache[sig] = p
                paths.append(p)
            if progress:
                progress(i + 1, len(states))
    finally:
        r.close()
    return paths, str(empty)
