"""Content-addressed fetching and caching of filing PDFs.

Parser iterations are frequent, so raw bytes are cached on disk and a reparse
never refetches. These are small managers' web servers hosting mandatory
regulatory disclosures: one request per second per host, identifying user agent,
no retries on 4xx.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

__all__ = ["USER_AGENT", "FetchError", "fetch", "cached_path_for", "read_manifest"]

log = logging.getLogger(__name__)

USER_AGENT = (
    "vngross/0.1 (academic research on Vietnamese fund flows; "
    "10thirtyLabs; mai@10thirtylabs.com)"
)

MIN_INTERVAL_S = 1.0
MAX_RETRIES = 4
TIMEOUT_S = 60.0
MANIFEST_NAME = "manifest.jsonl"

_host_lock = threading.Lock()
_last_request_at: dict[str, float] = {}


class FetchError(RuntimeError):
    """The resource could not be retrieved."""


def _throttle(host: str) -> None:
    """Block until at least MIN_INTERVAL_S has passed since this host's last hit."""
    with _host_lock:
        now = time.monotonic()
        previous = _last_request_at.get(host)
        if previous is not None:
            wait = MIN_INTERVAL_S - (now - previous)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        _last_request_at[host] = now


KNOWN_SUFFIXES = (".pdf", ".xlsx", ".xlsm", ".xls", ".zip")


def _split_name(url: str) -> tuple[str, str]:
    """Split a URL's file name into a sanitised stem and its suffix.

    VCBF filed PDFs until November 2022 and XLSX afterwards, so the suffix has
    to survive into the cache: openpyxl rejects a workbook served under a .pdf
    name regardless of its bytes.
    """
    name = urlsplit(url).path.rsplit("/", 1)[-1] or "download"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    suffix = ""
    for candidate in KNOWN_SUFFIXES:
        if name.lower().endswith(candidate):
            suffix = candidate
            name = name[: -len(candidate)]
            break
    return (name[:80] or "download"), suffix


def cached_path_for(url: str, cache_dir: Path, content: bytes | None = None) -> Path:
    """Where a URL's bytes live.

    Named `<stem>-<hash><suffix>`. The hash is of the content when known,
    otherwise of the URL, so the same document fetched from two mirrors dedupes
    by content once downloaded.
    """
    digest = hashlib.sha256(content if content is not None else url.encode()).hexdigest()
    stem, suffix = _split_name(url)
    return Path(cache_dir) / f"{stem}-{digest[:16]}{suffix}"


def _url_marker(url: str, cache_dir: Path) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    return Path(cache_dir) / ".urls" / f"{digest}.txt"


def _append_manifest(cache_dir: Path, record: dict) -> None:
    path = Path(cache_dir) / MANIFEST_NAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_manifest(cache_dir: Path) -> list[dict]:
    """Every fetch recorded in this cache, oldest first."""
    path = Path(cache_dir) / MANIFEST_NAME
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def fetch(url: str, cache_dir: Path, *, client: httpx.Client | None = None) -> Path:
    """Download `url` into `cache_dir` and return the local path.

    Skips the network entirely when the URL has already been fetched. Retries
    5xx and transport errors with exponential backoff; never retries a 4xx,
    which is a statement about the request, not the server.
    """
    cache_dir = Path(cache_dir)
    (cache_dir / ".urls").mkdir(parents=True, exist_ok=True)

    marker = _url_marker(url, cache_dir)
    if marker.exists():
        cached = cache_dir / marker.read_text(encoding="utf-8").strip()
        if cached.exists():
            log.debug("cache hit %s -> %s", url, cached.name)
            return cached

    host = urlsplit(url).netloc
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=TIMEOUT_S,
        )

    try:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            _throttle(host)
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("transport error on %s: %s", url, exc)
            else:
                status = response.status_code
                if status < 400:
                    content = response.content
                    if not content:
                        raise FetchError(f"empty body from {url}")
                    path = cached_path_for(url, cache_dir, content)
                    if not path.exists():
                        path.write_bytes(content)
                    marker.write_text(path.name, encoding="utf-8")
                    _append_manifest(
                        cache_dir,
                        {
                            "url": url,
                            "path": path.name,
                            "bytes": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "status": status,
                            "content_type": response.headers.get("content-type", ""),
                            "fetched_at": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            ),
                        },
                    )
                    return path
                if 400 <= status < 500:
                    raise FetchError(f"HTTP {status} for {url}; not retrying")
                last_error = FetchError(f"HTTP {status} for {url}")
                log.warning("HTTP %s on %s", status, url)

            if attempt < MAX_RETRIES - 1:
                time.sleep(2.0**attempt)

        raise FetchError(f"gave up on {url} after {MAX_RETRIES} attempts: {last_error}")
    finally:
        if owns_client:
            client.close()
