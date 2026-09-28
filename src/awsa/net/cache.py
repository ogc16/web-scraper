"""On-disk HTTP cache with ETag revalidation.

Keeps repeat runs cheap and keeps the agent off the origin's back. Entries are
stored as one JSON header file plus one body file per URL, keyed by a SHA-256
of the method, URL and accept header, so the cache can never serve the wrong
representation.

A 304 from the origin refreshes the stored entry's TTL and clears the
revalidation headers so the next run does a full fetch again.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

__all__ = ["CacheEntry", "HttpCache", "MemoryCache", "ResponseCache"]

_CACHE_VERSION: Final = 2


def _key(url: str, *, accept: str = "text/html", method: str = "GET") -> str:
    raw = f"{method.upper()}\x00{url}\x00{accept}".encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A stored response plus the metadata needed to revalidate it."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    stored_at: float
    etag: str | None = None
    last_modified: str | None = None

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.stored_at

    def is_fresh(self, ttl: float, now: float | None = None) -> bool:
        return self.age(now) < ttl

    def as_meta(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "stored_at": self.stored_at,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "body_bytes": len(self.body),
        }


class ResponseCache:
    """Interface implemented by both cache backends."""

    def get(self, url: str, *, accept: str = "text/html") -> CacheEntry | None:
        raise NotImplementedError

    def put(
        self,
        url: str,
        *,
        status: int,
        headers: dict[str, str],
        body: bytes,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str = "text/html",
    ) -> None:
        raise NotImplementedError

    def revalidate(self, url: str, *, accept: str = "text/html") -> CacheEntry | None:
        """Mark a cached entry as freshly revalidated (used on HTTP 304)."""
        raise NotImplementedError

    def clear(self) -> int:
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        raise NotImplementedError


class MemoryCache(ResponseCache):
    """In-process cache. Default when no cache directory is configured."""

    def __init__(self, max_entries: int = 512) -> None:
        self._data: dict[str, CacheEntry] = {}
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0

    def get(self, url: str, *, accept: str = "text/html") -> CacheEntry | None:
        entry = self._data.get(_key(url, accept=accept))
        if entry is None:
            self.misses += 1
        else:
            self.hits += 1
        return entry

    def put(
        self,
        url: str,
        *,
        status: int,
        headers: dict[str, str],
        body: bytes,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str = "text/html",
    ) -> None:
        if len(self._data) >= self.max_entries:
            oldest = min(self._data, key=lambda k: self._data[k].stored_at)
            del self._data[oldest]
        self._data[_key(url, accept=accept)] = CacheEntry(
            url=url,
            status=status,
            headers=headers,
            body=body,
            stored_at=time.time(),
            etag=etag,
            last_modified=last_modified,
        )

    def revalidate(self, url: str, *, accept: str = "text/html") -> CacheEntry | None:
        key = _key(url, accept=accept)
        entry = self._data.get(key)
        if entry is None:
            return None
        refreshed = CacheEntry(
            url=entry.url,
            status=entry.status,
            headers=entry.headers,
            body=entry.body,
            stored_at=time.time(),
            etag=None,
            last_modified=None,
        )
        self._data[key] = refreshed
        return refreshed

    def clear(self) -> int:
        count = len(self._data)
        self._data.clear()
        return count

    def stats(self) -> dict[str, Any]:
        return {
            "backend": "memory",
            "entries": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
        }


class HttpCache(ResponseCache):
    """Filesystem-backed cache, safe for concurrent processes via atomic writes."""

    def __init__(self, root: Path, *, max_body_bytes: int = 8 * 1024 * 1024) -> None:
        self.root = root
        self.max_body_bytes = max_body_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _paths(self, url: str, accept: str) -> tuple[Path, Path]:
        digest = _key(url, accept=accept)
        bucket = self.root / digest[:2]
        return bucket / f"{digest}.json", bucket / f"{digest}.bin"

    def get(self, url: str, *, accept: str = "text/html") -> CacheEntry | None:
        meta_path, body_path = self._paths(url, accept)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.misses += 1
            return None
        if meta.get("v") != _CACHE_VERSION:
            self.misses += 1
            return None
        try:
            body = body_path.read_bytes()
        except OSError:
            self.misses += 1
            return None
        self.hits += 1
        return CacheEntry(
            url=meta["url"],
            status=meta["status"],
            headers=meta.get("headers", {}),
            body=body,
            stored_at=meta["stored_at"],
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
        )

    def put(
        self,
        url: str,
        *,
        status: int,
        headers: dict[str, str],
        body: bytes,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str = "text/html",
    ) -> None:
        if len(body) > self.max_body_bytes:
            return
        meta_path, body_path = self._paths(url, accept)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "v": _CACHE_VERSION,
            "url": url,
            "status": status,
            "headers": headers,
            "stored_at": time.time(),
            "etag": etag,
            "last_modified": last_modified,
        }
        _atomic_write(body_path, body)
        _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False).encode("utf-8"))

    def revalidate(self, url: str, *, accept: str = "text/html") -> CacheEntry | None:
        entry = self.get(url, accept=accept)
        if entry is None:
            return None
        self.put(
            url,
            status=entry.status,
            headers=entry.headers,
            body=entry.body,
            accept=accept,
        )
        return self.get(url, accept=accept)

    def clear(self) -> int:
        count = 0
        for path in self.root.rglob("*.json"):
            try:
                path.unlink()
                count += 1
            except OSError:
                continue
        for path in self.root.rglob("*.bin"):
            try:
                path.unlink()
            except OSError:
                continue
        return count

    def stats(self) -> dict[str, Any]:
        entries = sum(1 for _ in self.root.rglob("*.json"))
        return {
            "backend": "filesystem",
            "root": str(self.root),
            "entries": entries,
            "hits": self.hits,
            "misses": self.misses,
        }


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Path.replace is os.replace under the hood: atomic on POSIX and Windows.
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def entry_to_dict(entry: CacheEntry) -> dict[str, Any]:
    """Serialise metadata only; the body is intentionally excluded."""
    return asdict(entry) | {"body": f"<{len(entry.body)} bytes>"}
