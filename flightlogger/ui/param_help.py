"""Parameter help: small tool window with a PX4 parameter's definition (key H in the table),
and the "Update parameter definitions" action."""
from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (QApplication, QMessageBox, QProgressDialog, QTextBrowser, QVBoxLayout,
                               QWidget)

from ..log import param_meta


class ParamHelpPopup(QWidget):
    """Non-modal, reused; Esc closes."""

    _instance: "ParamHelpPopup | None" = None

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool)
        self.setWindowTitle("Parameter help")
        self.resize(460, 420)
        self.browser = QTextBrowser()
        self.browser.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
                                             | Qt.LinksAccessibleByMouse)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.browser)

    @classmethod
    def instance(cls, parent=None) -> "ParamHelpPopup":
        if cls._instance is None:
            cls._instance = cls(parent)
        return cls._instance

    @classmethod
    def show_for(cls, name: str, value=None, default=None, parent=None) -> "ParamHelpPopup":
        p = cls.instance(parent)
        p.set_param(name, value, default)
        p.show()
        p.raise_()
        return p

    def set_param(self, name: str, value=None, default=None):
        self.setWindowTitle(f"Parameter help - {name}")
        self.browser.setHtml(param_meta.format_html(name, param_meta.lookup(name), value, default))

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(ev)


class _UpdateThread(QThread):
    progress = Signal(int, int)
    done = Signal(int, str)  # parameter count, error

    def run(self):
        try:
            n = param_meta.update_from_url(progress=lambda d, t: self.progress.emit(d, t))
            self.done.emit(n, "")
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.done.emit(0, f"{type(e).__name__}: {e}")


def update_definitions(parent: QWidget, interactive: bool = True) -> QThread:
    """Download the newest PX4 parameter definitions in a thread, with a progress dialog."""
    dlg = QProgressDialog("Downloading PX4 parameter definitions…", None, 0, 0, parent)
    dlg.setWindowTitle("Update parameter definitions")
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(0)
    th = _UpdateThread(parent)

    def progress(d, t):
        if t:
            dlg.setMaximum(t)
            dlg.setValue(min(d, t))

    def done(n, err):
        dlg.close()
        dlg.deleteLater()
        if not interactive:
            return
        if err:
            QMessageBox.warning(parent, "Update parameter definitions", f"Update failed:\n{err}")
        else:
            QMessageBox.information(parent, "Update parameter definitions",
                                    f"Loaded definitions for {n} parameters "
                                    f"(source: {param_meta.source_info().get('source', '')}).")

    th.progress.connect(progress)
    th.done.connect(done)
    th.finished.connect(th.deleteLater)
    if interactive:
        dlg.show()
    th.start()
    return th
