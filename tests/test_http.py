"""HTTP fetching, caching, pacing and limits against a real local server.

No mocks: redirects, 503s, ``Retry-After``, chunked bodies and robots.txt are
served by a real ``http.server``, so these exercise the wire behaviour rather
than a stand-in for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import pytest

from awsa.config import Config, NetworkSettings
from awsa.errors import FetchError, NetworkBlocked, RobotsDenied, UnsafeURLError
from awsa.net.cache import HttpCache
from awsa.net.http import HttpFetcher
from awsa.net.ratelimit import HostPacer
from conftest import FixtureServer

#: The `fetcher` fixture hands back a factory so each test can open its own
#: HttpFetcher as a context manager. Typing it here keeps the test signatures
#: free of a bare Callable in every one of them.
FetcherFactory = Callable[[], Awaitable[HttpFetcher]]


@pytest.fixture
def fetcher(config: Config) -> FetcherFactory:
    async def _make() -> HttpFetcher:
        f = HttpFetcher(config)
        await f.start()
        return f

    return _make


class TestNoNetwork:
    """`--no-network` must close every socket, not merely prefer cheap providers."""

    async def test_a_fresh_url_is_refused(self, server: FixtureServer) -> None:
        config = replace(Config(), network_enabled=False)
        async with HttpFetcher(config) as f:
            with pytest.raises(NetworkBlocked) as excinfo:
                await f.fetch(server.url("/people"))
        assert "no-network" in str(excinfo.value)

    async def test_it_makes_no_request(self, server: FixtureServer) -> None:
        config = replace(Config(), network_enabled=False)
        async with HttpFetcher(config) as f:
            with pytest.raises(NetworkBlocked):
                await f.fetch(server.url("/people"))
        # The refusal happens before the socket, so the server saw nothing.
        assert server.state.requests == []

    async def test_a_warm_cache_is_still_served(
        self, server: FixtureServer, config: Config, tmp_path: Path
    ) -> None:
        # Serving a cached page is local work, so refusing the network should
        # not make a previously-answered question unanswerable. A shared disk
        # cache is what carries the answer between runs.
        url = server.url("/people")
        shared = HttpCache(tmp_path / "cache")
        async with HttpFetcher(config, cache=shared) as warm:
            await warm.fetch(url)

        cold = replace(config, network_enabled=False)
        async with HttpFetcher(cold, cache=shared) as f:
            result = await f.fetch(url)
        assert result.from_cache is True
        assert result.body

    async def test_stats_record_the_block(self, server: FixtureServer) -> None:
        config = replace(Config(), network_enabled=False)
        async with HttpFetcher(config) as f:
            with pytest.raises(NetworkBlocked):
                await f.fetch(server.url("/people"))
        assert f.stats["blocked"] == 1

    async def test_it_implies_offline(self) -> None:
        # One flag, one meaning: a run that cannot reach the network must not
        # also be trying to call a hosted model.
        config = Config.from_env({"AWSA_NO_NETWORK": "1"})
        assert config.offline is True


class TestBasicFetch:
    async def test_fetches_a_page(self, fetcher: FetcherFactory, server: FixtureServer) -> None:
        async with await fetcher() as f:
            result = await f.fetch(server.url("/people"))
        assert result.status == 200
        assert result.ok
        assert result.is_html
        assert b"Ada Lovelace" in result.body
        assert result.final_url.endswith("/people")
        assert result.elapsed_ms >= 0

    async def test_missing_page_is_not_ok(
        self, fetcher: FetcherFactory, server: FixtureServer
    ) -> None:
        async with await fetcher() as f:
            result = await f.fetch(server.url("/nope"))
        assert result.status == 404
        assert not result.ok

    async def test_works_without_explicit_start(
        self, config: Config, server: FixtureServer
    ) -> None:
        # ``fetch`` must work on a fetcher that was never started, because the
        # library entry point does not require the caller to know about start.
        async with HttpFetcher(config) as f:
            assert (await f.fetch(server.url("/people"))).status == 200

    async def test_closing_twice_is_harmless(self, config: Config) -> None:
        f = HttpFetcher(config)
        await f.start()
        await f.close()
        await f.close()


class TestSafety:
    async def test_blocks_loopback_when_not_allowed(
        self, config: Config, server: FixtureServer
    ) -> None:
        strict = replace(config, network=replace(config.network, allow_private_hosts=False))
        async with HttpFetcher(strict) as f:
            with pytest.raises(UnsafeURLError):
                await f.fetch(server.url("/people"))

    async def test_redirect_to_metadata_endpoint_is_blocked(
        self, fetcher: FetcherFactory, server: FixtureServer
    ) -> None:
        async with await fetcher() as f:
            with pytest.raises((UnsafeURLError, FetchError)):
                await f.fetch(server.url("/redirect-external"))

    async def test_redirect_limit_is_enforced(
        self, fetcher: FetcherFactory, server: FixtureServer
    ) -> None:
        async with await fetcher() as f:
            with pytest.raises(FetchError, match="redirect"):
                await f.fetch(server.url("/redirect-loop"))

    async def test_follows_a_safe_redirect(
        self, fetcher: FetcherFactory, server: FixtureServer
    ) -> None:
        async with await fetcher() as f:
            result = await f.fetch(server.url("/redirect"))
        assert result.status == 200
        assert result.final_url.endswith("/people")

    async def test_byte_cap_aborts_a_huge_body(self, config: Config, server: FixtureServer) -> None:
        server.state.huge_bytes = 2_000_000
        capped = NetworkSettings(
            user_agent=config.network.user_agent,
            allow_private_hosts=True,
            per_host_delay_seconds=0.0,
            max_page_bytes=50_000,
        )
        async with HttpFetcher(replace(config, network=capped)) as f:
            with pytest.raises(FetchError):
                await f.fetch(server.url("/huge"))


class TestRetries:
    async def test_retries_a_503(self, config: Config, server: FixtureServer) -> None:
        server.state.flaky_remaining = 2
        async with HttpFetcher(config) as f:
            result = await f.fetch(server.url("/flaky"))
        assert result.status == 200
        assert b"Recovered" in result.body
        assert f.stats["retries"] >= 2

    async def test_returns_the_error_status_after_exhausting_retries(
        self, config: Config, server: FixtureServer
    ) -> None:
        """A persistent 503 is reported as a 503, not as a transport failure.

        The caller can then say "the site returned 503", which is a different and
        more actionable statement than "the fetch failed".
        """
        server.state.flaky_remaining = 99
        async with HttpFetcher(config) as f:
            result = await f.fetch(server.url("/flaky"))
        assert result.status == 503
        assert not result.ok

    async def test_gives_up_on_an_unreachable_host(self, config: Config) -> None:
        # Port 1 on loopback is closed; the transport error path must give up
        # rather than retry forever.
        strict = replace(config, network=replace(config.network, allow_private_hosts=True))
        async with HttpFetcher(
            replace(strict, network=replace(strict.network, max_retries=1))
        ) as f:
            with pytest.raises(FetchError):
                await f.fetch("http://127.0.0.1:1/")


class TestRobots:
    async def test_denied_path_raises(self, config: Config, server: FixtureServer) -> None:
        async with HttpFetcher(config) as f:
            with pytest.raises(RobotsDenied):
                await f.fetch(server.url("/private/secret"))

    async def test_allowed_path_succeeds(self, config: Config, server: FixtureServer) -> None:
        async with HttpFetcher(config) as f:
            assert (await f.fetch(server.url("/people"))).status == 200

    async def test_robots_can_be_waived(self, config: Config, server: FixtureServer) -> None:
        waived = replace(config, network=replace(config.network, respect_robots=False))
        async with HttpFetcher(waived) as f:
            assert (await f.fetch(server.url("/private/secret"))).status == 404


class TestCaching:
    async def test_second_request_is_served_from_cache(
        self, config: Config, server: FixtureServer
    ) -> None:
        async with HttpFetcher(config) as f:
            first = await f.fetch(server.url("/people"))
            hits_after_first = server.state.hits.get("/people", 0)
            second = await f.fetch(server.url("/people"))
        assert first.status == second.status
        assert first.body == second.body
        assert server.state.hits.get("/people", 0) == hits_after_first
        assert second.from_cache

    async def test_304_refreshes_the_entry(
        self, config: Config, server: FixtureServer, tmp_path: Path
    ) -> None:
        async with HttpFetcher(config, cache=HttpCache(tmp_path / "cache")) as f:
            first = await f.fetch(server.url("/people"))
            # Force the entry stale so the next fetch revalidates; the server
            # answers 304 with no body and the cached body must be reused.
            for entry_file in (tmp_path / "cache").rglob("*.json"):
                entry_file.write_text(
                    entry_file.read_text(encoding="utf-8").replace(
                        '"stored_at":', '"stored_at": 0, "_old":'
                    ),
                    encoding="utf-8",
                )
            second = await f.fetch(server.url("/people"))
        assert first.body == second.body
        assert second.from_cache

    async def test_bypass_cache_forces_the_network(
        self, config: Config, server: FixtureServer
    ) -> None:
        async with HttpFetcher(config) as f:
            await f.fetch(server.url("/people"))
            result = await f.fetch(server.url("/people"), use_cache=False)
        assert not result.from_cache


class TestHostPacer:
    async def test_first_acquire_is_immediate(self) -> None:
        pacer = HostPacer(min_interval=0.5)
        started = asyncio.get_running_loop().time()
        await pacer.acquire("a.test")
        assert asyncio.get_running_loop().time() - started < 0.2

    async def test_second_acquire_waits_the_interval(self) -> None:
        pacer = HostPacer(min_interval=0.4)
        await pacer.acquire("a.test")
        started = asyncio.get_running_loop().time()
        await pacer.acquire("a.test")
        assert asyncio.get_running_loop().time() - started >= 0.3

    async def test_hosts_are_paced_independently(self) -> None:
        pacer = HostPacer(min_interval=0.4)
        await pacer.acquire("a.test")
        started = asyncio.get_running_loop().time()
        await pacer.acquire("b.test")
        assert asyncio.get_running_loop().time() - started < 0.2

    async def test_penalty_is_honoured_even_when_interval_is_zero(self) -> None:
        """A cooldown must not be cancelled by a zero polite interval."""
        pacer = HostPacer(min_interval=0.0)
        await pacer.acquire("a.test")
        pacer.penalize("a.test", retry_after=0.3)
        started = asyncio.get_running_loop().time()
        await pacer.acquire("a.test")
        assert asyncio.get_running_loop().time() - started >= 0.2

    async def test_relax_clears_a_penalty(self) -> None:
        pacer = HostPacer(min_interval=0.0)
        await pacer.acquire("a.test")
        pacer.penalize("a.test", retry_after=5.0)
        pacer.relax("a.test")
        started = asyncio.get_running_loop().time()
        await pacer.acquire("a.test")
        assert asyncio.get_running_loop().time() - started < 0.2
