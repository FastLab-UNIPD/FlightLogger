"""Plot editor (series colors / thickness / line style, title) and the light-mode export."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QGridLayout, QLabel, QLineEdit, QPushButton,
                               QWidget)

from ..ui import theme

STYLES = {"Solid": Qt.SolidLine, "Dash": Qt.DashLine, "Dot": Qt.DotLine, "Dash-dot": Qt.DashDotLine}


def series_state(item: pg.PlotDataItem) -> dict:
    """Current look of a series (scatter series have no line: size instead of width)."""
    pen = item.opts.get("pen")
    scatter = pen is None
    if scatter:
        sp = pg.mkPen(item.opts.get("symbolPen"))
        return {"color": sp.color().name(), "width": float(item.opts.get("symbolSize", 5)),
                "style": "Solid", "visible": item.isVisible(), "scatter": True}
    pen = pg.mkPen(pen)
    style = next((k for k, v in STYLES.items() if v == pen.style()), "Solid")
    return {"color": pen.color().name(), "width": pen.widthF(), "style": style,
            "visible": item.isVisible(), "scatter": False}


def apply_series_state(item: pg.PlotDataItem, st: dict) -> None:
    color = QColor(st.get("color", "#ffffff"))
    width = float(st.get("width", 1.5))
    if item.opts.get("pen") is None:   # scatter
        item.setSymbolPen(pg.mkPen(color, width=1.5))
        item.setSymbolSize(max(2.0, width))
    else:
        item.setPen(pg.mkPen(color, width=width, style=STYLES.get(st.get("style"), Qt.SolidLine)))
    item.setVisible(bool(st.get("visible", True)))


class PlotEditDialog(QDialog):
    """Edit one plot live; Cancel restores what it looked like before."""

    def __init__(self, plot, parent=None):
        super().__init__(parent)
        self.plot = plot
        self.setWindowTitle("Edit plot")
        self.setMinimumWidth(520)
        self._items = plot.editable_series()
        self._before = [series_state(it) for _, it in self._items]
        self._title_before = plot.title
        self._rows = []

        g = QGridLayout(self)
        g.addWidget(QLabel("Title"), 0, 0)
        self.title = QLineEdit(plot.title)
        self.title.textEdited.connect(lambda s: plot.set_title(s))
        g.addWidget(self.title, 0, 1, 1, 5)
        for c, h in enumerate(("Show", "Series", "Color", "Width / size", "Style")):
            lab = QLabel(f"<b>{h}</b>")
            g.addWidget(lab, 1, c)
        for i, ((name, item), st) in enumerate(zip(self._items, self._before), start=2):
            vis = QCheckBox()
            vis.setChecked(st["visible"])
            color = QPushButton()
            color.setFixedWidth(48)
            width = QDoubleSpinBox(minimum=0.5, maximum=12, singleStep=0.5, decimals=1)
            width.setValue(st["width"])
            style = QComboBox()
            style.addItems(list(STYLES))
            style.setCurrentText(st["style"])
            style.setEnabled(not st["scatter"])
            row = {"item": item, "color": st["color"], "vis": vis, "btn": color, "width": width,
                   "style": style}
            self._rows.append(row)
            self._paint_btn(row)
            color.clicked.connect(lambda _c=False, r=row: self._pick(r))
            for w in (vis, width):
                (w.toggled if w is vis else w.valueChanged).connect(lambda *_a, r=row: self._push(r))
            style.currentTextChanged.connect(lambda *_a, r=row: self._push(r))
            g.addWidget(vis, i, 0)
            g.addWidget(QLabel(name), i, 1)
            g.addWidget(color, i, 2)
            g.addWidget(width, i, 3)
            g.addWidget(style, i, 4)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        g.addWidget(bb, len(self._rows) + 2, 0, 1, 5)

    @staticmethod
    def _paint_btn(row):
        row["btn"].setStyleSheet(f"background: {row['color']}; border: 1px solid #777;")

    def _pick(self, row):
        c = QColorDialog.getColor(QColor(row["color"]), self, "Series color")
        if c.isValid():
            row["color"] = c.name()
            self._paint_btn(row)
            self._push(row)

    def _push(self, row):
        apply_series_state(row["item"], {"color": row["color"], "width": row["width"].value(),
                                         "style": row["style"].currentText(),
                                         "visible": row["vis"].isChecked()})
        self.plot.style_changed()

    def reject(self):
        for (_, it), st in zip(self._items, self._before):
            apply_series_state(it, st)
        self.plot.set_title(self._title_before)
        self.plot.style_changed()
        super().reject()


# --- light-mode export ------------------------------------------------------------------
_LIGHT_FROM_DARK = {v: k for k, v in theme._DARK_SWAPS.items() if k.startswith("#")}


def _lighten(color: QColor) -> QColor:
    """Colors lifted for the dark theme go back to Flight Review's print colors."""
    return QColor(_LIGHT_FROM_DARK.get(color.name(), color.name()))


def export_light(plot, path: str, width: int = 1600) -> None:
    """Render one plot on white (black axes and text) to a PNG/SVG, then restore the dark look."""
    from pyqtgraph.exporters import ImageExporter, SVGExporter
    pi = plot.item
    saved = [(it, series_state(it)) for _, it in plot.editable_series()]
    cursor_vis = plot.cursor.isVisible() if plot.cursor is not None else False
    black = pg.mkPen("k")
    try:
        for it, st in saved:
            apply_series_state(it, dict(st, color=_lighten(QColor(st["color"])).name()))
        for name in (("left", "bottom") + (("right",) if plot.right is not None else ())):
            ax = pi.getAxis(name)
            ax.setPen(black)
            ax.setTextPen(black)
            if ax.labelText:
                ax.setLabel(ax.labelText, units=ax.labelUnits, color="#000000")
        pi.setTitle(plot.title, **dict(plot._title_opts, color="#000000"))
        plot.legend.setBrush(pg.mkBrush(255, 255, 255, 230))
        plot.legend.setPen(pg.mkPen(180, 180, 180))
        for _s, lbl in plot.legend.items:
            lbl.setText(lbl.text, color="#000000")
        if plot.cursor is not None:
            plot.cursor.setVisible(False)
        exp = SVGExporter(pi) if path.lower().endswith(".svg") else ImageExporter(pi)
        exp.parameters()["background"] = QColor("white")
        if isinstance(exp, ImageExporter):
            exp.parameters()["width"] = width
        exp.export(path)
    finally:
        for it, st in saved:
            apply_series_state(it, st)
        fg = pg.mkPen(theme.PLOT_FG)
        for name in (("left", "bottom") + (("right",) if plot.right is not None else ())):
            ax = pi.getAxis(name)
            ax.setPen(fg)
            ax.setTextPen(fg)
            if ax.labelText:
                ax.setLabel(ax.labelText, units=ax.labelUnits, color=theme.PLOT_FG)
        pi.setTitle(plot.title, **plot._title_opts)
        plot.legend.setBrush(theme.legend_style()["brush"])
        plot.legend.setPen(theme.legend_style()["pen"])
        for _s, lbl in plot.legend.items:
            lbl.setText(lbl.text, color=theme.TEXT if theme.is_themed() else "#000000")
        if plot.cursor is not None:
            plot.cursor.setVisible(cursor_vis)
