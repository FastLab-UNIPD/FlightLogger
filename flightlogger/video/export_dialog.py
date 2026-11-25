"""File > Export video with overlay…: choose range, overlay content, encoder preset, size, file."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
                               QPushButton, QRadioButton, QVBoxLayout)

from ..project import Settings
from .export import RESOLUTIONS, available_presets, ffmpeg_available, output_size, probe_encoders

RANGES = (("video", "Whole video"),
          ("log", "Part covered by the log"),
          ("visible", "Visible time range"),
          ("zoom", "Current plot zoom"))
CORNERS = (("bottom-left", "Bottom left"), ("bottom-right", "Bottom right"),
           ("top-left", "Top left"), ("top-right", "Top right"))
RESOLUTION_LABELS = {"original": "Original", "1080p": "1080p", "720p": "720p"}
INSTALL_MESSAGE = ("Exporting a video needs ffmpeg, which was not found on this computer.\n\n"
                   "Install it with:\n    sudo apt install ffmpeg\n\nthen try again.")


def fmt_duration(s: float | None) -> str:
    if s is None:
        return ""
    m, sec = divmod(max(s, 0.0), 60)
    return f"{int(m)}:{sec:04.1f}"


class VideoExportDialog(QDialog):
    """``durations``: range kind -> seconds (None = not available, with ``reasons[kind]``)."""

    def __init__(self, parent, durations: dict, reasons: dict, src_size: tuple[int, int],
                 default_name: str, default_dir: str, settings: Settings | None = None):
        super().__init__(parent)
        self.setWindowTitle("Export video with overlay")
        self.settings = settings or Settings()
        self._encoders: dict | None = None
        S = self.settings.get

        lay = QVBoxLayout(self)
        box = QGroupBox("Part of the video")
        bl = QVBoxLayout(box)
        self._group = QButtonGroup(self)
        self._radios: dict[str, QRadioButton] = {}
        for kind, label in RANGES:
            d = durations.get(kind)
            rb = QRadioButton(f"{label}  ({fmt_duration(d)})" if d is not None else label)
            if d is None:
                rb.setEnabled(False)
                rb.setToolTip(reasons.get(kind, "Not available"))
            self._group.addButton(rb)
            self._radios[kind] = rb
            bl.addWidget(rb)
        want = S("video_export_range", "log")
        chosen = want if durations.get(want) is not None else next(
            (k for k, _ in RANGES if durations.get(k) is not None), "video")
        self._radios[chosen].setChecked(True)
        lay.addWidget(box)

        box = QGroupBox("Drawn on the video")
        bl = QVBoxLayout(box)
        self.notes_cb = QCheckBox("Notes (visible sets only)")
        self.notes_cb.setChecked(bool(S("video_export_notes", True)))
        self.msgs_cb = QCheckBox("PX4 messages")
        self.msgs_cb.setChecked(bool(S("video_export_messages", True)))
        self.clock_cb = QCheckBox("Flight clock (T+mm:ss.s)")
        self.clock_cb.setChecked(bool(S("video_export_clock", False)))
        self.corner = QComboBox()
        for k, label in CORNERS:
            self.corner.addItem(label, k)
        self.corner.setCurrentIndex(max(self.corner.findData(S("video_export_corner", "bottom-left")), 0))
        self.corner.setEnabled(self.clock_cb.isChecked())
        self.clock_cb.toggled.connect(self.corner.setEnabled)
        row = QHBoxLayout()
        row.addWidget(self.clock_cb)
        row.addWidget(self.corner)
        row.addStretch(1)
        bl.addWidget(self.notes_cb)
        bl.addWidget(self.msgs_cb)
        bl.addLayout(row)
        lay.addWidget(box)

        form = QFormLayout()
        self.preset = QComboBox()
        self.preset.addItem("Detecting encoders…", None)
        self.preset.setEnabled(False)
        form.addRow("Speed / quality", self.preset)
        self.resolution = QComboBox()
        for key, label in RESOLUTION_LABELS.items():
            tgt = RESOLUTIONS[key]
            if tgt is None or tgt < src_size[1]:
                w, h = output_size(src_size[0], src_size[1], key)
                self.resolution.addItem(f"{label} ({w}×{h})", key)
        self.resolution.setCurrentIndex(
            max(self.resolution.findData(S("video_export_resolution", "original")), 0))
        form.addRow("Resolution", self.resolution)
        self.path = QLineEdit(str(Path(default_dir) / default_name))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        prow = QHBoxLayout()
        prow.addWidget(self.path, 1)
        prow.addWidget(browse)
        form.addRow("Save as", prow)
        lay.addLayout(form)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Export")
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.setMinimumWidth(520)
        QTimer.singleShot(0, self._load_encoders)   # after the dialog is on screen

    def _load_encoders(self):
        try:
            self._encoders = probe_encoders()    # cached after the first run
        except Exception:
            self._encoders = {}
        self.preset.clear()
        first = None
        saved = self.settings.get("video_export_preset", "fast_gpu")
        for key, label, enabled, reason in available_presets(self._encoders):
            self.preset.addItem(label if enabled else f"{label} (not available)", key)
            idx = self.preset.count() - 1
            if not enabled:
                item = self.preset.model().item(idx)
                item.setEnabled(False)
                item.setToolTip(reason)
            elif first is None or key == saved:
                first = idx
        self.preset.setEnabled(first is not None)
        if first is not None:
            self.preset.setCurrentIndex(first)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(first is not None)

    def _browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "Export video", self.path.text(),
                                           "MP4 video (*.mp4)")
        if p:
            self.path.setText(p if Path(p).suffix else p + ".mp4")

    def _accept(self):
        if not self.path.text().strip():
            return
        s = self.settings
        s.set("video_export_range", self.range_kind())
        s.set("video_export_notes", self.notes_cb.isChecked())
        s.set("video_export_messages", self.msgs_cb.isChecked())
        s.set("video_export_clock", self.clock_cb.isChecked())
        s.set("video_export_corner", self.corner.currentData())
        s.set("video_export_preset", self.preset.currentData())
        s.set("video_export_resolution", self.resolution.currentData())
        self.accept()

    def range_kind(self) -> str:
        for kind, rb in self._radios.items():
            if rb.isChecked():
                return kind
        return "video"

    def values(self) -> dict:
        p = self.path.text().strip()
        if not Path(p).suffix:
            p += ".mp4"
        return {"out_path": p, "range_kind": self.range_kind(),
                "notes_on": self.notes_cb.isChecked(), "msgs_on": self.msgs_cb.isChecked(),
                "clock_pos": self.corner.currentData() if self.clock_cb.isChecked() else None,
                "preset_key": self.preset.currentData(),
                "resolution": self.resolution.currentData()}


def ffmpeg_missing_message() -> str | None:
    """None when ffmpeg is usable, else the text to show the user."""
    ok, _msg = ffmpeg_available()
    return None if ok else INSTALL_MESSAGE
