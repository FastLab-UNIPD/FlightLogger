"""Thin wrapper around Qt Advanced Docking System (PySide6-QtAds).

Every panel is a CDockWidget: it can be split side by side, tabbed, or torn off
into a floating window (which may hold several panels) on any monitor.
"""
from __future__ import annotations

import PySide6QtAds as QtAds
from PySide6QtAds import CDockManager, CDockWidget

LEFT = QtAds.LeftDockWidgetArea
RIGHT = QtAds.RightDockWidgetArea
TOP = QtAds.TopDockWidgetArea
BOTTOM = QtAds.BottomDockWidgetArea
CENTER = QtAds.CenterDockWidgetArea


def configure_flags():
    """Must run before the CDockManager is created."""
    F = CDockManager.eConfigFlag
    for flag, on in [(F.OpaqueSplitterResize, True), (F.FocusHighlighting, True),
                     (F.XmlCompressionEnabled, False), (F.EqualSplitOnInsertion, True),
                     (F.FloatingContainerHasWidgetTitle, True),
                     (F.DockAreaHasUndockButton, True), (F.AllTabsHaveCloseButton, True)]:
        CDockManager.setConfigFlag(flag, on)


class DockHost:
    def __init__(self, main_window):
        configure_flags()
        self.mgr = CDockManager(main_window)

    def make(self, widget, title: str, name: str, deletable: bool = False) -> CDockWidget:
        dw = CDockWidget(self.mgr, title)
        dw.setObjectName(name)
        dw.setWidget(widget)
        F = CDockWidget.DockWidgetFeature
        dw.setFeature(F.DockWidgetDeleteOnClose, deletable)
        return dw

    def add(self, dw: CDockWidget, area, target=None):
        """target: a CDockAreaWidget to split/tab against (None = outer edge)."""
        if target is not None:
            return self.mgr.addDockWidget(area, dw, target)
        return self.mgr.addDockWidget(area, dw)
