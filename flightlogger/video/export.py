"""Qt-free helpers to export a video range with the notes / messages overlay and a flight clock.

Flow: ``export_range`` picks the video interval, ``state_timeline`` lists the distinct overlay
states in it, the caller renders each state to a transparent PNG, ``write_ffconcat`` lists the
PNGs with their durations, ``build_command`` makes the ffmpeg command and ``run_export`` runs it.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable, NamedTuple

from .overlay_model import OverlayOptions, overlay_items


class ExportError(RuntimeError):
    """ffmpeg failed; the message ends with the last lines of its error output."""


class State(NamedTuple):
    start: float      # seconds relative to the start of the exported range
    end: float
    items: list       # (html, flash, key) per box, as from overlay_items


# --------------------------------------------------------------------------- range

def export_range(kind: str, sync, log_duration: float, video_duration: float,
                 trim=None, x_range=None) -> tuple[float, float]:
    """Video-time interval (v0, v1) to export.

    kind: "video" (whole video, or ``trim`` = (v0, v1) in video seconds), "log" (the whole log
    mapped to the video), "visible" / "zoom" (``x_range`` = (t0, t1) in log seconds: the visible
    plot range / the zoomed range). Clipped to [0, video_duration]; ValueError if empty."""
    if not video_duration or video_duration <= 0:
        raise ValueError("No video is loaded")
    if kind == "video":
        a, b = (0.0, video_duration) if trim is None else (float(trim[0]), float(trim[1]))
    elif kind == "log":
        if not log_duration or log_duration <= 0:
            raise ValueError("No log is loaded")
        a, b = sync.to_video(0.0), sync.to_video(float(log_duration))
    elif kind in ("visible", "zoom"):
        if x_range is None:
            raise ValueError("No plot range available")
        a, b = sync.to_video(float(x_range[0])), sync.to_video(float(x_range[1]))
    else:
        raise ValueError(f"Unknown export range: {kind}")
    if a > b:
        a, b = b, a
    a, b = max(0.0, a), min(float(video_duration), b)
    if not b - a > 1e-3:
        raise ValueError("That range does not overlap the video")
    return a, b


# --------------------------------------------------------------------------- state timeline

def _flash_points(e: float, opts: OverlayOptions) -> list[float]:
    """Log times around an event where the quantized flash level (round(flash*8)) changes."""
    pts = [e - opts.flash_in, e, e + opts.flash_out]
    for k in range(8):
        f = (k + 0.5) / 8
        pts.append(e + (f - 1) * opts.flash_in)      # rising
        pts.append(e + (1 - f) * opts.flash_out)     # falling
    return pts


def state_timeline(v0: float, v1: float, notes, messages, opts: OverlayOptions, sync,
                   fps: float) -> list[State]:
    """Distinct overlay states over video range [v0, v1], times relative to v0, covering
    [0, v1 - v0] without gaps. Boundaries sit on the frame grid; states shorter than a frame
    are absorbed. Consecutive equal states are merged."""
    D = v1 - v0
    if D <= 0:
        return []
    fps = fps if fps and fps > 0 else 30.0
    frame = 1.0 / fps
    l0, l1 = sorted((sync.to_log(v0), sync.to_log(v1)))
    pad = max(opts.note_lead, opts.note_hold, opts.msg_lead, opts.msg_hold,
              opts.flash_in, opts.flash_out)

    notes_in, msgs_in, pts = [], [], []
    if opts.notes:
        for n in notes:
            if n.general:
                continue
            end = n.t if n.t_end is None else n.t_end
            if n.t - pad > l1 or end + pad < l0:
                continue
            notes_in.append(n)
            pts += [n.t - opts.note_lead, end + opts.note_hold]
            pts += _flash_points(n.t, opts)
            if n.t_end is not None:
                pts += _flash_points(n.t_end, opts)
    if opts.messages:
        for m in messages:
            if m.t - pad > l1 or m.t + pad < l0:
                continue
            msgs_in.append(m)
            pts += [m.t - opts.msg_lead, m.t + opts.msg_hold]
            pts += _flash_points(m.t, opts)

    bounds = {0.0}
    for lp in pts:
        r = round((sync.to_video(lp) - v0) / frame) * frame
        if 0 < r < D - 0.5 * frame:
            bounds.add(r)
    edges = sorted(bounds) + [D]

    states: list[State] = []
    for a, b in zip(edges, edges[1:]):
        items = overlay_items(sync.to_log(v0 + (a + b) / 2), notes_in, msgs_in, opts)
        if states and states[-1].items == items:
            states[-1] = State(states[-1].start, b, items)
        else:
            states.append(State(a, b, items))
    return states


def write_ffconcat(states, png_paths, path, empty_png=None) -> None:
    """Write an ffconcat list: one PNG per state with its duration.

    ``png_paths``: one path per state, or None / "" for an empty state (then ``empty_png``, a
    fully transparent image shared by all of them, is used). The last file is repeated because
    the concat demuxer ignores the duration of the last entry."""
    if len(states) != len(png_paths):
        raise ValueError("one PNG per state needed")
    lines = ["ffconcat version 1.0"]
    last = None
    for st, png in zip(states, png_paths):
        p = png or empty_png
        if not p:
            raise ValueError("an empty state needs empty_png")
        last = str(Path(p).resolve())
        lines += [f"file '{last.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'",
                  f"duration {max(st.end - st.start, 0.0):.6f}"]
    if last is None:
        raise ValueError("no states")
    lines.append(f"file '{last.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- ffmpeg / encoders

def ffmpeg_available() -> tuple[bool, str]:
    """(ok, message): ffmpeg and ffprobe on PATH; message = version line or what is missing."""
    missing = [n for n in ("ffmpeg", "ffprobe") if not shutil.which(n)]
    if missing:
        return False, ("Missing " + " and ".join(missing) + ". Install ffmpeg "
                       "(e.g. sudo apt install ffmpeg).")
    return True, _ffmpeg_version()


def _ffmpeg_version() -> str:
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10).stdout
        return out.splitlines()[0] if out else ""
    except (OSError, subprocess.SubprocessError):
        return ""


VAAPI_DEVICE = "/dev/dri/renderD128"

_PROBE_CMDS = {
    "h264_nvenc": ["-f", "lavfi", "-i", "color=s=256x256:d=0.1", "-c:v", "h264_nvenc"],
    "h264_qsv": ["-f", "lavfi", "-i", "color=s=256x256:d=0.1", "-pix_fmt", "nv12", "-c:v", "h264_qsv"],
    "h264_vaapi": ["-vaapi_device", VAAPI_DEVICE, "-f", "lavfi", "-i", "color=s=256x256:d=0.1",
                   "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi"],
    "libx264": ["-f", "lavfi", "-i", "color=s=256x256:d=0.1", "-pix_fmt", "yuv420p", "-c:v", "libx264"],
}


def probe_encoders(config_dir=None) -> dict[str, bool]:
    """Which H.264 encoders really work here (a 0.1 s test encode each). Cached as JSON in
    ``config_dir`` (default: the app config folder), keyed by the first line of `ffmpeg -version`."""
    ok, version = ffmpeg_available()
    if not ok:
        return {k: False for k in _PROBE_CMDS}
    if config_dir is None:
        from ..project import config_dir as _cd
        config_dir = _cd()
    cache = Path(config_dir) / "encoders.json"
    try:
        data = json.loads(cache.read_text())
        if data.get("ffmpeg") == version and set(data.get("encoders", {})) == set(_PROBE_CMDS):
            return {k: bool(v) for k, v in data["encoders"].items()}
    except (OSError, ValueError, AttributeError):
        pass
    result = {}
    for name, args in _PROBE_CMDS.items():
        if name == "h264_vaapi" and not os.path.exists(VAAPI_DEVICE):
            result[name] = False
            continue
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
                                "-f", "null", "-"], capture_output=True, timeout=10)
            result[name] = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            result[name] = False
    try:
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"ffmpeg": version, "encoders": result}))
    except OSError:
        pass
    return result


# preset key -> (label, encoders tried in order, quality args per encoder)
PRESETS = {
    "fast_gpu": ("Fast (graphics card)", ("h264_nvenc", "h264_qsv", "h264_vaapi"), {
        "h264_nvenc": ["-preset", "p1", "-cq", "23"],
        "h264_qsv": ["-preset", "veryfast", "-global_quality", "23"],
        "h264_vaapi": ["-qp", "23"]}),
    "fast_cpu": ("Fast (CPU)", ("libx264",), {"libx264": ["-preset", "veryfast", "-crf", "23"]}),
    "balanced": ("Balanced (CPU)", ("libx264",), {"libx264": ["-preset", "medium", "-crf", "20"]}),
    "small": ("Small file (CPU, slow)", ("libx264",), {"libx264": ["-preset", "slow", "-crf", "26"]}),
}
HW_PRESETS = ("fast_gpu",)


def resolve_preset(preset_key: str, encoders: dict) -> tuple[str, list[str]]:
    """(encoder name, quality args) for a preset; ValueError if no usable encoder."""
    if preset_key not in PRESETS:
        raise ValueError(f"Unknown preset: {preset_key}")
    _label, names, args = PRESETS[preset_key]
    for name in names:
        if encoders.get(name):
            return name, list(args[name])
    raise ValueError(f"No working encoder for preset {preset_key}")


def available_presets(encoders: dict) -> list[tuple[str, str, bool, str]]:
    """(key, label, enabled, reason when disabled) for every preset."""
    out = []
    for key, (label, names, _a) in PRESETS.items():
        if any(encoders.get(n) for n in names):
            out.append((key, label, True, ""))
        elif key == "fast_gpu":
            out.append((key, label, False, "No hardware H.264 encoder works on this computer"))
        else:
            out.append((key, label, False, "ffmpeg has no working libx264 encoder"))
    return out


# --------------------------------------------------------------------------- probing

def probe_video(path) -> dict:
    """{"width", "height", "fps", "duration", "audio_codec"} (audio_codec None without audio)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                        "-show_format", str(path)], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ExportError(r.stderr.strip() or "ffprobe failed")
    d = json.loads(r.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        raise ExportError("The file has no video stream")
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)

    def frac(s):
        try:
            n, _, dd = str(s).partition("/")
            return float(n) / float(dd or 1)
        except (ValueError, ZeroDivisionError):
            return 0.0
    fps = frac(v.get("avg_frame_rate")) or frac(v.get("r_frame_rate")) or 30.0
    dur = 0.0
    for src in (v.get("duration"), d.get("format", {}).get("duration")):
        try:
            dur = float(src)
            break
        except (TypeError, ValueError):
            pass
    return {"width": int(v["width"]), "height": int(v["height"]), "fps": fps, "duration": dur,
            "audio_codec": a.get("codec_name") if a else None}


