"""HTTP response caching, in both backends.

`MemoryCache` and `HttpCache` implement the same `ResponseCache` contract, so
most of these tests are parameterised over both. That is the point: a bug fixed
in one backend but not the other would show up as a parameterised failure rather
than as a production-only surprise.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from awsa.net.cache import CacheEntry, HttpCache, MemoryCache, ResponseCache, entry_to_dict

URL = "https://example.com/page"


@pytest.fixture(params=["memory", "filesystem"])
def cache(request: pytest.FixtureRequest, tmp_path: Path) -> ResponseCache:
    if request.param == "memory":
        return MemoryCache()
    return HttpCache(tmp_path / "cache")


def _put(cache: ResponseCache, url: str = URL, **kwargs: object) -> None:
    defaults: dict[str, object] = {
        "status": 200,
        "headers": {"Content-Type": "text/html"},
        "body": b"<html>hello</html>",
    }
    defaults.update(kwargs)
    cache.put(url, **defaults)  # type: ignore[arg-type]


class TestRoundTrip:
    def test_miss_then_hit(self, cache: ResponseCache) -> None:
        assert cache.get(URL) is None
        _put(cache)
        entry = cache.get(URL)
        assert entry is not None
        assert entry.status == 200
        assert entry.body == b"<html>hello</html>"
        assert entry.headers["Content-Type"] == "text/html"

    def test_url_is_preserved(self, cache: ResponseCache) -> None:
        _put(cache)
        assert cache.get(URL).url == URL  # type: ignore[union-attr]

    def test_conditional_metadata_round_trips(self, cache: ResponseCache) -> None:
        _put(cache, etag='"v1"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT")
        entry = cache.get(URL)
        assert entry is not None
        assert entry.etag == '"v1"'
        assert entry.last_modified == "Wed, 01 Jan 2025 00:00:00 GMT"

    def test_accept_header_is_part_of_the_key(self, cache: ResponseCache) -> None:
        # A JSON API response and an HTML rendering of the same URL are not
        # interchangeable; they must not share a cache slot.
        _put(cache, accept="text/html", body=b"html")
        _put(cache, accept="application/json", body=b"{}")
        assert cache.get(URL, accept="text/html").body == b"html"  # type: ignore[union-attr]
        assert cache.get(URL, accept="application/json").body == b"{}"  # type: ignore[union-attr]

    def test_distinct_urls_do_not_collide(self, cache: ResponseCache) -> None:
        _put(cache, "https://example.com/a", body=b"AAA")
        _put(cache, "https://example.com/b", body=b"BBB")
        assert cache.get("https://example.com/a").body == b"AAA"  # type: ignore[union-attr]
        assert cache.get("https://example.com/b").body == b"BBB"  # type: ignore[union-attr]

    def test_overwrite_replaces_the_body(self, cache: ResponseCache) -> None:
        _put(cache, body=b"first")
        _put(cache, body=b"second")
        assert cache.get(URL).body == b"second"  # type: ignore[union-attr]


class TestStats:
    def test_counts_hits_and_misses(self, cache: ResponseCache) -> None:
        cache.get(URL)
        _put(cache)
        cache.get(URL)
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["entries"] == 1

    def test_backend_is_reported(self, cache: ResponseCache) -> None:
        assert cache.stats()["backend"] in {"memory", "filesystem"}


class TestRevalidation:
    def test_revalidate_refreshes_the_timestamp(self, cache: ResponseCache) -> None:
        _put(cache, etag='"v1"')
        before = cache.get(URL)
        assert before is not None
        time.sleep(0.01)
        after = cache.revalidate(URL)
        assert after is not None
        assert after.stored_at >= before.stored_at
        assert after.age() < before.age()

    def test_revalidate_keeps_status_and_body(self, cache: ResponseCache) -> None:
        _put(cache, status=201, body=b"payload")
        after = cache.revalidate(URL)
        assert after is not None
        assert after.status == 201
        assert after.body == b"payload"

    def test_revalidate_of_a_miss_returns_none(self, cache: ResponseCache) -> None:
        assert cache.revalidate("https://example.com/never-stored") is None

    def test_revalidate_drops_stale_validators(self, cache: ResponseCache) -> None:
        # After a 304 the stored copy is authoritative, so the old validator
        # must not be replayed or we would loop forever.
        _put(cache, etag='"v1"')
        after = cache.revalidate(URL)
        assert after is not None
        assert after.etag is None
        assert after.last_modified is None


class TestClear:
    def test_clear_empties_the_cache(self, cache: ResponseCache) -> None:
        _put(cache)
        _put(cache, "https://example.com/other")
        removed = cache.clear()
        assert removed >= 1
        assert cache.get(URL) is None
        assert cache.get("https://example.com/other") is None

    def test_clear_on_an_empty_cache_is_zero(self, cache: ResponseCache) -> None:
        assert cache.clear() == 0


class TestFreshness:
    def test_is_fresh_within_ttl(self) -> None:
        entry = CacheEntry(url=URL, status=200, headers={}, body=b"", stored_at=time.time())
        assert entry.is_fresh(60.0) is True

    def test_is_stale_past_ttl(self) -> None:
        entry = CacheEntry(url=URL, status=200, headers={}, body=b"", stored_at=time.time() - 120)
        assert entry.is_fresh(60.0) is False

    def test_age_uses_the_supplied_clock(self) -> None:
        entry = CacheEntry(url=URL, status=200, headers={}, body=b"", stored_at=1000.0)
        assert entry.age(now=1010.0) == pytest.approx(10.0)


class TestMemoryEviction:
    def test_evicts_the_oldest_when_full(self) -> None:
        cache = MemoryCache(max_entries=2)
        _put(cache, "https://example.com/1", body=b"1")
        time.sleep(0.01)
        _put(cache, "https://example.com/2", body=b"2")
        time.sleep(0.01)
        _put(cache, "https://example.com/3", body=b"3")
        assert cache.get("https://example.com/3") is not None
        # The first insert is the oldest, so it is the one dropped.
        assert cache.get("https://example.com/1") is None
        assert cache.get("https://example.com/2") is not None


class TestFilesystemDetails:
    def test_creates_its_root_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "deep" / "nested" / "cache"
        HttpCache(root)
        assert root.is_dir()

    def test_oversized_body_is_not_stored(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path / "cache", max_body_bytes=16)
        _put(cache, body=b"x" * 100)
        assert cache.get(URL) is None

    def test_corrupt_metadata_reads_as_a_miss_not_a_crash(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path / "cache")
        _put(cache)
        meta_path = next((tmp_path / "cache").rglob("*.json"))
        meta_path.write_text("{not json", encoding="utf-8")
        assert cache.get(URL) is None

    def test_missing_body_reads_as_a_miss(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path / "cache")
        _put(cache)
        next((tmp_path / "cache").rglob("*.bin")).unlink()
        assert cache.get(URL) is None

    def test_stale_cache_version_reads_as_a_miss(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path / "cache")
        _put(cache)
        meta_path = next((tmp_path / "cache").rglob("*.json"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["v"] = -1
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        # A format change must invalidate rather than misparse.
        assert cache.get(URL) is None

    def test_leaves_no_temp_files_behind(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path / "cache")
        _put(cache)
        _put(cache, "https://example.com/2")
        assert not list((tmp_path / "cache").rglob(".tmp-*"))


class TestSerialisation:
    def test_entry_to_dict_omits_the_body_bytes(self) -> None:
        entry = CacheEntry(url=URL, status=200, headers={}, body=b"secret-bytes", stored_at=1.0)
        rendered = entry_to_dict(entry)
        assert rendered["body"] == "<12 bytes>"
        # Serialising metadata must never spill a response body into a log.
        assert "secret-bytes" not in json.dumps(rendered)

    def test_as_meta_reports_body_size(self) -> None:
        entry = CacheEntry(url=URL, status=200, headers={}, body=b"abcd", stored_at=1.0)
        meta = entry.as_meta()
        assert meta["body_bytes"] == 4
        assert meta["url"] == URL
        assert meta["etag"] is None
