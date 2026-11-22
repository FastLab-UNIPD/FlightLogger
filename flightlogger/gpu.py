"""Use the NVIDIA GPU on hybrid laptops, without breaking any other hardware.

On a laptop with an Intel/AMD GPU plus an NVIDIA GPU in "on-demand" mode, programs render on the
integrated GPU unless asked otherwise. ``configure()`` asks for the NVIDIA GPU (PRIME render
offload) only when ALL of these hold, and does nothing otherwise:

* Linux with an X11 session (the offload variables below are for GLX);
* the proprietary NVIDIA kernel driver is loaded (/proc/driver/nvidia);
* the machine is hybrid (another GPU driver is present too; an NVIDIA-only machine already
  renders on it);
* the user did not choose otherwise (FLIGHTLOGGER_GPU=default, or the offload variables are
  already set by the user);
* a short probe in a separate process confirms that OpenGL really runs on the NVIDIA GPU. The
  result is remembered per driver version, so the probe runs once (again after a driver update).

Any failure means the program simply starts the way it always did. Must be called before the
first QApplication / OpenGL context is created.

FLIGHTLOGGER_GPU: "auto" (default), "nvidia" (force, skip the probe) or "default" (never touch).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

OFFLOAD_ENV = {"__NV_PRIME_RENDER_OFFLOAD": "1", "__GLX_VENDOR_LIBRARY_NAME": "nvidia"}

# Run in a child process: open an OpenGL context and print the vendor string.
_PROBE = (
    "import ctypes\n"
    "from PySide6.QtWidgets import QApplication\n"
    "from PySide6.QtGui import QOpenGLContext, QOffscreenSurface\n"
    "app = QApplication([])\n"
    "c = QOpenGLContext()\n"
    "assert c.create()\n"
    "s = QOffscreenSurface(); s.setFormat(c.format()); s.create()\n"
    "assert c.makeCurrent(s)\n"
    "gl = ctypes.CDLL('libGL.so.1'); gl.glGetString.restype = ctypes.c_char_p\n"
    "print('RENDERER=' + gl.glGetString(0x1F01).decode())\n"
)


def _nvidia_driver_version() -> str | None:
    """The loaded proprietary NVIDIA driver (first line of /proc/driver/nvidia/version)."""
    try:
        return Path("/proc/driver/nvidia/version").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return None


def _gpu_drivers() -> set[str]:
    """Kernel driver names of the display adapters (nvidia, i915, amdgpu, ...)."""
    names = set()
    for card in Path("/sys/class/drm").glob("card[0-9]"):
        try:
            names.add((card / "device" / "driver").resolve().name)
        except OSError:
            pass
    return names


def _x11_session() -> bool:
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return False
    return bool(os.environ.get("DISPLAY"))


def _probe_nvidia() -> bool:
    """True if OpenGL runs on the NVIDIA GPU with the offload variables set."""
    env = dict(os.environ, **OFFLOAD_ENV)
    env.pop("QT_QPA_PLATFORM", None)
    try:
        out = subprocess.run([sys.executable, "-I", "-c", _PROBE], env=env, capture_output=True,
                             text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "RENDERER=" in out and "nvidia" in out.lower()


def configure(settings=None) -> str:
    """Set the offload variables when it is safe and useful. Returns what was decided
    ("nvidia", "default" or a short reason), mostly for debugging."""
    mode = (os.environ.get("FLIGHTLOGGER_GPU") or "auto").strip().lower()
    if mode == "default":
        return "default: disabled by FLIGHTLOGGER_GPU"
    if not sys.platform.startswith("linux"):
        return "default: not Linux"
    if any(k in os.environ for k in OFFLOAD_ENV):
        return "default: offload variables already set by the user"
    if not _x11_session():
        return "default: not an X11 session"
    version = _nvidia_driver_version()
    if version is None:
        return "default: no proprietary NVIDIA driver loaded"
    if mode == "nvidia":
        os.environ.update(OFFLOAD_ENV)
        return "nvidia: forced"
    if len(_gpu_drivers() - {"nvidia"}) == 0:
        return "default: NVIDIA is the only GPU"

    cached = settings.get("gpu_probe") if settings is not None else None
    if isinstance(cached, dict) and cached.get("driver") == version:
        ok = bool(cached.get("ok"))
    else:
        ok = _probe_nvidia()
        if settings is not None:
            try:
                settings.set("gpu_probe", {"driver": version, "ok": ok})
            except OSError:
                pass
    if ok:
        os.environ.update(OFFLOAD_ENV)
        return "nvidia"
    return "default: NVIDIA OpenGL probe failed"