# --------------------------------------------------------------------------- command

RESOLUTIONS = {"original": None, "1080p": 1080, "720p": 720}


def output_size(src_w: int, src_h: int, resolution: str = "original") -> tuple[int, int]:
    """Output (width, height): same aspect, even numbers; never upscaled."""
    target = RESOLUTIONS.get(resolution)
    if target is None or src_h <= target:
        return src_w - src_w % 2, src_h - src_h % 2
    w = round(src_w * target / src_h)
    return w + w % 2, target


def find_font() -> str | None:
    try:
        r = subprocess.run(["fc-match", "-f", "%{file}", "sans"], capture_output=True, text=True,
                           timeout=5)
        f = r.stdout.strip()
        return f if r.returncode == 0 and f and os.path.exists(f) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _esc_opt(s: str) -> str:
    """Escape a value for a filter option inside single quotes."""
    return s.replace("\\", "\\\\").replace("'", "'\\\\\\''").replace(":", "\\:")


def clock_filters(v0: float, clock: dict, out_h: int, font: str | None) -> list[str]:
    """drawtext filters writing the flight time as T+mm:ss.s (T- before the log start).

    clock: offset, scale (video_t = offset + scale * log_t), font_px, position
    ("top-left", "top-right", "bottom-left", "bottom-right")."""
    px = int(clock.get("font_px") or max(12, round(out_h * 0.04)))
    off, scale = float(clock.get("offset", 0.0)), float(clock.get("scale", 1.0)) or 1.0
    pos = clock.get("position", "bottom-left")
    m = max(6, round(px * 0.6))
    x = f"{m}" if pos.endswith("left") else f"w-tw-{m}"
    y = f"{m}" if pos.startswith("top") else f"h-th-{m}"
    # log time L as a function of the output time t; n = tenths of a second shown
    L = f"((t+{v0:.6f}-({off:.6f}))/{scale:.9f})"
    n = f"floor(abs({L})*10)"
    mm = f"%{{eif\\:floor({n}/600)\\:d\\:2}}"
    ss = f"%{{eif\\:mod(floor({n}/10)\\,60)\\:d\\:2}}"
    ds = f"%{{eif\\:mod({n}\\,10)\\:d}}"
    out = []
    for sign, cond in (("+", f"gte({L}\\,0)"), ("-", f"lt({L}\\,0)")):
        opts = [f"text='T{sign}{mm}\\:{ss}.{ds}'", f"fontsize={px}", "fontcolor=white",
                "box=1", "boxcolor=black@0.55", f"boxborderw={max(3, round(px * 0.3))}",
                f"x={x}", f"y={y}", f"enable='{cond}'"]
        if font:
            opts.insert(0, f"fontfile='{_esc_opt(font)}'")
        out.append("drawtext=" + ":".join(opts))
    return out


