"""The autonomous research loop.

```
        ┌──────────────────────────────────────────────┐
        │  1. plan  : spec ──▶ search queries (LLM)      │
        │  2. search: query ─▶ ranked URLs              │
        │  3. triage: score URLs, enforce domain policy  │
        │  4. fetch : polite, cached, budgeted          │
        │  5. reduce: HTML ─▶ readable text + metadata   │
        │  6. extract: text ─▶ field candidates (LLM)   │
        │  7. reconcile: candidates ─▶ claims           │
        │  8. check : enough coverage? else loop        │
        └──────────────────────────────────────────────┘
```

Every stage is bounded by :class:`~awsa.models.Budget` and by a wall-clock
deadline, and every stage degrades rather than aborting: a failing search
provider falls back, a failing LLM falls back to the deterministic extractor,
an unreadable page is skipped. The loop only stops when coverage is adequate,
the budget is spent, or the deadline passes — and it always returns a report,
including when everything failed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..errors import (
    AwsaError,
    BudgetExhausted,
    NetworkBlocked,
    ProviderError,
    RobotsDenied,
    UnsafeURLError,
)
from ..extract import extract_structured_fields, read_page
from ..models import (
    Budget,
    Evidence,
    FieldCandidate,
    ResearchReport,
    SearchHit,
    Source,
    Usage,
    dedupe_hits,
)
from ..net import HttpFetcher
from ..observability import Trace, configure_logging, get_logger
from ..providers import (
    ExtractedField,
    ExtractRequest,
    LLMStack,
    PlanRequest,
    Registry,
    SearchStack,
)
from .spec import ResearchSpec
from .verifier import reconcile

__all__ = ["AgentResult", "AutonomousScraperAgent", "FetchedPage", "run_research"]

log = get_logger("agent")

_EXTRACT_CHAR_BUDGET = 12_000
_MIN_PAGE_WORDS = 25
_MAX_SNIPPET = 400
_ARTICLE_HINTS = ("about", "bio", "profile", "author", "team", "contact", "who is")


@dataclass(slots=True)
class FetchedPage:
    """A page reduced to text, plus any publisher-declared structured data."""

    source: Source
    structured: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class AgentResult:
    """A report plus the trace and provider metadata that produced it."""

    report: ResearchReport
    trace: Trace
    providers: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    stopped_because: str = "coverage"

    @property
    def ok(self) -> bool:
        return bool(self.report.claims) and self.report.answered > 0


class AutonomousScraperAgent:
    """Plan, search, fetch, extract and reconcile, within a fixed budget.

    Args:
        config: Runtime configuration; provider selection and politeness.
        spec: The research request.
        fetcher: Optional pre-built fetcher, for tests or shared pools.
        registry: Optional provider registry override.
    """

    def __init__(
        self,
        config: Config | None = None,
        spec: ResearchSpec | None = None,
        *,
        fetcher: HttpFetcher | None = None,
        registry: Registry | None = None,
    ) -> None:
        self.config = config or Config()
        if spec is not None:
            self.spec = spec
        self.registry = registry or Registry(self.config)
        self._closed = False
        self._challenge_reported: set[str] = set()
        self._fetcher = fetcher
        self._owns_fetcher = fetcher is None
        self.trace = Trace()
        self.notes: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def _fetcher_or_new(self) -> HttpFetcher:
        if self._fetcher is None:
            self._fetcher = HttpFetcher(self.config)
        return self._fetcher

    async def _aclose(self) -> None:
        """Release owned resources. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        if self._fetcher is not None and self._owns_fetcher:
            await self._fetcher.close()
        await self.registry.aclose()

    async def __aenter__(self) -> AutonomousScraperAgent:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._aclose()

    def _note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)
        log.info("%s", message)

    # -- budget guard ------------------------------------------------------

    def _check_budget(self, usage: Usage, budget: Budget) -> str | None:
        """Return a stop reason, or ``None`` to keep going."""
        if usage.search_queries >= budget.max_search_queries:
            return "search budget exhausted"
        if usage.pages_fetched + usage.pages_from_cache >= budget.max_pages:
            return "page budget exhausted"
        if usage.llm_calls >= budget.max_llm_calls:
            return "llm budget exhausted"
        if usage.elapsed >= budget.max_wall_seconds:
            return "wall-clock deadline reached"
        if usage.bytes_downloaded >= budget.max_bytes_downloaded:
            return "byte budget exhausted"
        return None

    # -- main entry point --------------------------------------------------

    async def run(self, *, stop_on_coverage: bool = True) -> AgentResult:
        """Execute the loop and return a report. Never raises for content failures."""
        configure_logging(self.config.log_level)
        spec = self.spec
        budget = spec.budget
        started = time.perf_counter()
        usage = Usage()
        candidates: list[FieldCandidate] = []
        sources: list[Source] = []
        queries: list[str] = []
        per_domain: dict[str, int] = {}
        seen_urls: set[str] = set()

        search = self.registry.search_stack()
        llm = self.registry.llm_stack()
        fetcher = self._fetcher_or_new()

        with self.trace.span("providers"):
            self.trace.spans[-1].detail["search"] = ",".join(p.name for p in search.candidates())
            self.trace.spans[-1].detail["llm"] = ",".join(p.name for p in llm.candidates())

        try:
            async with fetcher:
                pending = await self._plan(spec, llm, queries, usage, budget)
                if not pending:
                    self._note("no candidate URLs were found; nothing to fetch")

                while pending:
                    reason = self._check_budget(usage, budget)
                    if reason:
                        self._note(f"stopping: {reason}")
                        break

                    hit = self._next_hit(pending, spec, seen_urls, per_domain)
                    if hit is None:
                        break
                    seen_urls.add(hit.url)

                    page = await self._fetch_one(fetcher, hit, usage, spec)
                    if page is None:
                        continue
                    host = spec.domain_of(hit.url)
                    per_domain[host] = per_domain.get(host, 0) + 1
                    sources.append(page.source)

                    candidates.extend(await self._extract_one(llm, spec, page, usage))

                    if stop_on_coverage and self._covered(spec, candidates):
                        self.trace.record("stop", 0, reason="coverage target met")
                        break
        except BudgetExhausted as exc:
            self._note(f"stopping: {exc}")
        except asyncio.CancelledError:
            raise
        except AwsaError as exc:
            self._note(f"run aborted: {exc}")

        # Describe the provider stacks *before* shutting them down: asking a
        # closed registry to describe itself would rebuild fresh clients and
        # leak their sockets.
        providers = self.registry.describe()
        provider_notes = list(self.registry.notes)

        claims, unresolved = reconcile(spec, candidates)
        usage = _with_llm_usage(usage, llm)

        report = ResearchReport(
            subject=spec.subject,
            claims=claims,
            sources=tuple(sources),
            usage=usage,
            budget=budget,
            queries=tuple(queries),
            unresolved_fields=unresolved,
            duration_seconds=time.perf_counter() - started,
        )
        result = AgentResult(
            report=report,
            trace=self.trace,
            providers=providers,
            notes=self.notes + provider_notes,
            stopped_because=self._stop_reason(usage, budget, report, stop_on_coverage),
        )
        await self._aclose()
        return result

    def _stop_reason(
        self, usage: Usage, budget: Budget, report: ResearchReport, stop_on_coverage: bool
    ) -> str:
        if stop_on_coverage and not report.unresolved_fields:
            return "all fields covered"
        return self._check_budget(usage, budget) or "ran out of candidate URLs"

    # -- stage 1: plan -----------------------------------------------------

    async def _plan(
        self,
        spec: ResearchSpec,
        llm: LLMStack,
        seen_queries: list[str],
        usage: Usage,
        budget: Budget,
    ) -> list[SearchHit]:
        request = PlanRequest(
            subject=spec.subject,
            fields=spec.field_names,
            field_hints=spec.field_hints,
            existing_queries=tuple(seen_queries),
            max_queries=max(1, min(budget.max_search_queries, 6)),
            language=spec.language,
        )
        with self.trace.span("plan"):
            planned, _ = await self._call_llm(llm, "plan_queries", request)
            queries = [str(q).strip() for q in planned if str(q).strip()]
            if not queries:
                self._note("query planner returned nothing")
                return []
            seen_queries.extend(queries)
            self.trace.spans[-1].detail["queries"] = len(queries)
        return await self._search_all(spec, list(queries), usage, budget)

    async def _call_llm(
        self, stack: LLMStack, method: str, request: PlanRequest | ExtractRequest
    ) -> tuple[list[str] | list[ExtractedField], str]:
        """Call an LLM method across the stack until one returns results.

        ``method`` is ``"plan_queries"`` or ``"extract_fields"``; the request
        type and the return element type are fixed by that pair, which the
        callers below pair up accordingly.

        Returns the items together with the name of the provider that produced
        them, so downstream confidence accounting attributes work to the right
        model instead of guessing.
        """
        for provider in stack.candidates():
            try:
                result = await getattr(provider, method)(request)
            except ProviderError as exc:
                log.warning("provider %s failed on %s: %s", provider.name, method, exc)
                continue
            except Exception as exc:
                log.warning("provider %s raised on %s: %s", provider.name, method, exc)
                continue
            if result:
                return list(result), provider.name
        return [], ""

    # -- stage 2: search ---------------------------------------------------

    async def _search_all(
        self,
        spec: ResearchSpec,
        queries: list[str],
        usage: Usage,
        budget: Budget,
    ) -> list[SearchHit]:
        stack = self.registry.search_stack()
        hits: list[SearchHit] = []
        with self.trace.span("search", queries=len(queries)):
            span = self.trace.spans[-1]
            for query in queries:
                if usage.search_queries >= budget.max_search_queries:
                    self._note("stopping: search budget exhausted")
                    break
                found = await self._search_one(stack, query, limit=10)
                usage.search_queries += 1
                span.detail["hits"] = int(span.detail.get("hits", 0)) + len(found)
                hits.extend(found)
        return self._rank_hits(hits, spec)

    async def _search_one(self, stack: SearchStack, query: str, *, limit: int) -> list[SearchHit]:
        for provider in stack.candidates():
            try:
                results = await provider.search(query, limit=limit)
            except NetworkBlocked as exc:
                # A policy refusal, not a provider fault. Staying silent here
                # would make --no-network look like "the web had no answers".
                if "network" not in self._challenge_reported:
                    self._challenge_reported.add("network")
                    self._note(str(exc))
                continue
            except ProviderError as exc:
                log.warning("search provider %s failed: %s", provider.name, exc)
                continue
            except Exception as exc:
                log.warning("search provider %s raised: %s", provider.name, exc)
                continue
            if results:
                return list(results)
            if getattr(provider, "blocked_by_challenge", False):
                # An anonymous endpoint refused us, which is different from
                # "this query genuinely has no results". Say so once, and say
                # what to do about it, instead of repeating it per query.
                if provider.name not in self._challenge_reported:
                    self._challenge_reported.add(provider.name)
                    self._note(
                        f"search provider {provider.name} served a bot challenge instead of "
                        f"results; it is refusing anonymous clients. "
                        f"Set AWSA_BRAVE_API_KEY for reliable search."
                    )
                continue
            self._note(f"search provider {provider.name} returned no results for {query!r}")
        return []

    def _rank_hits(self, hits: Sequence[SearchHit], spec: ResearchSpec) -> list[SearchHit]:
        """Filter by domain policy, de-duplicate, then order by intent match."""
        allowed = [h for h in hits if spec.allows_domain(h.url)]
        blocked = len(hits) - len(allowed)
        if blocked:
            log.debug("excluded %d hit(s) by domain policy", blocked)
        unique = dedupe_hits(allowed)
        subject_tokens = {t.lower() for t in spec.subject.split()}
        return sorted(
            unique,
            key=lambda h: (self._intent_score(h, subject_tokens), h.rank),
        )

    @staticmethod
    def _intent_score(hit: SearchHit, subject_tokens: set[str]) -> int:
        haystack = f"{hit.title} {hit.url} {hit.snippet}".lower()
        score = 0
        for token in subject_tokens:
            if len(token) > 2 and token in haystack:
                score += 2
        if any(hint in haystack for hint in _ARTICLE_HINTS):
            score += 3
        if any(host in haystack for host in ("linkedin", "github", "wikipedia", "medium")):
            score += 1
        return -score

    # -- stage 3: triage ---------------------------------------------------

    def _next_hit(
        self,
        pending: list[SearchHit],
        spec: ResearchSpec,
        seen_urls: set[str],
        per_domain: dict[str, int],
    ) -> SearchHit | None:
        """Pop the next URL worth fetching, skipping duplicates and over-used hosts."""
        while pending:
            hit = pending.pop(0)
            if hit.url in seen_urls:
                continue
            host = spec.domain_of(hit.url)
            if per_domain.get(host, 0) >= spec.max_pages_per_domain:
                continue
            return hit
        return None

    # -- stage 4/5: fetch and reduce ---------------------------------------

    async def _fetch_one(
        self,
        fetcher: HttpFetcher,
        hit: SearchHit,
        usage: Usage,
        spec: ResearchSpec,
    ) -> FetchedPage | None:
        with self.trace.span("fetch", host=spec.domain_of(hit.url)):
            try:
                result = await fetcher.fetch(hit.url)
            except RobotsDenied as exc:
                usage.robots_denials += 1
                log.info("skipping %s: %s", hit.url, exc)
                self._note(f"robots.txt disallows {hit.url}")
                return None
            except UnsafeURLError as exc:
                usage.blocked_urls += 1
                log.info("skipping %s: %s", hit.url, exc)
                return None
            except AwsaError as exc:
                usage.fetch_errors += 1
                log.warning("fetch failed for %s: %s", hit.url, exc)
                return None

            if result.from_cache:
                usage.pages_from_cache += 1
            else:
                usage.pages_fetched += 1
            usage.bytes_downloaded += len(result.body)

            if not result.ok:
                log.debug("skipping %s: HTTP %d", hit.url, result.status)
                usage.fetch_errors += 1
                return None

            content = read_page(result.final_url, result.body, self.config.extraction)
            self.trace.spans[-1].detail["words"] = content.word_count

            if content.word_count < _MIN_PAGE_WORDS:
                log.debug("skipping %s: only %d words", hit.url, content.word_count)
                return None

            return FetchedPage(
                source=Source(
                    url=result.url,
                    final_url=result.final_url,
                    text=content.text,
                    title=content.title or hit.title,
                    status=result.status,
                    elapsed_ms=result.elapsed_ms,
                    from_cache=result.from_cache,
                    content_type=result.content_type,
                    links=content.links,
                ),
                structured=extract_structured_fields(content),
            )

    # -- stage 6: extract --------------------------------------------------

    async def _extract_one(
        self, stack: LLMStack, spec: ResearchSpec, page: FetchedPage, usage: Usage
    ) -> list[FieldCandidate]:
        source = page.source
        request = ExtractRequest(
            subject=spec.subject,
            page_title=source.title,
            page_url=source.final_url,
            page_text=source.text[:_EXTRACT_CHAR_BUDGET],
            fields=spec.field_names,
            field_hints=spec.field_hints,
            max_snippet_chars=_MAX_SNIPPET,
        )
        with self.trace.span("extract", host=spec.domain_of(source.final_url)):
            extracted, provider_name = await self._call_llm(stack, "extract_fields", request)
            if extracted:
                usage.llm_calls += 1
            self.trace.spans[-1].detail["fields"] = len(extracted)
            self.trace.spans[-1].detail["extractor"] = provider_name or "none"

        out: list[FieldCandidate] = []
        for item in extracted:
            out.append(
                FieldCandidate(
                    field=str(getattr(item, "field", "")),
                    value=str(getattr(item, "value", "")),
                    confidence=float(getattr(item, "confidence", 0.5)),
                    extractor=_extractor_name(item, provider_name),
                    evidence=Evidence(
                        source_url=source.final_url,
                        quote=str(getattr(item, "quote", "")),
                        source_title=source.title,
                        char_start=int(getattr(item, "char_start", 0) or 0),
                    ),
                )
            )
        out.extend(_structured_candidates(spec, source, page.structured))
        return out

    # -- coverage ----------------------------------------------------------

    def _covered(self, spec: ResearchSpec, candidates: Sequence[FieldCandidate]) -> bool:
        """True when every required field has at least one candidate from 2+ domains."""
        seen: dict[str, set[str]] = {}
        for candidate in candidates:
            host = spec.domain_of(candidate.evidence.source_url)
            if host:
                seen.setdefault(candidate.field, set()).add(host)
        for field_spec in spec.fields:
            if not field_spec.required:
                continue
            if len(seen.get(field_spec.name, ())) < spec.min_independent_sources:
                return False
        return True


