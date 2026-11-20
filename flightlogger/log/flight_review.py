"""Download the original .ulg behind a Flight Review link.

Accepted input (any Flight Review instance, e.g. logs.px4.io / review.px4.io or
a self-hosted one):
    https://logs.px4.io/plot_app?log=<id>          (also /3d?log=, /download?log=)
    <id>                                           (uses logs.px4.io)
The file comes from Flight Review's download endpoint: <host>/download?log=<id>
(app/tornado_handlers/download.py in PX4/flight_review). Downloads are cached
in ~/.cache/flightlogger/logs/<id>/ so a link is only fetched once.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

DEFAULT_HOST = "https://logs.px4.io"
LOG_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")  # Flight Review's validate_log_id()
ULOG_MAGIC = b"ULog\x01\x12\x35"
USER_AGENT = "FlightLogger (PX4 log viewer)"


class LinkError(ValueError):
    pass


class DownloadCancelled(Exception):
    pass


def parse_link(text: str) -> tuple[str, str]:
    """-> (base URL of the Flight Review instance, log id)."""
    text = (text or "").strip()
    if not text:
        raise LinkError("Empty link")
    if LOG_ID_RE.match(text) and len(text) >= 8:
        return DEFAULT_HOST, text
    u = urlparse(text if "://" in text else "https://" + text)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise LinkError("Not a Flight Review link")
    log_id = (parse_qs(u.query).get("log") or [""])[0]
    if not log_id or not LOG_ID_RE.match(log_id):
        raise LinkError("The link has no log id (expected …?log=<id>)")
    # keep a path prefix if Flight Review is served below one (e.g. /review/plot_app)
    prefix = u.path.rsplit("/", 1)[0] if "/" in u.path.strip("/") else ""
    return f"{u.scheme}://{u.netloc}{prefix}", log_id


def download_url(base: str, log_id: str) -> str:
    return f"{base.rstrip('/')}/download?log={quote(log_id)}"


def cache_dir() -> Path:
    env = os.environ.get("FLIGHTLOGGER_CACHE_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "flightlogger" / "logs"


def cached_log(log_id: str) -> Path | None:
    d = cache_dir() / log_id
    logs = sorted(d.glob("*.ulg")) if d.is_dir() else []
    return logs[0] if logs else None


def _filename_from_headers(headers, log_id: str) -> str:
    cd = headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    name = Path(m.group(1)).name if m else ""
    if not name.lower().endswith(".ulg"):
        name = f"{log_id}.ulg"
    return re.sub(r"[^\w.\- ]", "_", name)


def download_log(link: str, progress=None, cancelled=lambda: False, timeout: float = 30.0) -> Path:
    """Download (or reuse the cached copy of) the log behind a Flight Review link.

    progress(done_bytes, total_bytes_or_0) is called while downloading;
    cancelled() is polled and raises DownloadCancelled when it returns True.
    """
    import requests

    base, log_id = parse_link(link)
    hit = cached_log(log_id)
    if hit is not None:
        return hit
    url = download_url(base, log_id)
    try:
        r = requests.get(url, stream=True, timeout=timeout, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as e:
        raise ConnectionError(f"Could not reach {urlparse(url).netloc}: {e}") from e
    with r:
        if r.status_code == 404:
            raise FileNotFoundError("Flight Review has no log with this id (or it is private)")
        if r.status_code != 200:
            raise ConnectionError(f"Flight Review answered HTTP {r.status_code}")
        total = int(r.headers.get("Content-Length") or 0)
        folder = cache_dir() / log_id
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / _filename_from_headers(r.headers, log_id)
        part = dest.with_name(dest.name + ".part")
        done = 0
        try:
            with open(part, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if cancelled():
                        raise DownloadCancelled()
                    if done == 0 and not chunk.startswith(ULOG_MAGIC[:4]):
                        raise ValueError("The link did not return a ULog file")
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            if done == 0:
                raise ValueError("Empty download")
            part.replace(dest)
        finally:
            if part.exists():
                part.unlink()
    return dest
