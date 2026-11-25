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
import shutil
import tempfile
import zipfile
from pathlib import Path

PROJECT_EXT = ".flproj"
LAYOUT_EXT = ".fllayout"
PRESET_EXT = ".flpreset"
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
def find_moved(path: str | Path | None, project_dir: str | Path) -> Path | None:
    """A file of the same name in the project's folder or its immediate subfolders."""
    if not path:
        return None
    name = Path(path).name
    base = Path(project_dir)
    try:
        if (base / name).is_file():
            return (base / name).resolve()
        for sub in sorted(p for p in base.iterdir() if p.is_dir()):
            if (sub / name).is_file():
                return (sub / name).resolve()
    except OSError:
        pass
    return None


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


class ExportCancelled(Exception):
    pass


def _copy_file(src: Path, dst: Path, done: list, total: int, progress, cancel, chunk=8 << 20):
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            if cancel():
                raise ExportCancelled()
            buf = fi.read(chunk)
            if not buf:
                break
            fo.write(buf)
            done[0] += len(buf)
            if progress:
                progress(done[0], total)
    shutil.copystat(src, dst, follow_symlinks=True)


def export_shareable(data: dict, dest_dir: str | Path, name: str, include_log: bool = True,
                     include_video: bool = True, make_zip: bool = False, progress=None,
                     cancel=lambda: False) -> Path:
    """Export the project with copies of the log/video and <name>.flproj pointing to them.

    make_zip False: writes the folder <dest_dir>/<name>/ and returns it.
    make_zip True: writes ONLY <dest_dir>/<name>.zip (containing <name>/<name>.flproj, log, video)
    and returns the zip path; no folder is created, so an existing <dest_dir>/<name> is never touched.

    data is project_data() (absolute paths). progress(done_bytes, total_bytes) is called while
    copying/streaming and finally with (total, total). On cancel or error, only what this call
    created is removed. A file that is not included keeps its original absolute path."""
    dest_dir = Path(dest_dir)
    folder = dest_dir / name
    zip_path = dest_dir / (name + ".zip")
    out = json.loads(json.dumps(data))
    todo = []   # (key, src)
    for key, include in (("log", include_log), ("video", include_video)):
        src = (out.get(key) or {}).get("path")
        if include and src and Path(src).is_file():
            todo.append((key, Path(src)))
    total = sum(s.stat().st_size for _, s in todo)
    done = [0]
    if make_zip:
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                for key, src in todo:
                    out[key]["path"] = str(Path(tmp) / src.name)   # becomes relative in the project
                proj_tmp = Path(tmp) / (name + PROJECT_EXT)
                save_project(proj_tmp, out)
                pdata = json.loads(proj_tmp.read_text(encoding="utf-8"))
                for key, _ in todo:
                    pdata[key].pop("abs_path", None)   # temp path: meaningless on another PC
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
                    z.writestr(f"{name}/{name}{PROJECT_EXT}", json.dumps(pdata, indent=2))
                    for key, src in todo:
                        with open(src, "rb") as fi, z.open(f"{name}/{src.name}", "w", force_zip64=True) as zo:
                            while True:
                                if cancel():
                                    raise ExportCancelled()
                                buf = fi.read(8 << 20)
                                if not buf:
                                    break
                                zo.write(buf)
                                done[0] += len(buf)
                                if progress:
                                    progress(done[0], total)
            if progress:
                progress(total, total)
        except BaseException:
            try:
                zip_path.unlink()
            except OSError:
                pass
            raise
        return zip_path
    created_folder = not folder.exists()
    created_files: list[Path] = []
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for key, src in todo:
            dst = folder / src.name
            created_files.append(dst)
            _copy_file(src, dst, done, total, progress, cancel)
            out[key]["path"] = str(dst)
        proj = folder / (name + PROJECT_EXT)
        created_files.append(proj)
        save_project(proj, out)
        if progress:
            progress(total, total)
    except BaseException:
        for f in created_files:
            try:
                f.unlink()
            except OSError:
                pass
        if created_folder:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder


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


class PresetStore:
    """Personal graph-panel presets (one .flpreset per preset in the config folder)."""

    def __init__(self, directory: str | Path | None = None):
        self.dir = Path(directory) if directory else config_dir() / "presets"

    def _file(self, name: str) -> Path:
        return self.dir / (_safe_name(name) + PRESET_EXT)

    def names(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted((f.stem for f in self.dir.glob("*" + PRESET_EXT)), key=str.lower)

    def save(self, name: str, panel: dict):
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {"app": "FlightLogger", "version": FORMAT_VERSION, "name": name, "panel": panel}
        self._file(name).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load(self, name: str) -> dict:
        return json.loads(self._file(name).read_text(encoding="utf-8"))["panel"]

    def delete(self, name: str):
        f = self._file(name)
        if f.exists():
            f.unlink()


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


class Settings:
    """Small persistent preferences (JSON in the config folder)."""

    def __init__(self, directory: str | Path | None = None):
        self.file = Path(directory or config_dir()) / "settings.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self, key: str, default=None):
        return self._read().get(key, default)

    def set(self, key: str, value):
        d = self._read()
        d[key] = value
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(d, indent=1), encoding="utf-8")


class LastDirs:
    """Last folder used per kind of file dialog ("log", "video", "project", ...)."""

    def __init__(self, directory: str | Path | None = None):
        self._settings = Settings(directory)

    def get(self, kind: str, fallback: str | Path) -> str:
        d = (self._settings.get("last_dirs", {}) or {}).get(kind)
        return d if d and Path(d).is_dir() else str(fallback)

    def remember(self, kind: str, path: str | Path):
        if not path:
            return
        p = Path(path)
        d = p if p.is_dir() else p.parent
        dirs = dict(self._settings.get("last_dirs", {}) or {})
        dirs[kind] = str(d)
        self._settings.set("last_dirs", dirs)
