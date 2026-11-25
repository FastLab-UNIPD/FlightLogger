<p align="center"><img src="flightlogger/resources/flightlogger.svg" width="128" alt="FlightLogger logo"></p>

# FlightLogger

**Version 0.2.0** (see the [update log](#update-log)).

> **Note:** all the code in this repository is entirely AI generated. FlightLogger is
> developed and tested on **Ubuntu 22.04**. The last state tested on **Ubuntu 24.04** is kept in
> the branch `Last-tested-Ubuntu-24.04`; the dependency versions are pinned (PySide6 6.11.x,
> QtAds 5.1.1) to avoid an exit crash with PySide6 6.12.0. Ongoing development happens in the
> branch `Development-Ubuntu-22.04`; `main` holds the released versions.

Desktop viewer for PX4 `.ulg` flight logs. It reproduces
[Flight Review](https://github.com/PX4/flight_review)'s graphs offline and adds
what a review session with a ground-camera video needs: synced video, notes,
undo, graph editing and a layout you can spread over several screens.

![FlightLogger: flight video synced to Flight Review graphs, track, timeline and notes](docs/screenshot.png)

*A PX4 log with its ground-camera video synced: video with notes and PX4
warnings drawn over it (left), Flight Review graphs with a note span and the
selected plot framed (center), track and notes (right), whole-flight timeline
(bottom). The lavender dark theme follows the look of OmniQuadGUI; a light theme
is one click away.*

## What it adds to Flight Review

**Flight Review parity**
* The same graphs (topics, derived values, titles, legends, colors, per-log
  conditions), generated locally from the `.ulg`, so no upload is needed and it
  works offline. Open a Flight Review link and the original log is fetched for you.

**Synced flight video**
* Plays any phone/camera format (MP4, MOV, HEVC, H.264, VP9, AV1, ...), with
  slow motion down to 0.05x, frame stepping and fullscreen on any screen.
* One red cursor drives graphs, timeline, track and video together. Sync by hand
  with one moment, or add a second point to correct camera/flight-controller clock drift.
* The video progress bar is clickable and shows small ticks for your notes and the
  PX4 warnings; notes and log messages can also be drawn over the picture (one
  semi-transparent box each, with a soft flash at the exact timestamp or for the whole
  length of a span). The **Timing** and **Size** menus set how long notes show before and
  after their time and how large the overlay is, relative to the video size (so it also scales in fullscreen).
* Export the video with that overlay burned in (notes, PX4 messages, a flight clock) as an MP4,
  on the GPU encoder when there is one.

**Notes on the timeline**
* Timed notes and spans (with categories and custom colors) plus general untimed
  notes, shown on every graph and the timeline. Hover a marker for a preview.
* Note sets: group the notes about one problem and show or hide each set.
* Export the notes, optionally with the PX4 messages, as text, Markdown (Notion), LaTeX, CSV or HTML.

**Dock and layout freedom**
* Graph panels, track, messages, parameters and lists can be split, tabbed or torn
  off onto other screens. Save named layouts and whole sessions as project files;
  share a project together with its log and video as one folder or `.zip`.
* One-click graph presets (positions, angles, angular rates, actuators, raw
  sensors, power) plus your own saved panels as presets, and isolate any plot in its own floating window.
* *Link X* off lets you zoom a single plot on its own; switching it back on
  realigns everything.

**Analysis**
* Hide the parts of the flight you don't need (e.g. time on the ground): plots and their
  autoscale only use the rest.
* Parameter help as in QGroundControl: select a parameter and press **H** for its PX4
  definition; parameters are grouped by category.

**Editing and export**
* Edit a plot's title and each series' color, thickness, line style and visibility,
  and keep the changes in the project.
* Export a plot as a light-mode PNG for papers; the export dialog has a one-click
  white background.
* Light and dark themes, switched with one button.

**Comfort**
* Undo / redo (Ctrl+Z / Ctrl+Shift+Z) for notes and plot changes, Canc deletes the
  selected note or plot, a toolbar with the common commands, and the plot you last
  clicked is framed. **Help → About** shows the version.

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
`libmpv1` on 22.04 / `libmpv2` on 24.04, `libxcb-cursor0`, `python3-venv`, and `ffmpeg`
for the video export),
creates `.venv`, installs the Python dependencies and adds FlightLogger to the
applications menu. Requires Python 3.10+ (22.04 ships 3.10, 24.04 ships 3.12).

## Graphics (NVIDIA laptops)

On a hybrid laptop (Intel/AMD + NVIDIA, "on-demand" mode, X11) FlightLogger renders on the NVIDIA GPU
automatically, and mpv then decodes video directly on it. It only does so when the proprietary NVIDIA
driver is loaded and a quick one-time test (repeated after a driver update) shows OpenGL really runs
there; on every other machine (Intel/AMD only, NVIDIA only, Wayland, open `nouveau` driver) nothing
changes. Override with the environment variable `FLIGHTLOGGER_GPU`: `default` (never touch the GPU
choice), `nvidia` (force, skip the test) or `auto` (the default). If you already set the PRIME
variables (`__NV_PRIME_RENDER_OFFLOAD`, `__GLX_VENDOR_LIBRARY_NAME`) they are respected.

## Run

```bash
./flightlogger.sh [path/to/log.ulg]
```

## Opening logs

* **File → Open log** (Ctrl+O), or drop a `.ulg` on the window.
* **File → Open Flight Review link** (Ctrl+L): paste a link such as
  `https://review.px4.io/plot_app?log=<id>` (logs.px4.io and self-hosted Flight
  Review servers work too), or drag the link from the browser onto the window.
  The original `.ulg` is downloaded once to `~/.cache/flightlogger/logs` and
  opened; projects remember the link and download it again if the file is gone.

Warnings and errors that PX4 logged appear as dotted markers on the graphs and
the timeline (hover for the text; the timeline right-click menu toggles warnings,
notes, flight events and flight modes). **View → Log messages on graphs** switches
between off, errors, warnings and errors (default), and all messages.

## Video sync

Open a video with **File → Open video** (Ctrl+Shift+O) or drop it on the window
(MP4, MOV, M4V, 3GP, MKV, WebM, AVI; H.264, HEVC, VP9, AV1, ...). Then:

1. Uncheck **Linked** in the Video panel.
2. Put the video on a recognizable moment (scrub bar, ←/→ frame steps).
3. Put the log cursor on the same moment: click a graph, or choose an event
   (Takeoff, Armed, warnings, ...) in **Jump log to**.
4. Press **Sync here**. Video and log are linked again.

Fine-tune with **−1f / +1f** (the video frame stays, the log cursor moves) or
type the offset.

Camera and flight controller clocks drift slightly apart. For long flights,
add a **second sync point** late in the flight: unlink, line up another
recognizable moment, and press **2nd point**. FlightLogger then corrects
the drift between the two points (shown next to the offset; **Reset**
goes back to a single point). While the video plays it drives the cursor; before it starts
and after it ends, the log keeps playing on its own. ⛶ (or double-click)
shows the video fullscreen on the screen its panel is on; Esc leaves. In fullscreen, move the mouse to
show a bar with the progress bar (click or drag to seek, with the note and warning ticks), play/pause,
the time and a close button; it hides again after a moment.

The Video panel can be resized, moved to any side of the main window or tabbed,
but not torn off into its own window yet; every other panel (graphs, track,
messages, parameters, lists, timeline) can float in separate windows, alone or
grouped, on any screen. Floating windows are ordinary desktop windows: they have
minimize, maximize and close buttons, appear in the taskbar and can be placed
anywhere, also outside the program. To dock one along a whole side of the main
window, drag it onto the edge of the window (the edge arrows appear) or use
**Window → Dock floating window to window edge**.

Closing a graph panel only hides it: the **Window** menu lists every graph panel
(closed or floating ones are marked) to bring it back, and has **Delete graph
panel**, **Bring floating windows to front** and **Dock all floating windows**.
Closing an isolated plot window removes that copy.

## Notes

Press **N** (or **Note** in the Notes panel) to add a note at the red cursor.
Give it a title, a category (Info, Maneuver, Anomaly, Crash, Question) or a custom
color, and a comment. **Set end** (or **Ctrl+N** with the note selected) turns it
into a span. **General** adds an untimed note about the whole flight. Notes show
as colored markers on every graph and the timeline (hover for a preview; switch
it off in **View → Note previews on hover**); click one in the list to jump there.
Give a note a **Set** to group it; **Sets ▾** shows or hides each set on the graphs, the
timeline, the video and the list. **Export** (or File → Export notes…) writes the notes,
optionally with the PX4 messages, as text, Markdown, LaTeX, CSV or HTML. Select rows in the
notes list or **Messages** and press Ctrl+C to copy them. On the timeline, note titles are staggered into rows so close notes stay readable.
In the **Video** panel, the *Notes* / *Messages* buttons under the
progress bar draw them over the picture: an instant note appears shortly before its time
and stays a few seconds after; a span stays for its whole length. Each box keeps its place while it is shown, and a new one takes the first
free space. **Timing ▾** sets how
long notes appear before and stay after (default 1 s and 1 s) and **Size ▾** sets the
overlay size (relative to the video, not fixed pixels); both are remembered.
**File → Export video with overlay…** writes an MP4 of the whole video, the part covered by
the log, the visible time range or the current zoom, with the notes, messages and a flight
clock burned in; presets go from fast (GPU) to small files.

## Projects and layouts

**File → Save project** (Ctrl+S) writes a `.flproj` file with the log, the
video and its sync offset, the notes, the cursor and zoom, the graph panels
and the window layout. Paths are stored relative to the project (move or share
the whole folder) and as absolute paths (move just the project file).
**File → Open project** (Ctrl+Shift+P) restores everything; if a file moved,
FlightLogger first looks for it next to the project, then asks you to locate it.
**File → Export shareable project…** copies the log and video together with the project,
into a folder or a single `.zip`, ready to send. Closing with unsaved notes or sync asks to save.

**View → Layouts** saves the current arrangement (graph panels and their graphs,
dock positions, floating windows on each screen, window size) under a name and
re-applies it with any log. One layout can be used at startup; **Reset to
default layout** brings back the standard arrangement. Windows saved on a
monitor that is no longer connected come back to the main screen. Layouts are
stored in `~/.config/flightlogger/layouts`.

## Keys

The shortcuts work from every FlightLogger window, also from floating panels and while typing a note.

| Key | Action |
| --- | --- |
| Space | Play / pause |
| ← / → | One video frame (0.1 s without video); Shift: 1 s, Ctrl: 10 s |
| `[` / `]` | Slower / faster (0.05× … 4×) |
| Home / End | Start / end of log |
| N / Ctrl+N | Add a note at the cursor / set the selected note's end at the cursor |
| Canc | Delete the selected note, or the plot you last clicked |
| Ctrl+Z / Ctrl+Shift+Z | Undo / redo (notes and plot changes) |
| Ctrl+O | Open log |
| Ctrl+L | Open Flight Review link |
| Ctrl+Shift+O | Open video |
| Esc | Leave fullscreen video |
| Ctrl+S / Ctrl+Shift+S | Save project / save as |
| Ctrl+Shift+P | Open project |
| Ctrl+G | New empty graph panel, asks for its name (presets are in the **New panel** menu) |
| H | Help for the selected parameter (Parameters panel) |
| Ctrl+C | Copy the selected rows (notes list, Messages) |

Click or drag on any graph or the timeline to move the cursor. The **Graphs**
list ticks the Flight Review graphs shown in the active panel; drag graphs onto
any panel to place them. Drag raw signals from **Signals** onto a plot (adds to
it) or onto empty space (new plot). Click a row in **Messages** to jump there. Right-click a plot to edit it
(colors, lines), export it as a light image, isolate it in its own window or remove it.
Panels can be split, tabbed, or torn off into floating windows on other screens.
Double-click a graph panel's tab (or right-click it) to rename it.

## Update log

Newest first. Versions follow `MAJOR.MINOR.PATCH`: patch = bug fixes, minor = new features that
keep old projects working, major = breaking changes (0.x = still in development).

### 0.2.0 (2026-11-25)

**New features**
* **Video export with overlay:** File → Export video with overlay… writes an MP4 with the notes,
  PX4 messages and a flight clock burned in, looking like the live overlay. Range: whole video,
  part covered by the log, visible time range or current plot zoom. Presets Fast (GPU), Fast (CPU),
  Balanced, Small file, with automatic fallback to the CPU; resolution original / 1080p / 720p;
  rotated phone videos supported. `install.sh` now installs `ffmpeg`.
* **Note sets:** each note can belong to a set; **Sets ▾** shows or hides each set on graphs,
  timeline, video overlay and list. Saved in the project.
* **Notes export:** text, Markdown (Notion), LaTeX, CSV or HTML, optionally with the PX4
  messages merged in time order; the file name defaults to "Notes - {project}".
* **Hide a time range:** View → Hide time range (or right-click the timeline): hide before /
  after the cursor, show the whole flight. Plots and autoscale ignore the hidden part, the cursor
  and playback stay inside it; undoable and saved in the project.
* **Parameter help:** select a parameter and press **H** (or right-click) for its PX4 definition
  (description, units, range, values, bitmask bits), as in QGroundControl; definitions are bundled
  and **Help → Update parameter definitions…** fetches newer ones. Parameters are grouped by
  category in a list on the left, with a Group column.
* **Graph panels:** rename a panel (double-click or right-click its tab, or **⋮** → Rename
  panel…; Ctrl+G asks for a name, presets take the preset's name); save a panel as your own preset;
  **Box zoom** button (drag a rectangle; the wheel still zooms time only); **Flip Y axis** per plot.
* **Timeline lock:** the lock button in the timeline's corner unlocks zooming and panning the
  timeline itself.
* **Saved view:** series hidden with a legend click, timeline layers, message markers, note
  markers and the timeline lock are saved in projects and layouts.
* **Projects:** **File → Export shareable project…** copies log and video with the project into a
  folder or a single `.zip`; a moved log or video is looked for next to the project before asking;
  saving shows a progress bar when it takes long.
* Copy rows of the notes list and Messages (Ctrl+C or right-click → Copy).
* **View → Show note markers on plots**; note markers are fainter by default.
* File dialogs reopen in the last folder used for each kind of file.
* Renders on the NVIDIA GPU on hybrid laptops when it is safe (see Graphics).
* Fullscreen video has an auto-hiding bar (progress bar with ticks, play/pause, time, close).
* Video overlay: boxes keep their place while shown, sizes follow the video size, every line of
  a note is shown; default timing 1 s before and 1 s after.
* Timeline note titles are staggered into rows; the video panel controls are more compact.
* Development builds show "- Development" in the window title.

**Bug fixes**
* Ctrl+S and the other shortcuts did not work while typing in a floating panel (e.g. Notes).
* Ctrl+G did nothing.
* Typing a note title lagged by about a second per key on large projects.
* A comment typed just before saving or exporting was left out of the file.
* The video overlay showed only the first line of a note.
* The plot range could be dragged outside the flight on the timeline.
* The Positions preset included the altitude estimate plot.

### 0.1.0 (first release)
First tagged version, after the development phase. Includes everything described above:
* Flight Review graphs generated offline from `.ulg` logs, Flight Review links, PX4 warning markers.
* Synced flight video with one- and two-point sync, slow motion, frame stepping, fullscreen.
* Timed notes, spans and general notes with categories and colors, export as text, notes and
  messages drawn over the video with Timing and Size menus.
* Dockable panels, floating windows, saved layouts and project files.
* Plot editing, isolate plot, light PNG export, graph presets, undo/redo, light and dark theme,
  Help → About.
* Fixes in this release: clicking a legend entry no longer moves the cursor; the Positions preset
  hides the barometer altitude; exit crash on Ubuntu 22.04 fixed by pinning PySide6 and QtAds.
