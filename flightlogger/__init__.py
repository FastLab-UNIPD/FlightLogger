"""FlightLogger: PX4 ULog viewer synced with flight video."""
# Single source of truth for the version (pyproject.toml reads it from here).
# Official releases are plain "X.Y.Z"; every development build carries ".devN"
# (e.g. "0.2.0.dev0"), which the program shows in its window title and About box.
# On a release: drop the ".devN" part; right after it: set the next version with ".dev0".
__version__ = "0.2.0"

IS_DEV = "dev" in __version__
# appended to the window title so a development build can never be mistaken for a release
TITLE_SUFFIX = " - Development" if IS_DEV else ""