def build_command(video_path, v0: float, v1: float, concat_path, out_path, preset_key: str,
                  encoders: dict, resolution: str = "original", clock: dict | None = None,
                  audio_codec: str | None = None, src_size: tuple[int, int] | None = None,
                  ffmpeg: str = "ffmpeg") -> list[str]:
    """ffmpeg command for an accurate cut of [v0, v1] re-encoded, with the overlay image stream
    (``concat_path``, rendered at ``output_size``) and flight clock when given.

    ``src_size`` (width, height) is only needed for the clock font size and for the scale
    filter's target; if omitted the video is probed."""
    enc, qargs = resolve_preset(preset_key, encoders)
    if src_size is None:
        info = probe_video(video_path)
        src_size = (info["width"], info["height"])
    ow, oh = output_size(src_size[0], src_size[1], resolution)

    cmd = [ffmpeg, "-hide_banner", "-y", "-progress", "pipe:1", "-nostats"]
    if enc == "h264_vaapi":
        cmd += ["-vaapi_device", VAAPI_DEVICE]
    cmd += ["-ss", f"{v0:.6f}", "-to", f"{v1:.6f}", "-i", str(video_path)]
    if concat_path:
        cmd += ["-f", "concat", "-safe", "0", "-i", str(concat_path)]

    chain = []
    if (ow, oh) != (src_size[0], src_size[1]):
        chain.append(f"scale={ow}:{oh}:flags=bicubic")
    graph = f"[0:v]{','.join(chain) if chain else 'null'}[base]"
    last = "base"
    if concat_path:
        graph += f";[1:v]format=rgba[ov];[{last}][ov]overlay=0:0:eof_action=repeat:format=auto[ovl]"
        last = "ovl"
    tail = []
    if clock:
        tail += clock_filters(v0, clock, oh, find_font())
    if enc == "h264_vaapi":
        tail += ["format=nv12", "hwupload"]
    elif enc != "h264_nvenc" or True:
        tail += ["format=yuv420p"]
    graph += f";[{last}]{','.join(tail)}[out]"
    cmd += ["-filter_complex", graph, "-map", "[out]"]

    if audio_codec:
        cmd += ["-map", "0:a:0"]
        cmd += ["-c:a", "copy"] if audio_codec in ("aac", "mp3") else ["-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", enc, *qargs]
    if enc != "h264_vaapi":
        cmd += ["-pix_fmt", "yuv420p"]
    cmd += ["-movflags", "+faststart", str(out_path)]
    return cmd


