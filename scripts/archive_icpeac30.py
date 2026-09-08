#!/usr/bin/env python3
"""Build and verify a byte-for-byte local archive of the ICPEAC 30 website."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import shutil
import ssl
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ORIGIN = "https://www.icpeac30.edu.au"
MANIFEST = Path(__file__).with_name("icpeac30_manifest.txt")
EXPECTED_PAGES = 29
EXPECTED_ASSETS = 241
EXPECTED_TOTAL = 270
HTML_TYPES = {"text/html", "application/xhtml+xml"}


def load_manifest() -> list[str]:
    """Load and validate the authoritative path/query list."""
    entries = [line.strip() for line in MANIFEST.read_text(encoding="utf-8").splitlines()]
    if any(not entry or not entry.startswith("/") for entry in entries):
        raise RuntimeError(f"invalid blank or non-absolute entry in {MANIFEST}")
    if len(entries) != EXPECTED_TOTAL:
        raise RuntimeError(
            f"manifest has {len(entries)} entries; expected {EXPECTED_TOTAL}"
        )
    if len(set(entries)) != len(entries):
        raise RuntimeError("manifest contains duplicate source entries")
    destinations = [output_path(Path("."), entry) for entry in entries]
    if len(set(destinations)) != len(destinations):
        raise RuntimeError("manifest source entries map to duplicate archive paths")
    return entries


def request_url(key: str) -> str:
    path, marker, query = key.partition("?")
    encoded = quote(path, safe="/%:@+~!$&'()*,;=-._")
    return ORIGIN + encoded + (marker + query if marker else "")


def output_path(root: Path, key: str) -> Path:
    path, marker, query = key.partition("?")
    if path.endswith("/"):
        path += "index.html"
    relative = PurePosixPath(path.lstrip("/"))
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError(f"unsafe archive path: {key!r}")
    filename = relative.name + (("?" + query) if marker else "")
    return root.joinpath(*relative.parent.parts, filename)


def fetch(key: str, attempts: int = 3) -> tuple[bytes, str]:
    """Fetch one manifest entry, retrying transient errors but never ignoring 404s."""
    url = request_url(key)
    for attempt in range(1, attempts + 1):
        try:
            request = Request(url, headers={"User-Agent": "ICPEAC30-archive-builder/1.0"})
            with urlopen(request, timeout=60, context=ssl.create_default_context()) as response:
                content_type = response.headers.get_content_type().lower()
                return response.read(), content_type
        except HTTPError as error:
            if error.code < 500 or attempt == attempts:
                raise RuntimeError(f"HTTP {error.code} while downloading {url}") from error
        except (URLError, TimeoutError, OSError) as error:
            if attempt == attempts:
                raise RuntimeError(f"failed to download {url}: {error}") from error
        time.sleep(attempt * 2)
    raise AssertionError("unreachable")


def restore(root: Path) -> tuple[int, int]:
    """Download every manifest entry without modifying response bodies."""
    page_count = 0
    asset_count = 0
    for key in load_manifest():
        data, content_type = fetch(key)
        destination = output_path(root, key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if content_type in HTML_TYPES:
            page_count += 1
        else:
            asset_count += 1
    return page_count, asset_count


def verify(root: Path, pages: int, assets: int) -> None:
    files = [path for path in root.rglob("*") if path.is_file()]
    total = len(files)
    required = root / "_files" / "icpeacabstract.tar.gz"
    if (pages, assets, total) != (EXPECTED_PAGES, EXPECTED_ASSETS, EXPECTED_TOTAL):
        raise RuntimeError(
            "archive verification failed: "
            f"observed {pages} rendered pages, {assets} assets, {total} files; "
            f"expected {EXPECTED_PAGES}, {EXPECTED_ASSETS}, {EXPECTED_TOTAL}"
        )
    if not required.is_file():
        raise RuntimeError(f"archive verification failed: missing {required.relative_to(root)}")
    print(f"Verified archive: {pages} rendered pages, {assets} assets, {total} files")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("icpeac30-archive"))
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    temporary = Path(tempfile.mkdtemp(prefix=".icpeac30-", dir=output.parent))
    try:
        pages, assets = restore(temporary)
        verify(temporary, pages, assets)
        if output.exists():
            shutil.rmtree(output)
        os.replace(temporary, output)
    except Exception as error:
        shutil.rmtree(temporary, ignore_errors=True)
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
