#!/usr/bin/env python3
"""Regenerate flightlogger/resources/px4_parameters.json.gz from PX4's parameters.json(.xz).

Usage: python tools/make_param_meta.py [--url URL | --file parameters.json[.xz]]
"""
import argparse
import json
import lzma
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from flightlogger.log import param_meta  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=param_meta.DEFAULT_URL)
    ap.add_argument("--file", help="local parameters.json or .xz instead of downloading")
    ap.add_argument("--out", default=str(param_meta.BUNDLED))
    a = ap.parse_args()
    if a.file:
        blob = Path(a.file).read_bytes()
    else:
        with urllib.request.urlopen(a.url, timeout=60) as r:
            blob = r.read()
    if blob[:6] == b"\xfd7zXZ\x00":
        blob = lzma.decompress(blob)
    data = param_meta.convert(json.loads(blob.decode("utf-8")), source=a.url)
    param_meta.write_gz(data, Path(a.out))
    print(f"{len(data) - 1} parameters -> {a.out}")


if __name__ == "__main__":
    main()
