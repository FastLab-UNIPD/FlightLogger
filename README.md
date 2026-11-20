# FlightLogger

Desktop viewer for PX4 `.ulg` flight logs: Flight Review's graphs in dockable
panels with a shared red time cursor, synced flight video (with slow motion and
frame stepping), a whole-flight timeline, track view, messages and parameters.
Timeline notes and project files are next.

The graph set is ported from [PX4 Flight Review](https://github.com/PX4/flight_review)
(BSD-3-Clause, see `flightlogger/log/FLIGHT_REVIEW_LICENSE.md`): same topics,
derived values, titles, legends, colors and per-log conditions. Not ported yet:
the VTOL state band, tailsitter attitude conversion, the map, and decoded
`event` messages.

## Setup (Ubuntu 22.04 / 24.04)

```bash
./install.sh
```

Installs the system packages it needs (asks for sudo only if some are missing:
`libmpv1` on 22.04 / `libmpv2` on 24.04, `libxcb-cursor0`, `python3-venv`),
creates `.venv`, installs the Python dependencies and adds FlightLogger to the
applications menu. Requires Python 3.10+ (22.04 ships 3.10, 24.04 ships 3.12).

## Run

```bash
./flightlogger.sh [path/to/log.ulg]
```

## Video sync

Open a video with **File → Open video** (Ctrl+Shift+O) or drop it on the window
(MP4, MOV, M4V, 3GP, MKV, WebM, AVI; H.264, HEVC, VP9, AV1, ...). Then:

1. Uncheck **Linked** in the Video panel.
2. Put the video on a recognizable moment (scrub bar, ←/→ frame steps).
3. Put the log cursor on the same moment: click a graph, or choose an event
   (Takeoff, Armed, warnings, ...) in **Jump log to**.
4. Press **Sync here**. Video and log are linked again.

Fine-tune with **−1f / +1f** (the video frame stays, the log cursor moves) or
type the offset. While the video plays it drives the cursor; before it starts
and after it ends, the log keeps playing on its own. ⛶ (or double-click)
shows the video fullscreen on the screen its panel is on; Esc leaves.

The Video panel can be resized, moved to any side of the main window or tabbed,
but not torn off into its own window yet; every other panel (graphs, track,
messages, parameters, lists, timeline) can float in separate windows, alone or
grouped, on any screen.

## Keys

| Key | Action |
| --- | --- |
| Space | Play / pause |
| ← / → | One video frame (0.1 s without video); Shift: 1 s, Ctrl: 10 s |
| `[` / `]` | Slower / faster (0.05× … 4×) |
| Home / End | Start / end of log |
| Ctrl+O | Open log |
| Ctrl+Shift+O | Open video |
| Esc | Leave fullscreen video |
| Ctrl+G | New graph panel (side by side with the active one) |

Click or drag on any graph or the timeline to move the cursor. The **Graphs**
list ticks the Flight Review graphs shown in the active panel; drag graphs onto
any panel to place them. Drag raw signals from **Signals** onto a plot (adds to
it) or onto empty space (new plot). Click a row in **Messages** to jump there.
Panels can be split, tabbed, or torn off into floating windows on other screens.

## Tests

```bash
.venv/bin/python -m pytest
```

Tests use a synthetic log generated on the fly (`tests/synth_ulg.py`). Real
flight data (logs, videos) is kept outside the repo in
`~/Desktop/FlightLogger Test Data` (override with `FLIGHTLOGGER_TEST_DATA`);
tests that need it are skipped when the folder is missing.

When a display is available, one test opens the app on screen for about 20 s
to check video rendering while docking (OpenGL cannot render headless). Set
`FLIGHTLOGGER_NO_DISPLAY_TESTS=1` to skip it.
