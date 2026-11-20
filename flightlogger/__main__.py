"""Entry point: python -m flightlogger [log.ulg]"""
from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from .ui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("FlightLogger")
    app.setDesktopFileName("flightlogger")
    apply_theme(app)  # before any window or graph exists

    from .app import MainWindow

    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        win.open_log(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
