"""
Tiny on-disk cache for third-party API responses.

Bibliographic records (Crossref, DBLP, ...) are effectively immutable, so
responses are kept with no expiry. ``zobs augment --refresh`` bypasses the
cache; deleting ``references/.zobs-cache/`` clears it.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

USER_AGENT = "zobs/0.1 (+https://github.com/vivi365/zobs)"

# minimum spacing between live requests (any host) — keep the public
# APIs happy without a per-host budget
_MIN_INTERVAL = 0.34
_last_request = 0.0

_NET_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    OSError,
    TimeoutError,
    ValueError,
)


def _throttle() -> None:
    global _last_request
    wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


class ResponseCache:
    def __init__(self, root: Path, *, refresh: bool = False) -> None:
        self.root = root
        self.refresh = refresh

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        if self.refresh:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, value: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self._path(key).write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def _fetch(self, url: str, accept: str, timeout: int) -> str | None:
        _throttle()
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": accept}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, "replace")
        except _NET_ERRORS:
            return None

    def fetch_json(
        self, url: str, *, accept: str = "application/json", timeout: int = 15
    ) -> Any | None:
        """GET ``url`` and parse JSON, through the cache. None on any failure."""
        cached = self.get(url)
        if cached is not None:
            return cached
        body = self._fetch(url, accept, timeout)
        if body is None:
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        self.put(url, data)
        return data

    def fetch_text(
        self, url: str, *, accept: str = "*/*", timeout: int = 15
    ) -> str | None:
        """GET ``url`` as text, through the cache. None on any failure."""
        cached = self.get(url)
        if isinstance(cached, dict) and "text" in cached:
            return cached["text"]
        body = self._fetch(url, accept, timeout)
        if body is not None:
            self.put(url, {"text": body})
        return body