def _extractor_name(item: object, provider_name: str = "") -> str:
    """Label of which provider produced a field value.

    Providers that label their own output win; otherwise fall back to the name
    of the provider that actually answered, and finally to ``heuristic`` — never
    ``unknown``, which would read as a failure rather than as a default.
    """
    name = getattr(item, "extractor", None)
    if isinstance(name, str) and name:
        return name
    return provider_name or "heuristic"


def _structured_candidates(
    spec: ResearchSpec, source: Source, structured: dict[str, object]
) -> list[FieldCandidate]:
    """Turn publisher-declared JSON-LD/OpenGraph values into high-confidence claims."""
    mapping = {
        "name": "name",
        "author": "name",
        "headline": "role",
        "addressLocality": "city",
        "addressCountry": "country",
        "description": "bio",
    }
    lowered = {f.name.lower().replace(" ", "_"): f.name for f in spec.fields}
    out: list[FieldCandidate] = []
    for key, value in structured.items():
        if not isinstance(value, str) or not value.strip():
            continue
        target = lowered.get(mapping.get(key, ""), "")
        if not target:
            target = lowered.get(key.lower().replace(" ", "_"), "")
        if not target:
            continue
        out.append(
            FieldCandidate(
                field=target,
                value=value,
                confidence=0.7 if _quote_for(value, source.text) else 0.5,
                extractor="structured-data",
                evidence=Evidence(
                    source_url=source.final_url,
                    # Metadata the page declares but never shows in its prose
                    # has no verbatim span to quote. Labelling it as metadata is
                    # honest; passing the value off as a quotation is not, so
                    # the value is kept but marked, and priced lower.
                    quote=_quote_for(value, source.text) or f"[structured data] {value}",
                    source_title=source.title,
                ),
            )
        )
    return out


