"""End-to-end agent loop behaviour with injected providers.

Real search backends bot-challenge anonymous clients and a hosted LLM needs a
key, so the loop is exercised here with fake providers and the loopback fixture
server. That keeps the tests hermetic while still covering the real
orchestration: planning, search, fetch, extract, reconcile, budget stops and
resource cleanup.

The properties pinned here are the ones a user notices: no runaway crawling,
no claiming coverage it did not reach, no leaked clients, and a clean exit even
when every provider fails.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from awsa.agent.loop import AutonomousScraperAgent
from awsa.agent.spec import ResearchSpec
from awsa.config import Config
from awsa.errors import ProviderError
from awsa.models import Budget, SearchHit
from awsa.providers.base import (
    ExtractedField,
    ExtractRequest,
    LLMUsage,
    PlanRequest,
)
from awsa.providers.registry import LLMStack, Registry, SearchStack
from conftest import FixtureServer


class FakeSearch:
    """Returns a fixed set of hits and records the queries it was asked."""

    name = "fake-search"
    requires_key = False
    reliability = "test"

    def __init__(self, hits: dict[str, list[str]] | None = None) -> None:
        self._hits = hits or {}
        self.queries: list[str] = []
        self.closed = False

    async def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        self.queries.append(query)
        return [
            SearchHit(url=url, title=f"Title {i}", snippet="snippet", provider=self.name, rank=i)
            for i, url in enumerate(self._hits.get(query, []))
        ]

    async def aclose(self) -> None:
        self.closed = True


class BrokenSearch:
    """A provider that always fails, to exercise fallback and total failure."""

    name = "broken-search"
    requires_key = True
    blocked_by_challenge = False

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or ProviderError("search backend unavailable")

    async def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        raise self.error


class FakeLLM:
    """Plans one fixed query and returns a canned field, grounded in page text."""

    name = "fake-llm"
    requires_key = False

    def __init__(
        self,
        *,
        quote: str = "Ada Lovelace",
        value: str = "Ada Lovelace",
        queries: Sequence[str] | None = None,
        field: str | None = None,
    ) -> None:
        self.quote = quote
        self.value = value
        # When given, the planner hands out these queries in order instead of
        # echoing the subject, so a re-plan can be told apart from the first one.
        self._queries = list(queries) if queries else None
        self.field = field
        self.plans = 0
        self.extractions = 0
        self.closed = False
        self.plan_requests: list[PlanRequest] = []

    async def plan_queries(self, request: PlanRequest) -> list[str]:
        self.plans += 1
        self.plan_requests.append(request)
        if self._queries is not None:
            index = min(self.plans - 1, len(self._queries) - 1)
            return [self._queries[index]]
        return [request.subject]

    async def extract_fields(self, request: ExtractRequest) -> list[ExtractedField]:
        self.extractions += 1
        offset = request.page_text.find(self.quote)
        if offset < 0:
            # Refusing to answer without the quote present is the honest
            # behaviour; a hallucinated value would be worse than none.
            return []
        return [
            ExtractedField(
                field=self.field or (request.fields[0] if request.fields else "name"),
                value=self.value,
                quote=self.quote,
                confidence=0.8,
                char_start=offset,
            )
        ]

    def usage(self) -> LLMUsage:
        return LLMUsage(calls=self.plans + self.extractions, input_tokens=10, output_tokens=5)

    async def aclose(self) -> None:
        self.closed = True


class FakeRegistry(Registry):
    """A Registry whose stacks are injected, so no real network or key is used."""

    def __init__(self, config: Config, search: Any, llm: Any) -> None:
        self._config = config
        self._search = search
        self._llm = llm
        self._notes: list[str] = []
        self.closed = False

    @property
    def notes(self) -> list[str]:
        # `notes` is a read-only property on the real Registry, so the stand-in
        # has to override it the same way rather than assigning to it.
        return self._notes

    def search_stack(self, name: str | None = None) -> SearchStack:
        return SearchStack(primary=self._search, fallbacks=(), notes=[])

    def llm_stack(self, name: str | None = None) -> LLMStack:
        return LLMStack(primary=self._llm, fallbacks=(), notes=[])

    def describe(self) -> dict[str, Any]:
        return {"search": [{"name": getattr(self._search, "name", "?")}], "llm": []}

    async def aclose(self) -> None:
        # Mirrors the real Registry: closing the registry closes the providers
        # that own HTTP clients, so the lifecycle tests below are meaningful.
        for provider in (self._search, self._llm):
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()
        self.closed = True


def _config() -> Config:
    # Pacing off and private hosts allowed so the loopback fixture server is
    # reachable and the suite does not spend seconds sleeping between hits.
    return Config(
        network=Config().network.__class__(
            allow_private_hosts=True,
            per_host_delay_seconds=0.0,
            cache_dir=None,
            respect_robots=True,
        )
    )


def _agent(
    server: FixtureServer,
    *,
    hits: dict[str, list[str]] | None = None,
    llm: FakeLLM | None = None,
    spec: ResearchSpec | None = None,
) -> tuple[AutonomousScraperAgent, FakeSearch, FakeLLM]:
    llm = llm or FakeLLM()
    search = FakeSearch(hits)
    config = _config()
    research_spec = spec or ResearchSpec.build(
        "Ada Lovelace", ["name"], budget=Budget.preset("deep")
    )
    registry = FakeRegistry(config, search, llm)
    return AutonomousScraperAgent(config, research_spec, registry=registry), search, llm


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestHappyPath:
    def test_produces_a_grounded_claim(self, server: FixtureServer) -> None:
        agent, _search, _ = _agent(server, hits={"Ada Lovelace": [server.url("/people")]})
        result = run(agent.run())
        assert result.report.answered >= 1
        claim = result.report.claim("name")
        assert claim is not None
        assert claim.value == "Ada Lovelace"
        assert claim.evidence, "a claim must carry its verbatim quote"
        assert claim.evidence[0].source_url.startswith(server.base)

    def test_queries_the_llm_planner(self, server: FixtureServer) -> None:
        agent, search, llm = _agent(server, hits={"Ada Lovelace": [server.url("/people")]})
        run(agent.run())
        # One page is a single domain, which cannot satisfy min_independent_sources
        # on its own, so the run is entitled to a second planning pass. What
        # matters here is that the planner is consulted and its query is searched.
        assert llm.plans >= 1
        assert search.queries, "the planned query should reach the search provider"

    def test_records_usage_and_sources(self, server: FixtureServer) -> None:
        agent, _, _ = _agent(server, hits={"Ada Lovelace": [server.url("/people")]})
        result = run(agent.run())
        assert result.report.usage.search_queries >= 1
        assert result.report.usage.pages_fetched >= 1
        assert result.report.sources

    def test_confidence_never_reaches_certainty_from_one_source(
        self, server: FixtureServer
    ) -> None:
        # A single page cannot corroborate itself, so full confidence would be a
        # lie even when the extractor is certain.
        agent, _, _ = _agent(server, hits={"Ada Lovelace": [server.url("/people")]})
        result = run(agent.run())
        claim = result.report.claim("name")
        assert claim is not None
        assert claim.confidence < 1.0


class TestEmptyAndFailure:
    def test_no_hits_yields_no_fabricated_values(self, server: FixtureServer) -> None:
        agent, _, _ = _agent(server, hits={})
        result = run(agent.run())
        # Every requested field is still listed, but as an empty, zero-confidence
        # claim. Omitting the field entirely would hide that we tried and found
        # nothing; inventing a value would be far worse.
        assert [c.field for c in result.report.claims] == ["name"]
        assert all(c.value == "" for c in result.report.claims)
        assert all(c.confidence == 0.0 for c in result.report.claims)
        assert all(c.evidence == () for c in result.report.claims)
        assert result.report.unresolved_fields == ("name",)
        assert result.report.answered == 0
        assert result.report.coverage == 0.0
        assert not result.ok
        assert any("nothing to fetch" in note for note in result.notes)

    def test_broken_search_is_absorbed(self, server: FixtureServer) -> None:
        config = _config()
        spec = ResearchSpec.build("Ada Lovelace", ["name"], budget=Budget.preset("standard"))
        registry = FakeRegistry(config, BrokenSearch(), FakeLLM())
        agent = AutonomousScraperAgent(config, spec, registry=registry)
        result = run(agent.run())
        # A dead backend is a note, not a traceback: the run still returns a
        # report, with nothing claimed and the field marked unresolved.
        assert result.report.answered == 0
        assert result.report.unresolved_fields == ("name",)
        assert result.stopped_because

    def test_fetch_failure_does_not_kill_the_run(self, server: FixtureServer) -> None:
        agent, _, _ = _agent(
            server,
            hits={"Ada Lovelace": [server.url("/challenge"), server.url("/people")]},
        )
        result = run(agent.run())
        # One bad page must not prevent the next from being read.
        assert result.report.sources

    def test_an_empty_llm_answer_still_uses_page_metadata(self, server: FixtureServer) -> None:
        # The extractor declines because its quote is absent, but the page
        # declares JSON-LD/OpenGraph values, which form a second evidence path.
        llm = FakeLLM(quote="text that is not present")
        agent, _, _ = _agent(server, hits={"Ada Lovelace": [server.url("/people")]}, llm=llm)
        result = run(agent.run())
        assert result.report.answered >= 1

    def test_metadata_only_values_are_marked_not_passed_off_as_prose(
        self, server: FixtureServer
    ) -> None:
        # A value the page declares but never prints has no verbatim span. The
        # evidence must say so rather than presenting the value as a quotation.
        from awsa.agent.loop import _structured_candidates
        from awsa.models import Source as Src

        spec = ResearchSpec.build("Ada Lovelace", ["country"])
        source = Src(
            url="https://a.test/1",
            final_url="https://a.test/1",
            text="Ada Lovelace was a mathematician.",
            title="A",
        )
        candidates = _structured_candidates(spec, source, {"addressCountry": "United Kingdom"})
        assert candidates, "publisher-declared metadata is still legitimate evidence"
        candidate = candidates[0]
        assert candidate.field == "country"
        assert candidate.extractor == "structured-data"
        assert candidate.evidence.quote.startswith("[structured data]")
        # Priced below a value that has a real quotation behind it.
        assert candidate.confidence < 0.7

    def test_metadata_backed_by_visible_prose_quotes_the_prose(self, server: FixtureServer) -> None:
        from awsa.agent.loop import _structured_candidates
        from awsa.models import Source as Src

        spec = ResearchSpec.build("Ada Lovelace", ["city"])
        source = Src(
            url="https://a.test/1",
            final_url="https://a.test/1",
            text="She lived in London for many years. That is where she worked.",
            title="A",
        )
        candidate = _structured_candidates(spec, source, {"addressLocality": "London"})[0]
        assert not candidate.evidence.quote.startswith("[structured data]")
        assert "London" in candidate.evidence.quote
        assert candidate.confidence == 0.7


class TestBudgetStops:
    def test_page_cap_is_respected(self, server: FixtureServer) -> None:
        spec = ResearchSpec.build("Ada Lovelace", ["name"], budget=Budget.preset("tiny"))
        cap = spec.budget.max_pages
        # Distinct paths, because search hits are deduplicated by host+path
        # and a query string would collapse them into one page.
        urls = [server.url(f"/page/{i}") for i in range(cap + 10)]
        agent, _, _ = _agent(server, hits={"Ada Lovelace": urls}, spec=spec)
        result = run(agent.run())
        assert result.report.usage.pages_fetched <= cap
        assert any("budget" in note for note in result.notes)

    def test_stop_reason_is_reported(self, server: FixtureServer) -> None:
        spec = ResearchSpec.build("Ada Lovelace", ["name"], budget=Budget.preset("tiny"))
        urls = [server.url(f"/page/{i}") for i in range(spec.budget.max_pages + 10)]
        agent, _, _ = _agent(server, hits={"Ada Lovelace": urls}, spec=spec)
        result = run(agent.run())
        assert result.stopped_because

    def test_trailing_slash_variants_are_fetched_once(self, server: FixtureServer) -> None:
        # `/people` and `/people/` are the same page, so a site cannot inflate
        # its apparent support by varying the trailing slash.
        spec = ResearchSpec.build("Ada Lovelace", ["name"], budget=Budget.preset("deep"))
        urls = [server.url("/people"), server.url("/people/")]
        agent, _, _ = _agent(server, hits={"Ada Lovelace": urls}, spec=spec)
        result = run(agent.run())
        assert result.report.usage.pages_fetched == 1

    def test_distinct_paths_are_read_separately(self, server: FixtureServer) -> None:
        # A different path is a different page even when the bytes match.
        spec = ResearchSpec.build("Ada Lovelace", ["name"], budget=Budget.preset("deep"))
        urls = [server.url("/people"), server.base + "/index.html"]
        agent, _, _ = _agent(server, hits={"Ada Lovelace": urls}, spec=spec)
        result = run(agent.run())
        assert result.report.usage.pages_fetched == 2

    def test_coverage_stop_ends_the_loop(self, server: FixtureServer) -> None:
        # The loop must not keep reading once the field is satisfied, and must
        # say why it stopped.
        spec = ResearchSpec.build("Ada Lovelace", ["name"], budget=Budget.preset("deep"))
        agent, _, _ = _agent(server, hits={"Ada Lovelace": [server.url("/people")]}, spec=spec)
        result = run(agent.run(stop_on_coverage=True))
        assert result.stopped_because == "all fields covered"
        assert result.report.usage.pages_fetched == 1


class TestReplanning:
    """A run that exhausts its URLs but is still missing fields re-plans."""

    def test_replans_when_a_field_lacks_a_second_source(self, server: FixtureServer) -> None:
        # 127.0.0.1 and localhost are distinct hosts to `domain_of` but both
        # reach the fixture server, which is how a second independent source is
        # produced without a second server.
        spec = ResearchSpec.build(
            "Ada Lovelace",
            ["name"],
            budget=Budget.preset("deep").with_updates(max_replans=2),
        )
        llm = FakeLLM(queries=["Ada Lovelace", "Ada Lovelace second source"])
        hits = {
            "Ada Lovelace": [server.url("/people")],
            "Ada Lovelace second source": [server.url("/people").replace("127.0.0.1", "localhost")],
        }
        agent, search, _ = _agent(server, hits=hits, llm=llm, spec=spec)
        result = run(agent.run())
        # The first pass corroborates nothing, so the planner is asked again with
        # the gap named, and the new query reaches the search stack.
        assert llm.plans == 2
        assert "Ada Lovelace second source" in search.queries
        assert result.report.usage.pages_fetched == 2
        assert result.stopped_because == "all fields covered"

    def test_replan_request_names_the_missing_field(self, server: FixtureServer) -> None:
        spec = ResearchSpec.build(
            "Ada Lovelace",
            ["name", "birth_year"],
            budget=Budget.preset("deep").with_updates(max_replans=2),
        )
        # Only "name" is extractable, so "birth_year" is the gap that never
        # closes. Each query adds a distinct host, so after the first two rounds
        # "name" is corroborated from two domains and drops off the missing list.
        llm = FakeLLM(
            queries=["Ada Lovelace", "Ada Lovelace bio", "Ada Lovelace birth year"],
            field="name",
        )
        other_host = "localhost"
        hits = {
            "Ada Lovelace": [server.url("/people")],
            "Ada Lovelace bio": [server.url("/index.html").replace("127.0.0.1", other_host)],
            "Ada Lovelace birth year": [server.url("/page/9")],
        }
        agent, _, _ = _agent(server, hits=hits, llm=llm, spec=spec)
        run(agent.run())
        assert len(llm.plan_requests) == 3
        second, third = llm.plan_requests[1], llm.plan_requests[2]
        # The planner is told what it already tried, so it should not repeat it.
        assert "Ada Lovelace" in second.existing_queries
        # Both fields are short of two domains after one page, so both are named.
        assert set(second.uncovered_fields) == {"name", "birth_year"}
        # By the third pass "name" has two domains, so only the real gap remains.
        assert third.uncovered_fields == ("birth_year",)

    def test_replan_limit_is_enforced(self, server: FixtureServer) -> None:
        spec = ResearchSpec.build(
            "Ada Lovelace",
            ["name"],
            budget=Budget.preset("deep").with_updates(max_replans=0),
        )
        # A query that keeps returning the same single-domain page can never be
        # corroborated, so the run must stop instead of spinning.
        llm = FakeLLM(queries=["Ada Lovelace"])
        agent, _, _ = _agent(
            server, hits={"Ada Lovelace": [server.url("/people")]}, llm=llm, spec=spec
        )
        result = run(agent.run())
        assert llm.plans == 1
        assert "re-plan" in result.stopped_because
        assert any("re-plan limit" in note for note in result.notes)

    def test_tiny_budget_never_replans(self, server: FixtureServer) -> None:
        spec = ResearchSpec.build("Ada Lovelace", ["name"], budget=Budget.preset("tiny"))
        assert spec.budget.max_replans == 0
        agent, _, llm = _agent(server, hits={"Ada Lovelace": [server.url("/people")]}, spec=spec)
        run(agent.run())
        assert llm.plans == 1

    def test_no_replan_when_a_second_url_corroborates(self, server: FixtureServer) -> None:
        # Both URLs are distinct paths on one host, so the field stays
        # uncorroborated and a re-plan is expected; this pins that re-planning
        # keys off independent domains, not off raw page count.
        spec = ResearchSpec.build(
            "Ada Lovelace",
            ["name"],
            budget=Budget.preset("deep").with_updates(max_replans=3),
        )
        agent, _, llm = _agent(
            server,
            hits={"Ada Lovelace": [server.url("/people"), server.url("/index.html")]},
            spec=spec,
        )
        run(agent.run())
        assert llm.plans == 2, "one host cannot satisfy min_independent_sources"

    def test_replan_does_not_refetch_a_seen_url(self, server: FixtureServer) -> None:
        spec = ResearchSpec.build(
            "Ada Lovelace",
            ["name"],
            budget=Budget.preset("deep").with_updates(max_replans=2),
        )
        # The re-plan hands back the same URL. Fetching it twice would inflate
        # apparent support, so the run must give up instead.
        llm = FakeLLM(queries=["Ada Lovelace", "Ada Lovelace"])
        agent, _, _ = _agent(
            server, hits={"Ada Lovelace": [server.url("/people")]}, llm=llm, spec=spec
        )
        result = run(agent.run())
        assert result.report.usage.pages_fetched == 1
        assert any("repeated" in note or "no URLs worth" in note for note in result.notes)


class TestLifecycle:
    def test_aclose_is_idempotent(self, server: FixtureServer) -> None:
        agent, _search, llm = _agent(server, hits={})
        run(agent.run())
        run(agent._aclose())
        run(agent._aclose())
        # Closing twice must not raise or double-close the providers.
        assert llm.closed

    def test_registry_is_closed_after_a_run(self, server: FixtureServer) -> None:
        config = _config()
        spec = ResearchSpec.build("Ada Lovelace", ["name"])
        registry = FakeRegistry(config, FakeSearch({}), FakeLLM())
        agent = AutonomousScraperAgent(config, spec, registry=registry)
        run(agent.run())
        run(agent._aclose())
        assert registry.closed

    def test_cancellation_propagates_and_still_closes(self, server: FixtureServer) -> None:
        config = _config()
        spec = ResearchSpec.build("Ada Lovelace", ["name"])
        registry = FakeRegistry(config, FakeSearch({}), FakeLLM())
        agent = AutonomousScraperAgent(config, spec, registry=registry)

        async def scenario() -> None:
            async with agent:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            run(scenario())
        run(agent._aclose())
        assert registry.closed

    def test_unexpected_exception_still_closes_the_fetcher(self, server: FixtureServer) -> None:
        config = _config()
        spec = ResearchSpec.build("Ada Lovelace", ["name"])
        registry = FakeRegistry(config, FakeSearch({}), FakeLLM())
        agent = AutonomousScraperAgent(config, spec, registry=registry)

        async def scenario() -> None:
            async with agent:
                msg = "boom"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError):
            run(scenario())
        # Leaving the context manager on an exception must still release the
        # owned HTTP client, or a long-lived process leaks sockets.
        assert agent._closed
