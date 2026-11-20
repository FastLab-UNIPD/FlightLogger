"""Video surface backed by libmpv, drawn through mpv's OpenGL render API.

The video dock stays inside the main window (it is not floatable): docking
moves, resizing and tabbing keep the widget in the same top-level window, so
its OpenGL context, and mpv's video output with it, stays alive.

Moving the widget to another top-level window (the fullscreen window) makes Qt
recreate the GL context. Freeing mpv's render context then makes mpv disable
the video track while the audio keeps playing; the track is re-selected once
the new context exists, and the picture returns after a short decoder restart.

libmpv >= 0.33 is required (python-mpv's minimum): Ubuntu 22.04 ships 0.34
(package libmpv1), Ubuntu 24.04 ships 0.37 (package libmpv2).
"""
from __future__ import annotations

import locale
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QOpenGLContext
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QLabel

VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".mkv", ".webm", ".avi", ".mts", ".m2ts")

INSTALL_HINT = ("Video playback needs the libmpv system library.\n"
                "Ubuntu 24.04:  sudo apt install libmpv2\n"
                "Ubuntu 22.04:  sudo apt install libmpv1")


def load_mpv():
    """Import python-mpv; raises ImportError/OSError when libmpv is missing."""
    import mpv  # noqa: F401  (loads libmpv via ctypes)
    return mpv


def mpv_available() -> tuple[bool, str]:
    try:
        load_mpv()
        return True, ""
    except (ImportError, OSError, RuntimeError) as e:
        return False, f"{INSTALL_HINT}\n\n({e})"


@dataclass
class VideoInfo:
    path: str = ""
    duration: float = 0.0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    rotate: int = 0
    codec: str = ""
    container: str = ""
    hwdec: str = ""

    @property
    def frame_time(self) -> float:
        return 1.0 / self.fps if self.fps > 0 else 1 / 30

    def describe(self) -> str:
        if not self.path:
            return ""
        rot = f", rotated {self.rotate}°" if self.rotate else ""
        hw = f", hw: {self.hwdec}" if self.hwdec and self.hwdec != "no" else ""
        return (f"{self.codec} {self.width}×{self.height} @ {self.fps:.2f} fps{rot}, "
                f"{self.duration:.1f} s ({self.container}{hw})")


