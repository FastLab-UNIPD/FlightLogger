"""PX4 parameter metadata (Qt-free): bundled snapshot of PX4's parameters.json, an optional newer
copy in the config dir, lookup and HTML formatting for the parameter help popup."""
from __future__ import annotations

import gzip
import html
import json
import lzma
import os
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

DEFAULT_URL = "https://px4-travis.s3.amazonaws.com/Firmware/master/parameters.json.xz"
FILE_NAME = "px4_parameters.json.gz"
BUNDLED = Path(__file__).resolve().parent.parent / "resources" / FILE_NAME

_cache: dict | None = None


# --- conversion (shared with tools/make_param_meta.py) ------------------------------
def convert(raw: dict, source: str = DEFAULT_URL, fetched: str | None = None) -> dict:
    """PX4 parameters.json (already parsed) -> {name: entry, "_meta": {...}}."""
    out: dict = {}
    for p in raw.get("parameters", []):
        name = p.get("name")
        if not name:
            continue
        e = {
            "short": p.get("shortDesc"), "long": (p.get("longDesc") or "").strip(),
            "units": p.get("units"), "min": p.get("min"), "max": p.get("max"),
            "default": p.get("default"), "type": p.get("type"), "group": p.get("group"),
            "decimals": p.get("decimalPlaces"), "increment": p.get("increment"),
            "reboot": True if p.get("rebootRequired") else None,
            "values": {str(v["value"]): v.get("description", "") for v in p.get("values", [])},
            "bitmask": {str(b["index"]): b.get("description", "") for b in p.get("bitmask", [])},
        }
        out[name] = {k: v for k, v in e.items() if v not in (None, "", {})}
    if not out:
        raise ValueError("no parameters found in metadata")
    out["_meta"] = {"source": source, "fetched": fetched or date.today().isoformat()}
    return out


def write_gz(data: dict, path: Path) -> None:
    """Atomic write of the compressed metadata."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --- loading ------------------------------------------------------------------------
def config_file() -> Path:
    from ..project import config_dir
    return config_dir() / FILE_NAME


def _read(path: Path) -> dict | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and len(data) > 10 else None
    except (OSError, ValueError, EOFError):
        return None


def load(reload: bool = False) -> dict:
    """Metadata dict (cached): config-dir copy if valid, else bundled, else empty."""
    global _cache
    if _cache is None or reload:
        _cache = _read(config_file()) or _read(BUNDLED) or {"_meta": {"source": "none", "fetched": ""}}
    return _cache


def lookup(name: str) -> dict | None:
    if name == "_meta":
        return None
    return load().get(name)


def source_info() -> dict:
    return dict(load().get("_meta", {}))


def update_from_url(url: str = DEFAULT_URL, progress=None) -> int:
    """Download PX4's parameters.json.xz, convert, store in the config dir. Returns the number
    of parameters. `progress(done_bytes, total_bytes)` is optional. Raises on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "FlightLogger"})
    chunks, done = [], 0
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            b = r.read(64 * 1024)
            if not b:
                break
            chunks.append(b)
            done += len(b)
            if progress:
                progress(done, total)
    raw = json.loads(lzma.decompress(b"".join(chunks)).decode("utf-8"))
    data = convert(raw, source=url)
    write_gz(data, config_file())
    load(reload=True)
    return len(data) - 1


# --- formatting ---------------------------------------------------------------------
def _num(v) -> str:
    return f"{v:.6g}" if isinstance(v, float) else str(v)


def _as_int(v) -> int | None:
    try:
        f = float(v)
        return int(f) if f == int(f) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _sort_key(item):
    i = _as_int(item[0])
    return (0, i, "") if i is not None else (1, 0, item[0])


def _log_values(value, default) -> str:
    bits = []
    if value is not None:
        bits.append(f"Current value: <b>{_num(value)}</b>")
    if default is not None:
        bits.append(f"Default (this log): {_num(default)}")
    return "<p>" + "<br>".join(bits) + "</p>" if bits else ""


def format_html(name: str, entry: dict | None, value=None, default=None) -> str:
    """HTML for the help popup. `value`/`default` are the log's own current and default values."""
    esc = html.escape
    if entry is None:
        src = source_info().get("source", "unknown")
        return (f"<h3>{esc(name)}</h3><p>No description available for {esc(name)} "
                f"(metadata from {esc(str(src))}).</p>" + _log_values(value, default))
    parts = [f"<h3>{esc(name)}</h3>"]
    if entry.get("short"):
        parts.append(f"<p><b>{esc(entry['short'])}</b></p>")
    if entry.get("long"):
        parts.append("<p>" + esc(entry["long"]).replace("\n", "<br>") + "</p>")
    rows = []
    if entry.get("group"):
        rows.append(("Group", esc(entry["group"])))
    if entry.get("type"):
        rows.append(("Type", esc(entry["type"])))
    if entry.get("units"):
        rows.append(("Units", esc(entry["units"])))
    if "min" in entry or "max" in entry:
        lo, hi = entry.get("min"), entry.get("max")
        rows.append(("Range", f"{'' if lo is None else _num(lo)} … {'' if hi is None else _num(hi)}"))
    if "increment" in entry:
        rows.append(("Increment", _num(entry["increment"])))
    if "default" in entry:
        rows.append(("Default (PX4)", _num(entry["default"])))
    if default is not None:
        rows.append(("Default (this log)", _num(default)))
    if value is not None:
        rows.append(("Current value", f"<b>{_num(value)}</b>"))
    parts.append("<table>" + "".join(f"<tr><td><i>{k}</i>&nbsp;&nbsp;</td><td>{v}</td></tr>"
                                     for k, v in rows) + "</table>")
    if entry.get("reboot"):
        parts.append("<p><b>Reboot required</b></p>")
    iv = _as_int(value)
    if entry.get("values"):
        parts.append("<p><i>Values</i></p><ul>")
        for k, d in sorted(entry["values"].items(), key=_sort_key):
            line = f"{esc(k)}: {esc(d)}"
            if iv is not None and _as_int(k) == iv:
                line = f"<b>{line} &nbsp;&larr; current</b>"
            parts.append(f"<li>{line}</li>")
        parts.append("</ul>")
    if entry.get("bitmask"):
        parts.append("<p><i>Bits</i></p><ul>")
        for k, d in sorted(entry["bitmask"].items(), key=_sort_key):
            bit = _as_int(k)
            line = f"bit {esc(k)}: {esc(d)}"
            if iv is not None and bit is not None and (iv >> bit) & 1:
                line = f"<b>{line} &nbsp;&larr; set</b>"
            parts.append(f"<li>{line}</li>")
        parts.append("</ul>")
    return "".join(parts)
