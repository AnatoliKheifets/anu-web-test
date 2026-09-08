#!/usr/bin/env python3
"""Build and verify a byte-for-byte local archive of the ICPEAC 30 website."""

from __future__ import annotations

import argparse
from collections import deque
from html.parser import HTMLParser
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


ORIGIN = "https://www.icpeac30.edu.au"
EXPECTED_PAGES = 29
EXPECTED_ASSETS = 241
EXPECTED_TOTAL = 270
SEEDS = ("/", "/index.php", "/iswamp/", "/iswamp/index.php")
HTML_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_TYPES = HTML_TYPES | {"text/css", "text/javascript", "application/javascript"}
EXCLUDED_404S = {"/local_research.php", "/satellite_meetings.php"}
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^\s)'\"]+)\1\s*\)", re.IGNORECASE)


class References(HTMLParser):
    """Collect resource and navigation references without changing page bytes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {"href", "src", "action", "poster", "data"} and value:
                self.values.append(value)


def canonical(reference: str, parent: str) -> str | None:
    """Return an in-scope path/query key, forcing all requests to the www origin."""
    if not reference or reference.startswith(("#", "data:", "mailto:", "javascript:")):
        return None
    absolute = urljoin(ORIGIN + parent, reference)
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "icpeac30.edu.au",
        "www.icpeac30.edu.au",
    }:
        return None
    path = unquote(parsed.path or "/")
    if path in EXCLUDED_404S:
        return None
    # A trailing empty query is significant here: the site's font CSS references
    # EOT compatibility copies as "font.eot?#iefix", and the verified archive
    # stores those responses as separate filenames ending in "?".
    had_query_marker = "?" in absolute.split("#", 1)[0]
    query = parsed.query
    return path + (("?" + query) if (query or had_query_marker) else "")


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
    url = request_url(key)
    for attempt in range(1, attempts + 1):
        try:
            request = Request(url, headers={"User-Agent": "ICPEAC30-archive-builder/1.0"})
            with urlopen(request, timeout=60, context=ssl.create_default_context()) as response:
                content_type = response.headers.get_content_type().lower()
                return response.read(), content_type
        except HTTPError:
            raise
        except (URLError, TimeoutError, OSError) as error:
            if attempt == attempts:
                raise RuntimeError(f"failed to download {url}: {error}") from error
            time.sleep(attempt * 2)
    raise AssertionError("unreachable")


def references(data: bytes, content_type: str) -> list[str]:
    text = data.decode("utf-8", errors="replace")
    found: list[str] = []
    if content_type in HTML_TYPES:
        parser = References()
        parser.feed(text)
        found.extend(parser.values)
    if content_type == "text/css":
        found.extend(match.group(2) for match in CSS_URL_RE.finditer(text))
    return found


def crawl(root: Path) -> tuple[int, int]:
    pending = deque(SEEDS)
    visited: set[str] = set()
    page_count = 0
    asset_count = 0

    while pending:
        key = pending.popleft()
        if key in visited:
            continue
        visited.add(key)
        try:
            data, content_type = fetch(key)
        except HTTPError as error:
            if error.code == 404:
                print(f"Skipping genuine 404: {request_url(key)}", file=sys.stderr)
                continue
            raise RuntimeError(f"HTTP {error.code} while downloading {request_url(key)}") from error

        destination = output_path(root, key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if content_type in HTML_TYPES:
            page_count += 1
        else:
            asset_count += 1

        if content_type in TEXT_TYPES:
            for value in references(data, content_type):
                child = canonical(value, key.partition("?")[0])
                if child is not None and child not in visited:
                    pending.append(child)

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
        pages, assets = crawl(temporary)
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
