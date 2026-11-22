"""Entry point: python -m flightlogger [log.ulg]"""
from __future__ import annotations

import sys


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

    from .app import MainWindow

    win = MainWindow()
    win.showMaximized()
    if len(sys.argv) > 1:
        if sys.argv[1].lower().endswith(".flproj"):
            win.open_project(sys.argv[1])
        else:
            win.open_log(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
