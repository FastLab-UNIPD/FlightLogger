import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
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
