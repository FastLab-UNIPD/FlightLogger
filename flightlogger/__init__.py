"""FlightLogger: PX4 ULog viewer synced with flight video."""
try:  # single source of truth: `version` in pyproject.toml
    from importlib.metadata import PackageNotFoundError, version as _version
    __version__ = _version("flightlogger")
except Exception:  # not installed as a package (running from a bare checkout)
    __version__ = "0.1.0"
