"""The HTTP fetcher: retries, backoff, robots, caching and size caps.

This is the only place in the codebase allowed to open a socket. Every request
passes, in order: SSRF guard, host policy, robots.txt, cache lookup, per-host
pacing, then httpx with bounded retries and exponential backoff honouring
``Retry-After``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Final, Self
from urllib.parse import urlsplit

import httpx

from ..config import Config, NetworkSettings
from ..errors import FetchError, NetworkBlocked, RobotsDenied, UnsafeURLError
from ..models import FetchResult
from ..observability import get_logger
from .cache import CacheEntry, HttpCache, MemoryCache, ResponseCache
from .client import build_async_client
from .guard import SSRFGuard
from .ratelimit import HostPacer
from .robots import RobotsCache, RobotsPolicy, decide, parse_robots

__all__ = ["HttpFetcher"]

log = get_logger("net.http")

_RETRY_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
_CHUNK: Final = 64 * 1024


class HttpFetcher:
    """Async, polite, cache-backed page fetcher.

    Args:
        config: Run configuration; ``config.network`` drives timeouts, retries,
            politeness and cache policy.
        transport: Optional httpx transport, injected by tests to avoid real I/O.
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: ResponseCache | None = None,
        guard: SSRFGuard | None = None,
    ) -> None:
        self.config = config or Config()
        self.net: NetworkSettings = self.config.network
        # `--no-network` is about sockets, not credentials: it blocks every
        # outbound request but still serves answers from a warm cache.
        self.net_enabled: bool = self.config.network_enabled
        self.guard = guard or SSRFGuard(
            allow_private_hosts=self.net.allow_private_hosts,
            allowlist=self.net.host_allowlist,
            blocklist=self.net.host_blocklist,
        )
        self.cache: ResponseCache = cache or self._default_cache()
        self.robots = RobotsCache()
        self.pacer = HostPacer(
            min_interval=self.net.per_host_delay_seconds,
            max_interval=max(30.0, self.net.per_host_delay_seconds * 30),
        )
        self._client: httpx.AsyncClient | None = None
        self._transport = transport
        self.stats: dict[str, int] = {
            "requests": 0,
            "cache_hits": 0,
            "revalidated": 0,
            "retries": 0,
            "bytes": 0,
            "robots_denials": 0,
            "blocked": 0,
        }

    def _default_cache(self) -> ResponseCache:
        if self.net.cache_dir is not None:
            return HttpCache(self.net.cache_dir)
        return MemoryCache()

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Open the underlying connection pool. Idempotent."""
        if self._client is not None:
            return
        limits = httpx.Limits(max_connections=16, max_keepalive_connections=8)
        timeout = httpx.Timeout(self.net.timeout_seconds, connect=self.net.connect_timeout_seconds)
        self._client = build_async_client(
            network_enabled=self.net_enabled,
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            verify=self.net.verify_tls,
            transport=self._transport,
            headers={
                "User-Agent": self.net.user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Language": "en",
                "Accept-Encoding": "gzip, deflate",
            },
        )

    async def close(self) -> None:
        """Close the connection pool and flush cache stats. Idempotent."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            msg = "HttpFetcher.start() must be awaited before fetching"
            raise RuntimeError(msg)
        return self._client

    # -- robots ------------------------------------------------------------

    async def _robots_for(self, url: str, client: httpx.AsyncClient) -> RobotsPolicy:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if self.robots.known(origin):
            cached = self.robots.get(origin)
            if cached is not None:
                return cached
        robots_url = f"{origin}/robots.txt"
        try:
            response = await client.get(
                robots_url,
                headers={"User-Agent": self.net.user_agent},
                timeout=httpx.Timeout(10.0),
            )
        except httpx.HTTPError as exc:
            log.debug("robots.txt unreachable for %s: %s", origin, exc)
            policy = parse_robots("", user_agent=self.net.user_agent)
            self.robots.put(origin, policy)
            return policy

        if response.status_code >= 400:
            policy = parse_robots("", user_agent=self.net.user_agent)
        else:
            policy = parse_robots(response.text, user_agent=self.net.user_agent)
        log.debug(
            "robots.txt for %s: group=%r allow=%d disallow=%d",
            origin,
            policy.matched_group,
            len(policy.rules.allow),
            len(policy.rules.disallow),
        )
        self.robots.put(origin, policy)
        return policy

    def _check_robots(self, url: str, policy: RobotsPolicy) -> None:
        if not self.net.respect_robots:
            return
        if policy.rules.crawl_delay:
            host = self.guard.check(url).host
            if host:
                self.pacer.observe_robots_delay(host, policy.rules.crawl_delay)
        decision = decide(policy, url)
        if not decision.allowed:
            self.stats["robots_denials"] += 1
            msg = f"robots.txt disallows {url}"
            raise RobotsDenied(msg, detail=decision.reason)

    # -- fetch -------------------------------------------------------------

    async def fetch(
        self,
        url: str,
        *,
        use_cache: bool = True,
        check_robots: bool | None = None,
        accept: str = "text/html",
    ) -> FetchResult:
        """Fetch ``url``, returning the raw body.

        Args:
            url: Absolute http(s) URL.
            use_cache: Serve a fresh cached copy when available.
            check_robots: Override the configured robots policy for this call.
            accept: Accept header; also part of the cache key.

        Returns:
            A :class:`FetchResult`. Non-2xx responses are returned rather than
            raised so callers can record them as evidence of a dead end.

        Raises:
            UnsafeURLError: The SSRF guard or host policy rejected the URL.
            RobotsDenied: robots.txt forbids this path.
            FetchError: Network failure or redirect loop after all retries.
        """
        entry = self.cache.get(url, accept=accept) if use_cache else None
        fresh = entry if entry is not None and entry.is_fresh(self.net.cache_ttl_seconds) else None

        if not self.net_enabled and fresh is None:
            # Checked before the guard so that a disabled network reports itself
            # rather than blaming the URL, and so a run with no sockets never
            # reaches a resolver.
            self.stats["blocked"] += 1
            raise NetworkBlocked(
                f"refusing to fetch {url!r}", detail="network access is disabled by --no-network"
            )

        verdict = self.guard.check(url)
        if not verdict.allowed:
            self.stats["blocked"] += 1
            raise UnsafeURLError(f"refusing to fetch {url!r}", detail=verdict.reason)

        # A fresh cache hit needs no socket at all, so it is served before the
        # robots lookup. robots.txt is consulted when we actually connect, which
        # is the case that matters; re-reading it on every cache hit would cost
        # a round-trip to learn nothing that changed within the TTL.
        if fresh is not None:
            self.stats["cache_hits"] += 1
            log.debug("cache hit %s", url)
            return FetchResult(
                url=url,
                status=fresh.status,
                content_type=fresh.headers.get("content-type", "text/html"),
                body=fresh.body,
                final_url=url,
                elapsed_ms=0,
                from_cache=True,
                etag=fresh.etag,
                last_modified=fresh.last_modified,
            )

        client = self._require_client()

        if check_robots if check_robots is not None else self.net.respect_robots:
            policy = await self._robots_for(url, client)
            self._check_robots(url, policy)

        # A stale entry is worth a conditional request: the server can answer
        # "not modified" without resending the body.
        conditional = entry if use_cache and entry is not None and fresh is None else None

        current_url = url
        for _hop in range(self.net.max_redirects + 1):
            result = await self._request_with_retries(
                client, current_url, conditional=conditional, accept=accept
            )
            if result.status in {301, 302, 303, 307, 308}:
                location = result.location
                next_url = httpx.URL(current_url).join(location) if location else None
                if next_url is None:
                    msg = f"redirect from {current_url} had no Location header"
                    raise FetchError(msg)
                hop_verdict = self.guard.check(str(next_url))
                if not hop_verdict.allowed:
                    self.stats["blocked"] += 1
                    raise UnsafeURLError(
                        f"redirect to {next_url} blocked", detail=hop_verdict.reason
                    )
                current_url = str(next_url)
                conditional = None
                if check_robots if check_robots is not None else self.net.respect_robots:
                    policy = await self._robots_for(current_url, client)
                    self._check_robots(current_url, policy)
                continue
            if use_cache and result.ok and not result.from_cache:
                self.cache.put(
                    result.final_url,
                    status=result.status,
                    headers={"content-type": result.content_type},
                    body=result.body,
                    etag=result.etag,
                    last_modified=result.last_modified,
                    accept=accept,
                )
                if result.final_url != url:
                    # Cache the alias too, so a later request for the original
                    # URL is served without a redirect round trip.
                    self.cache.put(
                        url,
                        status=result.status,
                        headers={"content-type": result.content_type},
                        body=result.body,
                        etag=result.etag,
                        last_modified=result.last_modified,
                        accept=accept,
                    )
            return result

        msg = f"exceeded {self.net.max_redirects} redirects starting at {url}"
        raise FetchError(msg)

    async def _request_with_retries(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        conditional: CacheEntry | None,
        accept: str,
    ) -> FetchResult:
        verdict = self.guard.check(url)
        host = verdict.host or "unknown"
        approved = verdict.resolved
        last_error: Exception | None = None
        attempts = self.net.max_retries + 1

        for attempt in range(attempts):
            await self.pacer.acquire(host)
            headers = {"Accept": accept}
            if conditional is not None:
                if conditional.etag:
                    headers["If-None-Match"] = conditional.etag
                if conditional.last_modified:
                    headers["If-Modified-Since"] = conditional.last_modified

            # Re-resolve immediately before the socket opens. A name that was
            # public at validation time may have been rebound to an internal
            # address by now, and the client resolves the name itself, so the
            # only place to catch that is right here.
            fresh = self.guard.recheck(url, approved=approved)
            if not fresh.allowed:
                self.stats["blocked"] += 1
                raise NetworkBlocked(
                    f"refusing to fetch {url!r}: {fresh.reason}", detail=fresh.reason
                )

            started = time.perf_counter()
            self.stats["requests"] += 1
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    elapsed = int((time.perf_counter() - started) * 1000)
                    if response.status_code == 304 and conditional is not None:
                        self.stats["revalidated"] += 1
                        refreshed = self.cache.revalidate(url, accept=accept)
                        body = refreshed.body if refreshed else b""
                        self.pacer.relax(host)
                        return FetchResult(
                            url=url,
                            status=conditional.status,
                            content_type=conditional.headers.get("content-type", "text/html"),
                            body=body,
                            final_url=url,
                            elapsed_ms=elapsed,
                            from_cache=True,
                        )

                    if response.status_code in _RETRY_STATUSES and attempt < attempts - 1:
                        retry_after = _parse_retry_after(response.headers.get("retry-after"))
                        delay = self.pacer.penalize(host, retry_after=retry_after)
                        log.debug(
                            "%s -> %s, backing off %.2fs (attempt %d/%d)",
                            url,
                            response.status_code,
                            delay,
                            attempt + 1,
                            attempts,
                        )
                        await response.aclose()
                        await asyncio.sleep(min(delay, self.net.backoff_max_seconds))
                        self.stats["retries"] += 1
                        continue

                    body = await self._read_capped(response)
                    self.stats["bytes"] += len(body)
                    self.pacer.relax(host)
                    return FetchResult(
                        url=url,
                        status=response.status_code,
                        content_type=response.headers.get("content-type", "text/html"),
                        body=body,
                        final_url=str(response.url),
                        elapsed_ms=elapsed,
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        location=response.headers.get("location", ""),
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    break
                delay = min(
                    self.net.backoff_max_seconds,
                    self.net.backoff_base_seconds * (2**attempt),
                )
                log.debug("network error on %s (%s), retrying in %.2fs", url, exc, delay)
                await asyncio.sleep(delay)
                self.stats["retries"] += 1

        msg = f"failed to fetch {url} after {attempts} attempt(s)"
        raise FetchError(msg, detail=str(last_error) if last_error else "exhausted retries")

    async def _read_capped(self, response: httpx.Response) -> bytes:
        """Read the body, aborting if it exceeds ``max_page_bytes``."""
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.net.max_page_bytes:
            msg = f"response too large: {declared} bytes > {self.net.max_page_bytes} cap"
            raise FetchError(msg)

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes(_CHUNK):
            total += len(chunk)
            if total > self.net.max_page_bytes:
                msg = f"response exceeded {self.net.max_page_bytes} byte cap mid-stream"
                raise FetchError(msg)
            chunks.append(chunk)
        return b"".join(chunks)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    target = when if when.tzinfo else when.replace(tzinfo=UTC)
    return max(0.0, (target - datetime.now(UTC)).total_seconds())
