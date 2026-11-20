"""Video: phone-format decoding through libmpv, and playback/sync logic.

Format clips are generated with ffmpeg into a temp folder (nothing stored in
the repo). Decoding tests are skipped when ffmpeg or libmpv is missing.
"""
import time

import pytest
from PySide6.QtCore import QObject, Signal

from flightlogger.playback import Playback
from flightlogger.sync import Sync
from flightlogger.timebus import TimeBus
from flightlogger.video.mpv_widget import VideoInfo, mpv_available

from conftest import CLIPS, FPS

@pytest.fixture(scope="session")
def mpv_mod():
    ok, msg = mpv_available()
    if not ok:
        pytest.skip(msg)
    import mpv
    return mpv


def _open(mpv, path):
    p = mpv.MPV(vo="null", ao="null", pause=True, keep_open="always", idle=True, hr_seek="yes")
    p.loadfile(str(path))
    for _ in range(100):
        if p.duration:
            break
        time.sleep(0.05)
    return p


def _settle(p, timeout=3.0):
    """Wait until mpv finished seeking (a frame step sent mid-seek is dropped)."""
    t0 = time.time()
    time.sleep(0.05)
    while time.time() - t0 < timeout and p._get_property("seeking"):
        time.sleep(0.02)


def _wait_pos(p, cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        pos = p.time_pos
        if pos is not None and cond(pos):
            return pos
        time.sleep(0.02)
    return p.time_pos


@pytest.mark.parametrize("name", list(CLIPS) + ["vfr.mp4", "rotated.mp4"])
def test_phone_formats_decode_seek_and_step(clips, mpv_mod, name):
    if name not in clips:
        pytest.skip(f"could not encode {name} on this machine")
    p = _open(mpv_mod, clips[name])
    try:
        assert p.duration == pytest.approx(4.0, abs=0.2)
        assert p._get_property("video-params/w") > 0
        frame = 1 / FPS
        pos = _wait_pos(p, lambda x: True)
        p.seek(2.0, reference="absolute", precision="exact")
        pos = _wait_pos(p, lambda x: abs(x - 2.0) < 1.5 * frame)
        assert pos == pytest.approx(2.0, abs=1.5 * frame)
        _settle(p)
        p.command("frame-step")
        pos2 = _wait_pos(p, lambda x: x > pos + 1e-4)
        assert pos2 - pos == pytest.approx(frame, abs=0.6 * frame)
        _settle(p)
        p.command("frame-back-step")
        pos3 = _wait_pos(p, lambda x: x < pos2 - 1e-4)
        assert pos3 == pytest.approx(pos, abs=1e-3)
        if name == "rotated.mp4":
            assert p._get_property("video-params/rotate") in (90, 270)
    finally:
        p.terminate()


def test_real_phone_video(test_data_dir, mpv_mod):
    vids = sorted(f for f in test_data_dir.iterdir() if f.suffix.lower() in (".mp4", ".mov"))
    if not vids:
        pytest.skip("no video in test data folder")
    p = _open(mpv_mod, vids[0])
    try:
        assert p.duration > 1
        p.seek(p.duration / 2, reference="absolute", precision="exact")
        pos = _wait_pos(p, lambda x: abs(x - p.duration / 2) < 0.05, timeout=5)
        assert pos == pytest.approx(p.duration / 2, abs=0.05)
    finally:
        p.terminate()


# --- playback / sync logic with a fake video ---------------------------------
class FakeVideo(QObject):
    timePosChanged = Signal(float)

    def __init__(self, duration=100.0, fps=50.0):
        super().__init__()
        self.info = VideoInfo(path="fake.mp4", duration=duration, fps=fps)
        self.loaded = True
        self.time_pos = 0.0
        self.eof = False
        self.playing = False
        self.speed = 1.0
        self.seeks = []

    def play(self): self.playing = True
    def pause(self): self.playing = False
    def set_speed(self, s): self.speed = s

    def seek(self, t):
        t = min(max(t, 0.0), self.info.duration - 1e-3)
        self.seeks.append(t)
        self.time_pos = t
        self.timePosChanged.emit(t)

    def frame_step(self, n):
        self.playing = False
        self.time_pos += n * self.info.frame_time
        self.timePosChanged.emit(self.time_pos)

    def advance(self, dt):  # what mpv does while playing
        self.time_pos += dt * self.speed
        self.timePosChanged.emit(self.time_pos)


@pytest.fixture
def pb(qapp):
    bus = TimeBus()
    bus.reset(200.0)
    p = Playback(bus)
    v = FakeVideo()
    p.attach_video(v)
    return p, bus, v


def test_sync_math():
    s = Sync(offset=43.5)
    assert s.to_video(10) == 53.5 and s.to_log(53.5) == 10
    assert s.offset_for(50.0, 6.5) == 43.5
    assert Sync.from_dict(s.to_dict()) == s


def test_linked_seek_moves_video(pb):
    p, bus, v = pb
    p.set_offset(5.0)
    bus.seek(20.0)
    assert v.time_pos == pytest.approx(25.0)
    assert bus.t == pytest.approx(20.0)


def test_video_drives_cursor_while_playing(pb):
    p, bus, v = pb
    bus.seek(10.0)
    p.play()
    assert p._master == "video" and v.playing
    v.advance(1.0)
    assert bus.t == pytest.approx(11.0)
    p.pause()
    assert not v.playing


def test_speed_reaches_video(pb):
    p, _, v = pb
    p.slower()
    assert v.speed == 0.5


def test_clock_before_video_hands_over(pb):
    p, bus, v = pb
    p.set_offset(-10.0)            # video starts at log 10 s
    bus.seek(5.0)
    p.play()
    assert p._master == "clock" and not v.playing
    bus.set_time(9.995)
    p._elapsed.restart()
    import time as _t; _t.sleep(0.02)
    p._tick()
    assert p._master == "video" and v.playing
    p.pause()


def test_video_end_falls_back_to_clock(pb):
    p, bus, v = pb
    bus.seek(10.0)
    p.play()
    v.time_pos = v.info.duration - 0.001
    v.eof = True
    p._tick()
    assert p._master == "clock" and not v.playing
    p.pause()


def test_frame_step_moves_cursor_when_linked(pb):
    p, bus, v = pb
    bus.seek(10.0)
    assert p.step_frames(1)
    assert bus.t == pytest.approx(10.0 + v.info.frame_time)


def test_unlinked_sync_here(pb):
    p, bus, v = pb
    p.set_linked(False)
    p.seek_video(50.0)               # video slider: video only
    bus.seek(6.4)                    # graph click: log only
    assert v.time_pos == pytest.approx(50.0) and bus.t == pytest.approx(6.4)
    p.step_frames(1)                 # steps the video only
    assert bus.t == pytest.approx(6.4)
    p.sync_here()
    assert p.linked
    assert p.offset == pytest.approx(50.0 + v.info.frame_time - 6.4)
    bus.seek(20.0)
    assert v.time_pos == pytest.approx(20.0 + p.offset)


def test_nudge_keeps_frame_moves_cursor(pb):
    p, bus, v = pb
    bus.seek(30.0)
    frame = v.info.frame_time
    p.nudge_frames(1)
    assert v.time_pos == pytest.approx(30.0)
    assert bus.t == pytest.approx(30.0 - frame)


def test_video_plays_without_log(qapp):
    bus = TimeBus()                   # no log loaded: duration 0
    p = Playback(bus)
    v = FakeVideo()
    p.attach_video(v)
    p.play()
    assert p.playing and v.playing
    p.pause()
    assert not v.playing
    assert p.step_frames(1) and v.time_pos == pytest.approx(v.info.frame_time)
