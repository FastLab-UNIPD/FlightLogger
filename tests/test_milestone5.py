"""Flight Review links, PX4 message markers, two-point sync."""
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from flightlogger.log import flight_review as fr
from flightlogger.sync import Sync


# --- link parsing ------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("https://logs.px4.io/plot_app?log=0f3a2c1e-1234-4abc-9def-001122334455",
     ("https://logs.px4.io", "0f3a2c1e-1234-4abc-9def-001122334455")),
    ("review.px4.io/plot_app?log=abcdef12-aaaa&plots=pid_analysis",
     ("https://review.px4.io", "abcdef12-aaaa")),
    ("http://10.0.0.5:5006/download?log=xyz12345", ("http://10.0.0.5:5006", "xyz12345")),
    ("https://example.org/review/plot_app?log=deadbeef99", ("https://example.org/review", "deadbeef99")),
    ("0f3a2c1e-1234-4abc-9def-001122334455", (fr.DEFAULT_HOST, "0f3a2c1e-1234-4abc-9def-001122334455")),
])
def test_parse_link(text, expected):
    assert fr.parse_link(text) == expected


@pytest.mark.parametrize("bad", ["", "https://logs.px4.io/browse", "ftp://x/plot_app?log=abc",
                                 "https://logs.px4.io/plot_app?log=../etc"])
def test_parse_link_rejects(bad):
    with pytest.raises(fr.LinkError):
        fr.parse_link(bad)


# --- a local stand-in for Flight Review --------------------------------------------------
class _FakeFlightReview:
    def __init__(self, ulg: bytes):
        self.ulg = ulg
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                log = (parse_qs(u.query).get("log") or [""])[0]
                outer.requests.append((u.path, log))
                if u.path != "/download" or log == "missing":
                    self.send_response(404)
                    self.end_headers()
                    return
                if log == "html":
                    body = b"<html>not a log</html>"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="field test.ulg"')
                self.send_header("Content-Length", str(len(outer.ulg)))
                self.end_headers()
                step = 64 * 1024
                for i in range(0, len(outer.ulg), step):
                    self.wfile.write(outer.ulg[i:i + step])
                    if log == "slow":
                        time.sleep(0.05)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def link(self, log_id):
        return f"{self.base}/plot_app?log={log_id}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def server(synth_path):
    s = _FakeFlightReview(synth_path.read_bytes() * 1)
    yield s
    s.close()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    monkeypatch.setenv("FLIGHTLOGGER_CACHE_DIR", str(d))
    return d


def test_download_and_cache(server, cache, synth_path):
    seen = []
    p = fr.download_log(server.link("abc123"), progress=lambda d, t: seen.append((d, t)))
    assert p == cache / "abc123" / "field test.ulg"
    assert p.read_bytes() == synth_path.read_bytes()
    assert seen and seen[-1][0] == seen[-1][1] == p.stat().st_size
    n = len(server.requests)
    assert fr.download_log(server.link("abc123")) == p   # cached: no second request
    assert len(server.requests) == n


def test_download_errors(server, cache):
    with pytest.raises(FileNotFoundError):
        fr.download_log(server.link("missing"))
    with pytest.raises(ValueError):
        fr.download_log(server.link("html"))
    assert not list(cache.rglob("*.ulg")) and not list(cache.rglob("*.part"))
    with pytest.raises(ConnectionError):
        fr.download_log("http://127.0.0.1:9/plot_app?log=nobody", timeout=2)


def test_download_cancel(server, cache, synth_path):
    server.ulg = synth_path.read_bytes() * 20     # long enough to cancel midway
    calls = []
    with pytest.raises(fr.DownloadCancelled):
        fr.download_log(server.link("slow"), progress=lambda d, t: calls.append(d),
                        cancelled=lambda: len(calls) >= 2)
    assert not list(cache.rglob("*.ulg")) and not list(cache.rglob("*.part"))


# --- in the app ---------------------------------------------------------------------
def _wait(qapp, cond, timeout=15):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    return cond()


@pytest.fixture
def make_window(qapp, tmp_path):
    from flightlogger.app import MainWindow
    from flightlogger.project import LayoutStore, Settings
    made = []

    def make():
        w = MainWindow()
        w.interactive = False
        w.layouts = LayoutStore(tmp_path / "layouts")
        w.settings = Settings(tmp_path / "cfg")
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()


