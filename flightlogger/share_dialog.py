"""Shareable project export dialog and a helper to run work in a thread with a progress dialog."""
from __future__ import annotations

from PySide6.QtCore import QEventLoop, QThread, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressDialog,
                               QPushButton, QVBoxLayout)


class _Worker(QThread):
    progress = Signal(object, object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.cancelled = False
        self.result = None
        self.error: BaseException | None = None

    def run(self):
        try:
            self.result = self.fn(lambda d, t: self.progress.emit(d, t), lambda: self.cancelled)
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller
            self.error = e


def run_with_progress(parent, label: str, fn, cancelable: bool = False, interactive: bool = True,
                      min_ms: int = 400):
    """Run fn(progress, cancel) and return its result, with a progress dialog if it takes long.

    Blocks (local event loop) until fn is done, so callers can treat it as synchronous. Without
    a GUI (interactive False) fn simply runs in the calling thread. Exceptions are re-raised."""
    if not interactive:
        return fn(lambda d, t: None, lambda: False)
    th = _Worker(fn)
    dlg = QProgressDialog(label, "Cancel" if cancelable else None, 0, 0, parent)
    dlg.setWindowTitle("FlightLogger")
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(min_ms)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    loop = QEventLoop()

    def progress(done, total):
        if total:
            dlg.setMaximum(int(total // 1024))
            dlg.setValue(int(done // 1024))
            dlg.setLabelText(f"{label}\n{done / 1e6:.0f} of {total / 1e6:.0f} MB")

    th.progress.connect(progress)
    th.finished.connect(loop.quit)
    if cancelable:
        dlg.canceled.connect(lambda: setattr(th, "cancelled", True))
    th.start()
    if not th.isFinished():
        loop.exec()
    th.wait()
    dlg.close()
    dlg.deleteLater()
    if th.error is not None:
        raise th.error
    return th.result


class ShareDialog(QDialog):
    """Options for File > Export shareable project."""

    def __init__(self, parent, dest: str, name: str, has_log: bool, video_mb: float | None):
        super().__init__(parent)
        self.setWindowTitle("Export shareable project")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Copies the log, the video and a project file into one folder\n"
                             "that can be sent to someone else."))
        form = QFormLayout()
        row = QHBoxLayout()
        self.dest = QLineEdit(dest)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.dest, 1)
        row.addWidget(browse)
        form.addRow("Destination folder:", row)
        self.name = QLineEdit(name)
        form.addRow("Folder name:", self.name)
        lay.addLayout(form)
        self.log_cb = QCheckBox("Include log")
        self.log_cb.setChecked(has_log)
        self.log_cb.setEnabled(has_log)
        self.video_cb = QCheckBox("Include video" + (f" ({video_mb:.0f} MB)" if video_mb else ""))
        self.video_cb.setChecked(video_mb is not None)
        self.video_cb.setEnabled(video_mb is not None)
        self.zip_cb = QCheckBox("Also create a .zip")
        for cb in (self.log_cb, self.video_cb, self.zip_cb):
            lay.addWidget(cb)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Destination folder", self.dest.text())
        if d:
            self.dest.setText(d)

    def _accept(self):
        if not self.dest.text().strip() or not self.name.text().strip():
            return
        if set('/\\:*?"<>|') & set(self.name.text()):
            QMessageBox.warning(self, "Folder name", "The name cannot contain / \\ : * ? \" < > |")
            return
        self.accept()

    def values(self) -> dict:
        return {"dest": self.dest.text().strip(), "name": self.name.text().strip(),
                "include_log": self.log_cb.isChecked(), "include_video": self.video_cb.isChecked(),
                "make_zip": self.zip_cb.isChecked()}