def _quote_for(value: str, text: str) -> str:
    position = text.find(value)
    if position < 0:
        return ""
    start = max(0, text.rfind(".", 0, position) + 1)
    tail = text.find(".", position)
    end = tail if tail > 0 else min(len(text), position + 300)
    return text[start:end].strip()


def _with_llm_usage(usage: Usage, stack: LLMStack) -> Usage:
    from dataclasses import replace

    from ..providers.registry import merge_usage

    total = merge_usage(tuple(stack.candidates()))
    return replace(
        usage,
        llm_calls=total.calls or usage.llm_calls,
        llm_input_tokens=total.input_tokens,
        llm_output_tokens=total.output_tokens,
    )


async def run_research(
    subject: str,
    fields: Sequence[str],
    **kwargs: Any,
) -> AgentResult:
    """Convenience wrapper: build a spec, run the agent, return the result.

    Args:
        subject: Who or what to research.
        fields: Field names, optionally ``"name:hint"`` strings.
        **kwargs: Forwarded to :class:`ResearchSpec`; ``config`` is pulled out
            and passed to the agent.

    Returns:
        The :class:`AgentResult` for the run.
    """
    config = kwargs.pop("config", None) or Config()
    spec = ResearchSpec.build(subject, list(fields), **kwargs)
    agent = AutonomousScraperAgent(config, spec)
    return await agent.run()
