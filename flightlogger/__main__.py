"""Entry point: python -m flightlogger [log.ulg]"""
from __future__ import annotations

import sys


def parse_args(argv: list[str]) -> tuple[str | None, str | None]:
    """(mode, path): mode is "compare" (--compare [file.flcmp] or a .flcmp), "project", "log" or None."""
    args = [a for a in argv if a != "--compare"]
    compare = len(args) != len(argv)
    path = args[0] if args else None
    if compare or (path and path.lower().endswith(".flcmp")):
        return "compare", (path if path and path.lower().endswith(".flcmp") else None)
    if path is None:
        return None, None
    return ("project" if path.lower().endswith(".flproj") else "log"), path


def main() -> int:
    # Before any Qt / OpenGL object exists: use the NVIDIA GPU on hybrid laptops when that is
    # safe (see gpu.py; FLIGHTLOGGER_GPU=default turns it off).
    from . import gpu
    from .project import Settings as _Settings
    gpu.configure(_Settings())

    from PySide6.QtWidgets import QApplication

    from .ui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("FlightLogger")
    app.setDesktopFileName("flightlogger")
    from .project import Settings

    apply_theme(app, Settings().get("theme", "dark"))  # before any window or graph exists

    mode, path = parse_args(sys.argv[1:])
    if mode == "compare":
        from .compare.window import CompareWindow

        cwin = CompareWindow()
        cwin.showMaximized()
        if path:
            cwin.open_comparison(path)
        return app.exec()

    from .app import MainWindow

    win = MainWindow()
    win.showMaximized()
    if mode == "project":
        win.open_project(path)
    elif mode == "log":
        win.open_log(path)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
