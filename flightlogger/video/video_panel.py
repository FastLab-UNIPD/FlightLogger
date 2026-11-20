"""Video dock: the mpv surface plus video transport and sync controls.

Sync workflow:
  1. Uncheck "Linked".
  2. Put the video on a recognizable moment (scrub bar, frame steps).
  3. Put the log cursor on the matching instant (click a graph, or pick an
     event such as Takeoff from "Jump log to").
  4. Press "Sync here": the offset is stored and video and log are linked again.
Fine-tune any time with the ±frame buttons (the video frame stays, the log
cursor moves) or by typing the offset.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QSizePolicy, QSlider, QStackedLayout, QToolButton,
                               QVBoxLayout, QWidget)

from ..log.ulog_loader import FlightLog
from ..playback import Playback
from ..plots.common import fmt_time
from ..timebus import TimeBus
from ..ui import icons, theme
from .mpv_widget import MpvWidget, mpv_available

SLIDER_RES = 1000  # slider steps per second


class _FullscreenWindow(QWidget):
    closed = Signal()

    def __init__(self):
        super().__init__(None, Qt.Window)
        self.setWindowTitle("FlightLogger video")
        self.setStyleSheet("background: black;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.lay = lay

    def closeEvent(self, ev):
        self.closed.emit()
        super().closeEvent(ev)


class VideoPanel(QWidget):
    openRequested = Signal()

    def __init__(self, bus: TimeBus, playback: Playback, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.pb = playback
        self.video: MpvWidget | None = None
        self.log: FlightLog | None = None
        self._fs: _FullscreenWindow | None = None
        self._dragging = False
        self._pending_path: str | None = None

        # --- surface / placeholder ---
        ok, msg = mpv_available()
        self.mpv_ok = ok
        self.placeholder = QWidget()
        ph = QVBoxLayout(self.placeholder)
        self.ph_label = QLabel("No video loaded.\nOpen one (Ctrl+Shift+O) or drop a video file on the window."
                               if ok else msg, alignment=Qt.AlignCenter)
        self.ph_label.setWordWrap(True)
        theme.style_role(self.ph_label, "dim", "color: gray;")
        self.ph_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_btn = icons.decorate(QPushButton("Open video…"), "film", "primary")
        open_btn.setEnabled(ok)
        open_btn.clicked.connect(self.openRequested.emit)
        ph.addStretch(1)
        ph.addWidget(self.ph_label)
        ph.addWidget(open_btn, 0, Qt.AlignCenter)
        ph.addStretch(1)
        self.surface_host = QWidget()
        self.surface_host.setStyleSheet("background: black;")
        self.surface_lay = QVBoxLayout(self.surface_host)
        self.surface_lay.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedLayout()
        self.stack.addWidget(self.placeholder)
        self.stack.addWidget(self.surface_host)

        # --- video transport ---
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setEnabled(False)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.slider.sliderReleased.connect(self._slider_released)
        self.slider.sliderMoved.connect(lambda v: self.pb.seek_video(v / SLIDER_RES))
        self.vtime = QLabel("–")
        self.vtime.setStyleSheet("font-family: monospace; background: transparent;")
        row1 = QHBoxLayout()
        row1.addWidget(self.slider, 1)
        row1.addWidget(self.vtime)

        def tb(text, tip, slot, checkable=False, icon=None, variant="neutral"):
            b = QToolButton(text=text, toolTip=tip, checkable=checkable)
            b.setFocusPolicy(Qt.NoFocus)
            (b.toggled if checkable else b.clicked).connect(slot)
            if icon:
                icons.decorate(b, icon, variant)
            return b

        self.back_btn = tb("◀|", "Previous frame (←)", lambda: self.pb.step_frames(-1), icon="step_back")
        self.play_btn = tb("▶", "Play / pause (Space)", self.pb.toggle, icon="play", variant="primary")
        self.fwd_btn = tb("|▶", "Next frame (→)", lambda: self.pb.step_frames(1), icon="step_forward")
        self.link_btn = tb("Linked", "Video and log move together. Uncheck to line them up, "
                           "then press 'Sync here'.", self.pb.set_linked, checkable=True, icon="link")
        self.link_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.link_btn.setChecked(True)
        self.mute_btn = tb("🔊", "Mute", self._on_mute, checkable=True, icon="volume")
        self.fs_btn = tb("⛶", "Fullscreen on this panel's screen (double-click video, Esc to leave)",
                         self.toggle_fullscreen, icon="fullscreen")
        row2 = QHBoxLayout()
        for w in (self.back_btn, self.play_btn, self.fwd_btn, self.link_btn):
            row2.addWidget(w)
        row2.addStretch(1)
        row2.addWidget(self.mute_btn)
        row2.addWidget(self.fs_btn)

        # --- sync controls ---
        self.offset_spin = QDoubleSpinBox(decimals=3, minimum=-1e5, maximum=1e5, singleStep=0.1,
                                          suffix=" s", keyboardTracking=False)
        self.offset_spin.setToolTip("Video time at log time 0 (video_t = log_t + offset)")
        self.offset_spin.valueChanged.connect(self.pb.set_offset)
        self.minus_f = tb("−1f", "Shift sync by one frame (video frame stays, log moves)",
                          lambda: self.pb.nudge_frames(-1))
        self.plus_f = tb("+1f", "Shift sync by one frame (video frame stays, log moves)",
                         lambda: self.pb.nudge_frames(1))
        self.event_box = QComboBox(toolTip="Move the log cursor to a log event")
        self.event_box.setFocusPolicy(Qt.NoFocus)
        self.event_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.event_box.activated.connect(self._on_event_chosen)
        self.sync_btn = icons.decorate(QPushButton("Sync here"), "sync", "primary")
        self.sync_btn.setToolTip("The current video frame is the log cursor instant")
        self.sync_btn.clicked.connect(self.pb.sync_here)
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Offset"))
        row3.addWidget(self.offset_spin)
        row3.addWidget(self.minus_f)
        row3.addWidget(self.plus_f)
        row3.addWidget(self.event_box, 1)
        row3.addWidget(self.sync_btn)

        # second sync point: corrects camera / flight-controller clock drift
        self.point2_btn = icons.decorate(QPushButton("Add 2nd point"), "target")
        self.point2_btn.setToolTip("Late in the flight: unlink, line up another moment, then press "
                                   "this to correct clock drift between camera and log")
        self.point2_btn.clicked.connect(self._second_point)
        self.sync_label = QLabel()
        theme.style_role(self.sync_label, "dim", "color: gray;")
        self.drift_reset_btn = QToolButton(text="Reset drift",
                                           toolTip="Back to a single sync point (no drift correction)")
        self.drift_reset_btn.clicked.connect(self.pb.reset_drift)
        icons.decorate(self.drift_reset_btn, "undo")
        self.drift_reset_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        row4 = QHBoxLayout()
        row4.addWidget(self.sync_label, 1)
        row4.addWidget(self.drift_reset_btn)
        row4.addWidget(self.point2_btn)

        self.banner = QLabel("Unlinked: put the video and the log cursor on the same moment, "
                             "then press Sync here (or Add 2nd point, late in the flight, "
                             "to correct clock drift).")
        self.banner.setWordWrap(True)
        theme.style_role(self.banner, "banner", "background: #ffe7b0; color: #503000; padding: 3px;")
        self.info = QLabel()
        theme.style_role(self.info, "info", "color: gray; font-size: 11px;")
        self.info.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(3)
        lay.addLayout(self.stack, 1)
        lay.addLayout(row1)
        lay.addLayout(row2)
        lay.addLayout(row3)
        lay.addLayout(row4)
        lay.addWidget(self.banner)
        lay.addWidget(self.info)

        self.pb.playingChanged.connect(self._on_playing)
        self.pb.linkedChanged.connect(self._on_linked)
        self.pb.offsetChanged.connect(self._on_offset)
        self.pb.syncChanged.connect(self._update_sync_label)
        self.pb.videoTimeChanged.connect(self._on_video_time)
        self._on_linked(True)
        self._update_enabled()

    # --- loading ---
    def load(self, path: str) -> bool:
        if not self.mpv_ok:
            return False
        if self.video is None:
            self.video = MpvWidget()
            self.video.fileLoaded.connect(self._on_file_loaded)
            self.video.doubleClicked.connect(self.toggle_fullscreen)
            self.surface_lay.addWidget(self.video)
        self.pb.detach_video()
        self._pending_path = path
        self.stack.setCurrentWidget(self.surface_host)
        self.info.setText(f"Loading {Path(path).name}…")
        self.video.load(path)
        return True

    def _on_file_loaded(self):
        v = self.video
        info = v.refresh_info()
        if info.duration <= 0:
            return
        self.slider.setRange(0, int(info.duration * SLIDER_RES))
        self.info.setText(f"{Path(info.path).name}: {info.describe()}")
        self.pb.attach_video(v)
        self._update_enabled()

    @property
    def video_path(self) -> str | None:
        return self.video.info.path if self.video is not None and self.video.loaded else None

    def set_log(self, log: FlightLog | None):
        self.log = log
        self.event_box.clear()
        self.event_box.addItem("Jump log to…", None)
        if log is None:
            return
        self.event_box.addItem("Log start  0:00.0", 0.0)
        for ev in log.events():
            self.event_box.addItem(f"{ev.label}  {fmt_time(ev.t, 1)}", ev.t)
        for m in log.messages():
            if m.level in ("WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"):
                self.event_box.addItem(f"{m.level.title()}: {m.text[:40]}  {fmt_time(m.t, 1)}", m.t)

    def _on_event_chosen(self, idx: int):
        t = self.event_box.itemData(idx)
        if t is not None:
            self.bus.seek(float(t))
        self.event_box.setCurrentIndex(0)

    # --- state updates ---
    def _update_enabled(self):
        has = self.pb.has_video
        for w in (self.slider, self.back_btn, self.play_btn, self.fwd_btn, self.link_btn,
                  self.offset_spin, self.minus_f, self.plus_f, self.mute_btn, self.fs_btn):
            w.setEnabled(has)
        self.sync_btn.setEnabled(has and not self.pb.linked)
        self.point2_btn.setEnabled(has and not self.pb.linked and bool(self.pb.sync.points))
        self.drift_reset_btn.setEnabled(has and self.pb.sync.scale != 1.0)
        self._update_sync_label()

    def _update_sync_label(self):
        sync = self.pb.sync
        n = len(sync.points)
        if n == 0:
            text = "Not synced yet"
        elif n == 1 or sync.scale == 1.0:
            text = "Synced at 1 point (no drift correction)"
        else:
            text = (f"Synced at 2 points · drift {sync.drift_percent:+.3f} % "
                    f"({1000 * (sync.scale - 1):+.1f} ms per s)")
        self.sync_label.setText(text)
        self.drift_reset_btn.setEnabled(self.pb.has_video and sync.scale != 1.0)

    def _second_point(self):
        try:
            self.pb.sync_second_point()
        except ValueError as e:
            QMessageBox.warning(self, "Second sync point", str(e))

    def _on_playing(self, playing: bool):
        self.play_btn.setText("⏸" if playing else "▶")
        self.play_btn.setIcon(icons.icon("pause" if playing else "play", "primary"))

    def _on_linked(self, linked: bool):
        self.link_btn.blockSignals(True)
        self.link_btn.setChecked(linked)
        self.link_btn.blockSignals(False)
        self.link_btn.setText("Linked" if linked else "Unlinked")
        self.link_btn.setIcon(icons.icon("link" if linked else "sync", checkable=True))
        self.banner.setVisible(not linked)
        self._update_enabled()

    def _on_offset(self, off: float):
        self.offset_spin.blockSignals(True)
        self.offset_spin.setValue(off)
        self.offset_spin.blockSignals(False)

    def _on_video_time(self, vt: float):
        if self.video is None:
            return
        if not self._dragging:
            self.slider.blockSignals(True)
            self.slider.setValue(int(vt * SLIDER_RES))
            self.slider.blockSignals(False)
        self.vtime.setText(f"{fmt_time(vt)} / {fmt_time(self.video.info.duration, 1)}")

    def _slider_released(self):
        self._dragging = False
        self.pb.seek_video(self.slider.value() / SLIDER_RES)

    def _on_mute(self, muted: bool):
        self.mute_btn.setText("🔇" if muted else "🔊")
        self.mute_btn.setIcon(icons.icon("mute" if muted else "volume", checkable=True))
        if self.video is not None:
            self.video.set_muted_by_user(muted)

    # --- fullscreen (on the screen this panel is on) ---
    @property
    def fullscreen(self) -> bool:
        return self._fs is not None

    def toggle_fullscreen(self):
        if self._fs is not None:
            self.exit_fullscreen()
            return
        if self.video is None:
            return
        screen = (QGuiApplication.screenAt(self.mapToGlobal(self.rect().center()))
                  or QGuiApplication.primaryScreen())
        fs = _FullscreenWindow()
        fs.closed.connect(self._fs_closed)
        self.surface_lay.removeWidget(self.video)
        fs.lay.addWidget(self.video)
        fs.setGeometry(screen.geometry())
        fs.showFullScreen()
        self._fs = fs

    def exit_fullscreen(self):
        fs, self._fs = self._fs, None
        if fs is None:
            return
        fs.lay.removeWidget(self.video)
        self.surface_lay.addWidget(self.video)
        fs.closed.disconnect(self._fs_closed)
        fs.close()
        fs.deleteLater()

    def _fs_closed(self):
        if self._fs is not None:
            self.exit_fullscreen()

    def shutdown(self):
        self.exit_fullscreen()
        self.pb.detach_video()
        if self.video is not None:
            self.video.shutdown()
