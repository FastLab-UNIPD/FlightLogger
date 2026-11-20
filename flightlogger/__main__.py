"""Entry point: python -m flightlogger [log.ulg]"""
from __future__ import annotations

import sys


def main() -> int:
    import pyqtgraph as pg
    from PySide6.QtWidgets import QApplication

    from .app import MainWindow

    pg.setConfigOptions(background="w", foreground="k", antialias=False)
    app = QApplication(sys.argv)
    app.setApplicationName("FlightLogger")
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        win.open_log(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
