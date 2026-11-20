"""The lavender theme, icons and logo (run in a subprocess: the theme is global)."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication
app = QApplication([])
from flightlogger.ui import theme, icons
theme.apply_theme(app)
from flightlogger.app import MainWindow
from flightlogger.log.ulog_loader import FlightLog
w = MainWindow(); w.interactive = False; w.resize(1200, 800); w.show()
w._on_loaded(FlightLog(sys.argv[2]), "")
app.processEvents()
plot = next(p for p in w.active_panel.plots if p.time_based)
out = {
    "dark": theme.is_dark(),
    "black_swapped": theme.plot_color("#000000"),
    "orange_kept": theme.plot_color("#d55e00"),
    "logo": not theme.logo_icon().isNull(),
    "icons": all(not icons.icon(n).isNull() for n in icons.SVG),
    "tab_title": w.video_dw.tabWidget().text(),
    "menu_title": w.video_dw.toggleViewAction().text(),
    "ads_qss": "ads--CDockAreaWidget" in w.docks.mgr.styleSheet(),
    "play_variant": w.toolBar_play_variant if hasattr(w, "toolBar_play_variant") else
                    w.findChild(type(w.video_panel.play_btn), "playButton").property("variant"),
    "plot_bg": w.active_panel.view.backgroundBrush().color().name(),
    "grab": not w.grab().isNull(),
}
print("RESULT " + json.dumps(out))
w.close()
'''


def test_theme_applies(synth_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT), str(synth_path)],
                       capture_output=True, text=True, timeout=120, env=env)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, r.stdout + r.stderr
    res = json.loads(line[7:])
    assert "Could not parse" not in r.stderr, r.stderr     # stylesheet errors
    assert res["dark"] and res["logo"] and res["icons"] and res["ads_qss"] and res["grab"]
    assert res["black_swapped"] != "#000000" and res["orange_kept"] == "#d55e00"
    assert res["tab_title"] == "VIDEO" and res["menu_title"] == "Video"
    assert res["play_variant"] == "primary"
    assert res["plot_bg"] == "#1a1a1a"


def test_logo_svg_is_valid():
    from PySide6.QtSvg import QSvgRenderer
    for name in ("flightlogger.svg", "check.svg", "ads-close.svg", "ads-detach.svg", "ads-menu.svg"):
        assert QSvgRenderer(str(ROOT / "flightlogger" / "resources" / name)).isValid(), name
