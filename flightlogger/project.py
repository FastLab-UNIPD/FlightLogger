"""Project files (.flproj) and named layouts (.fllayout).

A project holds one analysis session: the log, the video and its sync, the
notes, the cursor/zoom, the graph panels and the window layout. Paths are
stored relative to the project file (so a folder with log + video + project
can be moved or shared as a whole) and as absolute paths (so the project file
alone can be moved); opening tries the relative path first.

A layout holds only the arrangement (graph panels and their graphs, dock
positions, floating windows, window geometry), so it can be reused with any
log. Layouts live in the user's config folder.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

PROJECT_EXT = ".flproj"
LAYOUT_EXT = ".fllayout"
FORMAT_VERSION = 1


# --- paths -----------------------------------------------------------------------
def to_rel(path: str | Path | None, base_dir: Path) -> str | None:
    if not path:
        return None
    try:
        return os.path.relpath(Path(path).resolve(), base_dir.resolve())
    except ValueError:  # different drive (Windows)
        return str(Path(path).resolve())


def from_rel(path: str | None, base_dir: Path) -> Path | None:
    if not path:
        return None
    p = Path(path)
    return p if p.is_absolute() else (base_dir / p).resolve()


def b64(data: bytes) -> str:
    return base64.b64encode(bytes(data)).decode("ascii")


def unb64(text: str | None) -> bytes:
    return base64.b64decode(text) if text else b""


# --- project ------------------------------------------------------------------------
def save_project(path: str | Path, data: dict):
    """data uses absolute paths under data['log']['path'] / data['video']['path']."""
    path = Path(path)
    base = path.parent
    out = json.loads(json.dumps(data))  # deep copy
    out["app"] = "FlightLogger"
    out["version"] = FORMAT_VERSION
    for key in ("log", "video"):
        if out.get(key) and out[key].get("path"):
            absolute = str(Path(out[key]["path"]).resolve())
            out[key]["path"] = to_rel(absolute, base)   # survives moving the whole folder
            out[key]["abs_path"] = absolute             # survives moving only the project
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    tmp.replace(path)  # never leave a half-written project


def load_project(path: str | Path) -> dict:
    """Returns the project with absolute log/video paths."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("app") != "FlightLogger":
        raise ValueError(f"{path.name} is not a FlightLogger project")
    if int(data.get("version", 0)) > FORMAT_VERSION:
        raise ValueError(f"{path.name} was saved by a newer FlightLogger")
    for key in ("log", "video"):
        entry = data.get(key)
        if entry and entry.get("path"):
            rel = from_rel(entry["path"], path.parent)
            absolute = Path(entry["abs_path"]) if entry.get("abs_path") else None
            if rel.exists() or absolute is None or not absolute.exists():
                entry["path"] = str(rel)
            else:
                entry["path"] = str(absolute)
    return data


# --- named layouts ------------------------------------------------------------------
def config_dir() -> Path:
    env = os.environ.get("FLIGHTLOGGER_CONFIG_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "flightlogger"


def _safe_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("empty layout name")
    return re.sub(r'[/\\:*?"<>|]', "_", name)


class LayoutStore:
    def __init__(self, directory: str | Path | None = None):
        self.dir = Path(directory) if directory else config_dir() / "layouts"

    def _file(self, name: str) -> Path:
        return self.dir / (_safe_name(name) + LAYOUT_EXT)

    def names(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted((f.stem for f in self.dir.glob("*" + LAYOUT_EXT)), key=str.lower)

    def save(self, name: str, layout: dict):
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {"app": "FlightLogger", "version": FORMAT_VERSION, "name": name, "layout": layout}
        self._file(name).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load(self, name: str) -> dict:
        data = json.loads(self._file(name).read_text(encoding="utf-8"))
        return data["layout"]

    def delete(self, name: str):
        f = self._file(name)
        if f.exists():
            f.unlink()
        if self.startup == name:
            self.startup = None

    # the layout applied when FlightLogger starts
    @property
    def startup(self) -> str | None:
        f = self.dir / "startup.json"
        try:
            name = json.loads(f.read_text(encoding="utf-8")).get("name")
        except (OSError, ValueError):
            return None
        return name if name in self.names() else None

    @startup.setter
    def startup(self, name: str | None):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "startup.json").write_text(json.dumps({"name": name}), encoding="utf-8")


class RecentProjects:
    MAX = 8

    def __init__(self, directory: str | Path | None = None):
        self.file = Path(directory or config_dir()) / "recent_projects.json"

    def list(self) -> list[str]:
        try:
            items = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [p for p in items if Path(p).exists()]

    def add(self, path: str | Path):
        p = str(Path(path).resolve())
        items = [p] + [x for x in self.list() if x != p]
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(items[: self.MAX], indent=1), encoding="utf-8")