def test_open_link_in_app_and_project_redownloads(make_window, qapp, server, cache, tmp_path):
    w = make_window()
    link = server.link("flight42")
    w.open_flight_review(link)
    assert _wait(qapp, lambda: w.log is not None)
    assert w.log.path.name == "field test.ulg" and w.log_source_url == link
    proj = tmp_path / "p.flproj"
    assert w.save_project(proj)
    # the cached download disappears (cache cleared, other machine, ...)
    shutil.rmtree(cache)
    w2 = make_window()
    assert w2.open_project(proj)
    assert _wait(qapp, lambda: w2.log is not None)
    assert w2.log.path.exists() and w2.log_source_url == link
    assert ("/download", "flight42") in server.requests


def test_message_markers(make_window, qapp, synth_log):
    w = make_window()
    w._on_loaded(synth_log, "")
    plots = [p for p in w.active_panel.plots if p.time_based]
    # synthetic log: 1 WARNING (45 s) + 4 INFO messages
    assert all(len(p.message_items) == 1 for p in plots)
    assert len(w.timeline._message_items) == 1
    assert "High vibration level" in plots[0].message_items[0].toolTip()
    w.set_message_mode("all")
    assert all(len(p.message_items) == 5 for p in plots)
    w.set_message_mode("off")
    assert all(len(p.message_items) == 0 for p in plots) and not w.timeline._message_items
    assert w.settings.get("message_markers") == "off"
    p2 = w.new_panel()
    p2.add_plot(["battery_status:0/voltage_v"])
    assert p2.plots[0].message_items == []


# --- two-point sync -----------------------------------------------------------------
def test_two_point_sync_math():
    s = Sync()
    s.set_single_point(6.4, 50.0)
    assert (s.offset, s.scale) == pytest.approx((43.6, 1.0))
    s.set_second_point(106.4, 150.15)            # 0.15 s drift over 100 s
    assert s.scale == pytest.approx(1.0015)
    assert s.to_video(6.4) == pytest.approx(50.0) and s.to_video(106.4) == pytest.approx(150.15)
    assert s.to_log(s.to_video(70.0)) == pytest.approx(70.0)
    assert s.drift_percent == pytest.approx(0.15)
    s.shift(0.02)
    assert s.points[1][1] == pytest.approx(150.17) and s.to_video(106.4) == pytest.approx(150.17)
    assert Sync.from_dict(s.to_dict()) == s
    s.reset_drift()
    assert s.scale == 1.0 and s.offset == pytest.approx(43.62) and len(s.points) == 1
    with pytest.raises(ValueError):
        s.set_second_point(8.0, 51.6)            # too close to the first point
    with pytest.raises(ValueError):
        s.set_second_point(106.4, 170.0)         # 20 % "drift": wrong pair
    with pytest.raises(ValueError):
        Sync().set_second_point(100, 100)        # no first point


def test_two_point_sync_in_playback_and_panel(qapp, make_window, synth_log):
    from test_video import FakeVideo
    w = make_window()
    w._on_loaded(synth_log, "")
    v = FakeVideo(duration=200.0, fps=50.0)
    w.clock.attach_video(v)
    pb, vp = w.clock, w.video_panel
    vp._update_enabled()
    pb.set_linked(False)
    pb.seek_video(50.0)
    w.bus.seek(6.4)
    pb.sync_here()
    assert "1 point" in vp.sync_label.text()
    pb.set_linked(False)
    pb.seek_video(150.15)
    w.bus.seek(106.4)
    assert vp.point2_btn.isEnabled()
    pb.sync_second_point()
    assert pb.linked and pb.sync.scale == pytest.approx(1.0015)
    assert "drift +0.150 %" in vp.sync_label.text() and vp.drift_reset_btn.isEnabled()
    w.bus.seek(56.4)                              # linked seek uses the drift-corrected map
    assert v.time_pos == pytest.approx(50.0 + 50.0 * 1.0015)
    # survives a project round trip
    data = w.project_data()
    w2 = make_window()
    w2.clock.set_sync(Sync.from_dict(data["sync"]))
    assert w2.clock.sync.scale == pytest.approx(1.0015) and len(w2.clock.sync.points) == 2
    pb.reset_drift()
    assert pb.sync.scale == 1.0 and "1 point" in vp.sync_label.text()