# --------------------------------------------------------------------------- running

def run_export(cmd: list[str], total_seconds: float, progress: Callable[[float, float], None] | None = None,
               cancel: Callable[[], bool] = lambda: False) -> None:
    """Run an ffmpeg command built with `-progress pipe:1`. ``progress(done_s, total_s)`` is
    called as it advances. If ``cancel()`` turns true the process is killed and the partial
    output deleted (returns normally; check your own cancel flag). ExportError on failure."""
    out_path = cmd[-1]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             errors="replace")
    except OSError as e:
        raise ExportError(f"Could not start ffmpeg: {e}") from e
    tail: deque[str] = deque(maxlen=20)

    def drain():
        for line in p.stderr:
            tail.append(line.rstrip())
    th = threading.Thread(target=drain, daemon=True)
    th.start()

    cancelled = False

    def watch():          # cancel even while ffmpeg prints nothing
        nonlocal cancelled
        while p.poll() is None:
            if cancel():
                cancelled = True
                p.kill()
                return
            threading.Event().wait(0.1)
    wt = threading.Thread(target=watch, daemon=True)
    wt.start()
    try:
        for line in p.stdout:
            key, _, val = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and progress:
                try:
                    done = int(val) / 1e6
                except ValueError:
                    continue
                progress(max(0.0, min(done, total_seconds)), total_seconds)
        p.wait()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        th.join(timeout=2)
        wt.join(timeout=2)
    if cancelled or cancel():
        try:
            os.remove(out_path)
        except OSError:
            pass
        return
    if p.returncode != 0:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise ExportError("ffmpeg failed:\n" + "\n".join(tail))
    if progress:
        progress(total_seconds, total_seconds)


def export_with_fallback(make_cmd: Callable[[str], list[str]], preset_key: str, total_seconds: float,
                         progress=None, cancel: Callable[[], bool] = lambda: False) -> str:
    """Run ``make_cmd(preset_key)``; if a hardware preset fails, retry once with "fast_cpu".
    Returns the preset actually used (compare with ``preset_key`` to tell the user)."""
    try:
        run_export(make_cmd(preset_key), total_seconds, progress, cancel)
        return preset_key
    except ExportError:
        if preset_key not in HW_PRESETS:
            raise
    run_export(make_cmd("fast_cpu"), total_seconds, progress, cancel)
    return "fast_cpu"
