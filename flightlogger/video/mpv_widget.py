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
from PySide6.QtWidgets import QLabel, QWidget

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
        # follows the video into fullscreen). One box per item, so each flashes on its own.
        self.overlay = QWidget(self)
        self.overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        # the app stylesheet paints plain widgets opaque: keep only the boxes visible
        self.overlay.setAttribute(Qt.WA_TranslucentBackground)
        self.overlay.setObjectName("videoOverlay")
        self.overlay.setStyleSheet("#videoOverlay { background: transparent; }")
        self._ov_boxes: dict = {}     # item key -> box on screen (each keeps its place)
        self._ov_spare: list[QLabel] = []
        self._ov_geometry = None      # (width, font px) the boxes are placed for
        self._ov_scale = 1.0        # relative size, set by the overlay's Size menu
        self._ov_px = 0             # font size currently applied to the boxes
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

    # Overlay sizes are proportional to the video picture, so a small panel and
    # fullscreen look the same. Reference: 13 px text on a 460 px high video at scale 1.
    OV_FONT_FRAC = 0.028      # text height as a fraction of the widget height
    OV_WIDTH_FRAC = 0.30      # box width as a fraction of the widget width (times the scale)

    def set_overlay_scale(self, scale: float):
        """Relative size of the overlay (1.0 = default); the Size menu changes it."""
        self._ov_scale = float(scale)
        if self.overlay.isVisible():
            self._place_overlay()

    def _ov_font_px(self) -> int:
        return max(8, round(self.height() * self.OV_FONT_FRAC * self._ov_scale))

    OV_MAX_BOXES = 6

    def _new_box(self) -> QLabel:
        box = QLabel(self.overlay)
        box.setTextFormat(Qt.RichText)
        box.setWordWrap(True)
        box.setAttribute(Qt.WA_TransparentForMouseEvents)
        box._flash_level = None
        box._px = 0
        box._y = None          # top of the box inside the overlay; None = not placed yet
        return box

    def _style_box(self, box: QLabel, level: int):
        """Background brightness (flash level 0..8) and proportional text/padding of one box."""
        box._flash_level, box._px = level, self._ov_px
        k = level / 8
        r, g, b, a = (round(40 * k), round(40 * k), round(55 * k), round(150 + 35 * k))
        fp = self._ov_px
        box.setStyleSheet(f"background: rgba({r}, {g}, {b}, {a}); color: white; "
                          f"border-radius: {round(fp * 0.46)}px; "
                          f"padding: {round(fp * 0.46)}px {round(fp * 0.6)}px; font-size: {fp}px;")

    def _drop_box(self, key):
        box = self._ov_boxes.pop(key)
        box.hide()
        box._y = None
        self._ov_spare.append(box)

    def set_overlay_items(self, items: list):
        """items: (html, flash 0..1[, key]) per box; flash gently brightens that box only.

        A box keeps its place on screen for as long as its item (same key) is shown, so it can be
        read in peace; a new item goes into the first free space from the top."""
        keyed = {}
        for item in items:
            keyed[item[2] if len(item) > 2 else item[0]] = (item[0], item[1])
        for key in [k for k in self._ov_boxes if k not in keyed]:
            self._drop_box(key)
        if not keyed:
            self.overlay.hide()
            return
        self._ov_px = self._ov_font_px()
        for key, (html, flash) in keyed.items():
            box = self._ov_boxes.get(key)
            if box is None:
                if len(self._ov_boxes) >= self.OV_MAX_BOXES:
                    continue     # no free slot: shown once one of the others has gone
                box = self._ov_boxes[key] = self._ov_spare.pop() if self._ov_spare else self._new_box()
                box._flash_level = None
            level = round(max(0.0, min(1.0, flash)) * 8)   # few steps: no style churn
            if level != box._flash_level or self._ov_px != box._px:
                self._style_box(box, level)
            if html != box.text():
                box.setText(html)
            box.show()
        self.overlay.setVisible(True)
        self._place_overlay()

    def _place_overlay(self):
        self._ov_px = self._ov_font_px()
        boxes = list(self._ov_boxes.values())
        for box in boxes:
            if box._px != self._ov_px:          # the video was resized: restyle
                self._style_box(box, box._flash_level or 0)
        w = max(120, min(int(self.width() * 0.5),
                         int(self.width() * self.OV_WIDTH_FRAC * max(self._ov_scale, 1.0))))
        gap = max(2, round(self._ov_px * 0.3))
        order = sorted((b for b in boxes if b._y is not None), key=lambda b: b._y)
        order += [b for b in boxes if b._y is None]
        if self._ov_geometry != (w, self._ov_px):   # new size: place again, keeping the order
            self._ov_geometry = (w, self._ov_px)
            for box in boxes:
                box._y = None
        for box in order:
            box.ensurePolished()
            h = box.heightForWidth(w)
            box.resize(w, h if h > 0 else box.sizeHint().height())
        placed: list = []
        # boxes already on screen first (they stay put), then the new ones into free space
        for box in sorted(order, key=lambda b: b._y is None):
            y = box._y if box._y is not None else 0
            for other in sorted(placed, key=lambda b: b._y):
                if y < other._y + other.height() + gap and other._y < y + box.height() + gap:
                    y = other._y + other.height() + gap   # taken (or grown into): next free spot
            box._y = y
            box.move(0, y)
            placed.append(box)
        height = max((b._y + b.height() for b in placed), default=0)
        self.overlay.setFixedSize(w, height)
        margin = max(6, round(self._ov_px * 0.77))
        self.overlay.move(self.width() - w - margin, margin)
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
