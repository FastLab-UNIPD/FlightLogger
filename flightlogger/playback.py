"""Playback control: one play/pause/speed for log and video.

Linked mode (normal):
  * Inside the video's time range the video is the master clock: every frame
    mpv shows sets the log cursor (log_t = sync.to_log(video_t)).
  * Before the video starts / after it ends, a log-only clock runs, and hands
    over to the video as soon as the cursor enters its range.
  * User seeks on the TimeBus (plot clicks, keys, timeline) move the video too.

Unlinked mode (for syncing): video and log cursor move independently. Play,
pause and frame steps act on the video only; clicking plots moves only the
log cursor. "Sync here" stores the offset between the two and re-links.

The video object only needs: loaded, info (duration, frame_time), time_pos,
eof, play(), pause(), seek(t), frame_step(n), set_speed(s) and the Qt signal
timePosChanged(float). Tests use a fake with the same surface.
"""
from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, Signal

from .sync import Sync
from .timebus import TimeBus

SPEED_PRESETS = [0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0]
CURSOR_HZ = 30


class Playback(QObject):
    playingChanged = Signal(bool)
    speedChanged = Signal(float)
    linkedChanged = Signal(bool)
    offsetChanged = Signal(float)
    syncChanged = Signal()           # points / drift changed (offset may too)
    videoTimeChanged = Signal(float)

    def __init__(self, bus: TimeBus, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.sync = Sync()
        self.video = None
        self._speed = 1.0
        self._playing = False
        self._linked = True
        self._master = "clock"          # "clock" | "video"
        self._stepping = False          # a frame step is in flight
        # While the video plays, the cursor follows at most CURSOR_HZ: redrawing
        # many graphs for every 60 fps frame would starve the video itself.
        self._pending_pos: float | None = None
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setSingleShot(True)
        self._cursor_timer.setInterval(int(1000 / CURSOR_HZ))
        self._cursor_timer.timeout.connect(self._apply_pending_pos)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._elapsed = QElapsedTimer()
        bus.seekRequested.connect(self._on_seek)

    # --- state ---
    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def linked(self) -> bool:
        return self._linked

    @property
    def offset(self) -> float:
        return self.sync.offset

    @property
    def has_video(self) -> bool:
        return self.video is not None and self.video.loaded

    def _video_range_contains(self, vt: float) -> bool:
        v = self.video
        return 0.0 <= vt < v.info.duration - 1.5 * v.info.frame_time

    # --- video ---
    def attach_video(self, video):
        self.detach_video()
        self.video = video
        video.timePosChanged.connect(self._on_video_pos)
        video.set_speed(self._speed)
        if self._linked:
            self._seek_video_to_log(self.bus.t)

    def detach_video(self):
        if self.video is not None:
            self.pause()
            try:
                self.video.timePosChanged.disconnect(self._on_video_pos)
            except (RuntimeError, TypeError):
                pass
            self.video = None
            self._master = "clock"

    def _seek_video_to_log(self, log_t: float):
        if self.has_video:
            self.video.seek(self.sync.to_video(log_t))  # clamped to the clip by the player

    # --- linking / sync ---
    def set_linked(self, on: bool):
        if on == self._linked:
            return
        self.pause()
        self._linked = on
        if on:
            self._seek_video_to_log(self.bus.t)
        self.linkedChanged.emit(on)

    def set_offset(self, offset: float):
        """Change the offset keeping the video frame; the log cursor moves."""
        if offset == self.sync.offset:
            return
        self.sync.shift(offset - self.sync.offset)
        self.offsetChanged.emit(self.sync.offset)
        self.syncChanged.emit()
        self._follow_video_frame()

    def set_sync(self, sync: Sync):
        """Replace the whole sync (e.g. from a project file)."""
        self.sync = sync
        self.offsetChanged.emit(sync.offset)
        self.syncChanged.emit()
        self._follow_video_frame()

    def _follow_video_frame(self):
        if self._linked and self.has_video:
            pos = self.video.time_pos
            if pos is not None:
                self.bus.set_time(self.sync.to_log(pos))

    def nudge_frames(self, n: int):
        if self.has_video:
            self.set_offset(self.sync.offset + n * self.video.info.frame_time)

    def _matched_pair(self):
        if not self.has_video:
            return None
        pos = self.video.time_pos
        return None if pos is None else (self.bus.t, pos)

    def _relink(self):
        self.offsetChanged.emit(self.sync.offset)
        self.syncChanged.emit()
        if not self._linked:
            self._linked = True
            self.linkedChanged.emit(True)

    def sync_here(self):
        """The current video frame and the current log cursor are the same instant."""
        pair = self._matched_pair()
        if pair is None:
            return
        self.pause()
        self.sync.set_single_point(*pair)
        self._relink()

    def sync_second_point(self):
        """A second matched instant (late in the flight) corrects clock drift.
        Raises ValueError when the pair is unusable."""
        pair = self._matched_pair()
        if pair is None:
            return
        self.pause()
        self.sync.set_second_point(*pair)
        self._relink()

    def reset_drift(self):
        self.sync.reset_drift()
        self.offsetChanged.emit(self.sync.offset)
        self.syncChanged.emit()
        self._follow_video_frame()

    # --- speed ---
    def set_speed(self, s: float):
        s = min(max(s, SPEED_PRESETS[0]), SPEED_PRESETS[-1])
        if s != self._speed:
            self._speed = s
            if self.video is not None:
                self.video.set_speed(s)
            self.speedChanged.emit(s)

    def faster(self):
        nxt = [p for p in SPEED_PRESETS if p > self._speed + 1e-9]
        if nxt:
            self.set_speed(nxt[0])

    def slower(self):
        prv = [p for p in SPEED_PRESETS if p < self._speed - 1e-9]
        if prv:
            self.set_speed(prv[-1])

    # --- play / pause ---
    def play(self):
        if self._playing:
            return
        if not self._linked or self.bus.duration <= 0:
            # unlinked (syncing), or a video opened before any log: video only
            if self.has_video:
                self._playing = True
                self.video.play()
                self.playingChanged.emit(True)
            return
        lo, hi = self._play_limits()
        if self.bus.t >= hi:
            self.bus.seek(lo)
        elif self.bus.t < lo:
            self.bus.seek(lo)
        self._playing = True
        self._elapsed.start()
        self._timer.start()
        self._choose_master()
        self.playingChanged.emit(True)

    def _play_limits(self) -> tuple[float, float]:
        tr = self.bus.trim
        return (tr[0], tr[1]) if tr else (0.0, self.bus.duration)

    def pause(self):
        if not self._playing:
            return
        self._playing = False
        self._timer.stop()
        was_video = self._master == "video"
        if self.video is not None:
            self.video.pause()
        self._master = "clock"
        self.playingChanged.emit(False)
        if was_video and self._linked:
            # mpv may show one more frame after pausing: put the cursor on it
            QTimer.singleShot(80, self._snap_to_video)

    def _snap_to_video(self):
        if self._playing or not self._linked or not self.has_video:
            return
        pos = self.video.time_pos
        if pos is not None and self._video_range_contains(pos):
            self.bus.set_time(self.sync.to_log(pos))

    def toggle(self):
        self.pause() if self._playing else self.play()

    def _choose_master(self):
        if self.has_video and self._linked:
            vt = self.sync.to_video(self.bus.t)
            if self._video_range_contains(vt):
                pos = self.video.time_pos
                if pos is None or abs(pos - vt) > self.video.info.frame_time:
                    self.video.seek(vt)
                self._master = "video"
                self.video.play()
                return
            self.video.pause()
        self._master = "clock"

    def _tick(self):
        dt = self._elapsed.restart() / 1000.0
        if self._master == "video":
            v = self.video
            pos = v.time_pos if v is not None else None
            ended = v is None or v.eof or (pos is not None and pos >= v.info.duration - 1.5 * v.info.frame_time)
            if not ended:
                return  # frames drive the cursor (see _on_video_pos)
            self._master = "clock"
            if v is not None:
                v.pause()
                if pos is not None:  # continue from the last frame shown
                    self.bus.set_time(self.sync.to_log(pos))
        t = self.bus.t + dt * self._speed
        end = self._play_limits()[1]
        if t >= end:
            self.bus.set_time(end)
            self.pause()
            return
        self.bus.set_time(t)
        if self.has_video and self._linked and self._video_range_contains(self.sync.to_video(t)):
            self._choose_master()

    # --- stepping ---
    def step_frames(self, n: int):
        """Frame step the video (and the cursor when linked)."""
        if not self.has_video:
            return False
        if self._linked and not self._video_range_contains(self.sync.to_video(self.bus.t)):
            return False  # cursor outside the clip: caller falls back to time steps
        self.pause()
        self._stepping = self._linked
        self.video.frame_step(n)
        return True

    def step_seconds(self, dt: float):
        if self._linked or not self.has_video:
            self.bus.seek(self.bus.t + dt)
        else:
            pos = self.video.time_pos or 0.0
            self.video.seek(pos + dt)

    # --- signals in ---
    def _on_video_pos(self, pos: float):
        self.videoTimeChanged.emit(pos)
        if not self._linked:
            return
        if self._stepping:
            self._stepping = False
            self.bus.set_time(self.sync.to_log(pos))
        elif self._playing and self._master == "video":
            if self.sync.to_log(pos) >= self._play_limits()[1]:
                self.bus.set_time(self._play_limits()[1])
                self.pause()
                return
            self._pending_pos = pos
            if not self._cursor_timer.isActive():
                self._apply_pending_pos()
                self._cursor_timer.start()

    def _apply_pending_pos(self):
        pos, self._pending_pos = self._pending_pos, None
        if pos is not None and self._linked and self._master == "video":
            self.bus.set_time(self.sync.to_log(pos))

    def _on_seek(self, t: float):
        if self.has_video and self._linked:
            self._seek_video_to_log(t)
            if self._playing:
                self._choose_master()

    def seek_video(self, vt: float):
        """Seek from the video's own slider."""
        if not self.has_video:
            return
        if self._linked:
            self.bus.seek(self.sync.to_log(vt))
        else:
            self.video.seek(vt)


PlaybackClock = Playback  # backwards-compatible name
