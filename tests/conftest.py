import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# never read or write the user's real layouts / recent projects
import tempfile  # noqa: E402
os.environ["FLIGHTLOGGER_CONFIG_DIR"] = tempfile.mkdtemp(prefix="flightlogger-test-config-")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


@pytest.fixture(scope="session")
def synth_path(tmp_path_factory) -> Path:
    import synth_ulg
    p = tmp_path_factory.mktemp("logs") / "synth.ulg"
    synth_ulg.write(str(p))
    return p


@pytest.fixture(scope="session")
def synth_log(synth_path):
    from flightlogger.log.ulog_loader import FlightLog
    return FlightLog(synth_path)


# Real flight data (logs, videos) lives outside the repo. Override with
# FLIGHTLOGGER_TEST_DATA=/path/to/folder.
TEST_DATA = Path(os.environ.get("FLIGHTLOGGER_TEST_DATA",
                                Path.home() / "Desktop" / "FlightLogger Test Data"))


@pytest.fixture(scope="session")
def test_data_dir() -> Path:
    if not TEST_DATA.is_dir():
        pytest.skip(f"test data folder not found: {TEST_DATA}")
    return TEST_DATA


@pytest.fixture(scope="session")
def test_data_dir_optional():
    """The test data folder, or None (for tests with a generated fallback)."""
    return TEST_DATA if TEST_DATA.is_dir() else None


@pytest.fixture(scope="session")
def real_log(test_data_dir):
    """First .ulg in the test data folder; skipped if none."""
    logs = sorted(test_data_dir.glob("*.ulg"))
    if not logs:
        pytest.skip(f"no .ulg in {test_data_dir}")
    from flightlogger.log.ulog_loader import FlightLog
    return FlightLog(logs[0])


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# --- generated phone-style video clips (temp folder, never stored in the repo) ---
FPS = 30
SRC = ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={FPS}:duration=4",
       "-f", "lavfi", "-i", "sine=frequency=440:duration=4"]

# name -> extra ffmpeg args (phone-style containers/codecs)
CLIPS = {
    "h264.mp4": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    "hevc.mp4": ["-c:v", "libx265", "-tag:v", "hvc1", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-x265-params", "log-level=error"],
    "hevc.mov": ["-c:v", "libx265", "-tag:v", "hvc1", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-x265-params", "log-level=error"],
    "h264.3gp": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "16000"],
    "vp9.webm": ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-c:a", "libopus"],
    "av1.mkv": ["-c:v", "libsvtav1", "-preset", "12", "-c:a", "libopus"],
    "mpeg4.avi": ["-c:v", "mpeg4", "-c:a", "aac"],
}


def _ffmpeg(args, out):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args, str(out)], check=True,
                   capture_output=True, timeout=120)


@pytest.fixture(scope="session")
def clips(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("clips")
    made = {}
    for name, extra in CLIPS.items():
        try:
            _ffmpeg([*SRC, *extra, "-shortest"], d / name)
            made[name] = d / name
        except subprocess.CalledProcessError:
            pass  # encoder missing on this machine
    # variable frame rate: irregular timestamps, like phones under load
    try:
        _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=4",
                 "-vf", "setpts='N/30/TB+0.01*sin(N)/TB'", "-fps_mode", "vfr",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p"], d / "vfr.mp4")
        made["vfr.mp4"] = d / "vfr.mp4"
    except subprocess.CalledProcessError:
        pass
    # portrait phone video: landscape pixels + rotation metadata
    try:
        _ffmpeg(["-display_rotation:v:0", "90", "-i", str(made["h264.mp4"]), "-c", "copy"],
                d / "rotated.mp4")
        made["rotated.mp4"] = d / "rotated.mp4"
    except (subprocess.CalledProcessError, KeyError):
        pass
    return made