class MpvWidget(QOpenGLWidget):
    # mpv emits these from its event thread; Qt queues them to the GUI thread
    timePosChanged = Signal(float)
    pausedChanged = Signal(bool)
    eofChanged = Signal(bool)
    fileLoaded = Signal()
    doubleClicked = Signal()
    _frameReady = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        mpv = load_mpv()
        self._mpv_mod = mpv
        locale.setlocale(locale.LC_NUMERIC, "C")  # libmpv requirement
        self.player = mpv.MPV(
            vo="libmpv", hwdec="auto-safe", keep_open="always", idle=True, pause=True,
            osc=False, input_default_bindings=False, input_vo_keyboard=False,
            hr_seek="yes", hr_seek_framedrop=False, audio_pitch_correction=True,
            video_timing_offset=0, loglevel="error")
        self.info = VideoInfo()
        # notes / log messages drawn over the picture, top right (a child widget, so it
        # follows the video into fullscreen)
        self.overlay = QLabel(self)
        self.overlay.setTextFormat(Qt.RichText)
        self.overlay.setWordWrap(True)
        self.overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.overlay.setStyleSheet("background: rgba(0, 0, 0, 150); color: white; "
                                   "border-radius: 6px; padding: 6px 8px; font-size: 13px;")
        self.overlay.hide()
        self._render = None
        self._get_proc = None
        self._restore_vid = None
        self._pending_load: str | None = None
        self._eof = False
        self.frames_presented = 0  # frames drawn (diagnostics / tests)
        self._frameReady.connect(self.update, Qt.QueuedConnection)
        self.player.observe_property("time-pos", self._obs_time)
        self.player.observe_property("pause", lambda _n, v: self.pausedChanged.emit(bool(v)))
        self.player.observe_property("eof-reached", self._obs_eof)
        # 'duration' becoming known = file loaded (portable across mpv 0.34 .. 0.37+)
        self.player.observe_property("duration", self._obs_duration)
        self.setMinimumSize(160, 90)

    # --- mpv callbacks (mpv thread) ---
    def _obs_time(self, _name, value):
        if value is not None:
            self.timePosChanged.emit(float(value))

    def _obs_eof(self, _name, value):
        self._eof = bool(value)
        self.eofChanged.emit(self._eof)

    def _obs_duration(self, _name, value):
        if value:
            self.fileLoaded.emit()

    def set_overlay_html(self, html: str, flash: float = 0.0):
        """flash 0..1 brightens the box (a small pulse around a note's / message's time)."""
        if not html:
            self.overlay.hide()
            return
        level = round(max(0.0, min(1.0, flash)) * 8)   # few steps: no style churn
        if level != getattr(self, "_flash_level", None):
            self._flash_level = level
            k = level / 8
            r, g, b, a = (round(0 + 85 * k), round(0 + 85 * k), round(0 + 100 * k), round(150 + 70 * k))
            self.overlay.setStyleSheet(f"background: rgba({r}, {g}, {b}, {a}); color: white; "
                                       "border-radius: 6px; padding: 6px 8px; font-size: 13px;")
        if html != self.overlay.text():
            self.overlay.setText(html)
        self.overlay.setVisible(True)
        self._place_overlay()

    def _place_overlay(self):
        w = max(120, min(int(self.width() * 0.45), 480))
        self.overlay.setFixedWidth(w)
        self.overlay.adjustSize()
        self.overlay.move(self.width() - self.overlay.width() - 10, 10)
        self.overlay.raise_()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.overlay.isVisible():
            self._place_overlay()

    # --- OpenGL ---
    def initializeGL(self):
        mpv = self._mpv_mod

        def get_proc(_ctx, name):
            ctx = QOpenGLContext.currentContext()
            return ctx.getProcAddress(name) if ctx is not None else 0

        self._get_proc = mpv.MpvGlGetProcAddressFn(get_proc)
        self._render = mpv.MpvRenderContext(self.player, "opengl",
                                            opengl_init_params={"get_proc_address": self._get_proc})
        self._render.update_cb = self._frameReady.emit
        # Qt recreates the GL context when the widget moves to another
        # top-level window (fullscreen and back)
        self.context().aboutToBeDestroyed.connect(self._free_render)
        if self._restore_vid is not None:
            # freeing the old render context made mpv disable the video track
            # (audio kept playing): re-select it so the video output restarts
            vid, self._restore_vid = self._restore_vid, None
            self.player.vid = vid
        if self._pending_load is not None:
            path, self._pending_load = self._pending_load, None
            self.player.loadfile(path)

    def paintGL(self):
        if self._render is None:
            return
        r = self.devicePixelRatioF()
        self._render.render(flip_y=True, opengl_fbo={
            "w": int(self.width() * r), "h": int(self.height() * r),
            "fbo": self.defaultFramebufferObject()})
        self.frames_presented += 1

    def _free_render(self):
        if self._render is None:
            return
        try:
            vid = self.player.vid
        except Exception:
            vid = None
        self._restore_vid = vid if vid not in (None, False, "no") else "auto"
        self.makeCurrent()
        self._render.free()
        self._render = None
        self.doneCurrent()

    def mouseDoubleClickEvent(self, ev):
        self.doubleClicked.emit()

    # --- control API (GUI thread) ---
    def load(self, path: str):
        self._eof = False
        self.info = VideoInfo(path=path)
        self.player.pause = True
        if self._render is None:
            # mpv opens its video output only once a render context exists
            # (first paint); loading earlier can silently fail
            self._pending_load = path
        else:
            self.player.loadfile(path)

    def refresh_info(self) -> VideoInfo:
        p = self.player

        def get(name, default=None):
            try:
                v = p._get_property(name)
                return default if v is None else v
            except Exception:  # property not available (yet)
                return default

        self.info.duration = float(get("duration", 0.0))
        self.info.fps = float(get("container-fps", 0.0) or get("estimated-vf-fps", 0.0))
        self.info.width = int(get("video-params/w", 0) or get("width", 0))
        self.info.height = int(get("video-params/h", 0) or get("height", 0))
        self.info.rotate = int(get("video-params/rotate", 0))
        self.info.codec = str(get("video-format", "") or get("video-codec", ""))
        self.info.container = str(get("file-format", ""))
        self.info.hwdec = str(get("hwdec-current", ""))
        return self.info

    @property
    def loaded(self) -> bool:
        return bool(self.info.path) and self.info.duration > 0

    @property
    def time_pos(self) -> float | None:
        try:
            v = self.player.time_pos
        except Exception:
            return None
        return None if v is None else float(v)

    @property
    def eof(self) -> bool:
        return self._eof

    @property
    def paused(self) -> bool:
        return bool(self.player.pause)

    def play(self):
        self.player.pause = False

    def pause(self):
        self.player.pause = True

    def seek(self, t: float):
        t = min(max(t, 0.0), max(self.info.duration - 1e-3, 0.0))
        self._eof = False
        self.player.seek(t, reference="absolute", precision="exact")

    def frame_step(self, n: int = 1):
        cmd = "frame-step" if n > 0 else "frame-back-step"
        for _ in range(abs(n)):
            self.player.command(cmd)

    def set_speed(self, s: float):
        self.player.speed = s
        self.player.mute = s < 0.25  # pitch-corrected audio gets unusable below that

    def set_muted_by_user(self, muted: bool):
        self.player.volume = 0 if muted else 100

    def shutdown(self):
        self._free_render()
        try:
            self.player.terminate()
        except Exception:
            pass
