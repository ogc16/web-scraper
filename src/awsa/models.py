"""Immutable data structures shared across the agent pipeline.

These are the contract between the network layer, the extractors and the
reporter. They are frozen dataclasses so results can be cached, hashed and
passed between threads without defensive copying.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "Budget",
    "Claim",
    "Evidence",
    "FetchResult",
    "FieldCandidate",
    "ResearchReport",
    "SearchHit",
    "Source",
    "Usage",
    "utcnow",
]

_WS = re.compile(r"\s+")


def utcnow() -> datetime:
    """Timezone-aware current time, isolated here for easy test monkeypatching."""
    return datetime.now(UTC)


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Budget:
    """Ceilings that bound a single agent run.

    Every ceiling exists because autonomous crawling without limits is how you
    accidentally hammer a host or run up an unbounded API bill.
    """

    max_search_queries: int = 8
    max_pages: int = 20
    max_llm_calls: int = 24
    max_wall_seconds: float = 120.0
    max_bytes_downloaded: int = 12 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_search_queries",
            "max_pages",
            "max_llm_calls",
            "max_bytes_downloaded",
        ):
            if getattr(self, name) < 0:
                msg = f"{name} must be >= 0, got {getattr(self, name)}"
                raise ValueError(msg)
        if self.max_wall_seconds <= 0:
            msg = f"max_wall_seconds must be > 0, got {self.max_wall_seconds}"
            raise ValueError(msg)

    @classmethod
    def preset(cls, name: str) -> Budget:
        """Return a named preset: ``tiny``, ``standard``, ``deep`` or ``none``."""
        presets: dict[str, Budget] = {
            "tiny": cls(max_search_queries=2, max_pages=4, max_llm_calls=6, max_wall_seconds=30.0),
            "standard": cls(),
            "deep": cls(
                max_search_queries=25,
                max_pages=120,
                max_llm_calls=150,
                max_wall_seconds=900.0,
                max_bytes_downloaded=64 * 1024 * 1024,
            ),
            "none": cls(
                max_search_queries=10_000,
                max_pages=10_000,
                max_llm_calls=10_000,
                max_wall_seconds=86_400.0,
                max_bytes_downloaded=2 * 1024 * 1024 * 1024,
            ),
        }
        try:
            return presets[name]
        except KeyError:
            msg = f"unknown budget preset {name!r}; expected one of {sorted(presets)}"
            raise ValueError(msg) from None

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_search_queries": self.max_search_queries,
            "max_pages": self.max_pages,
            "max_llm_calls": self.max_llm_calls,
            "max_wall_seconds": self.max_wall_seconds,
            "max_bytes_downloaded": self.max_bytes_downloaded,
        }


@dataclass(slots=True)
class Usage:
    """Counters incremented as work is performed, checked against :class:`Budget`.

    Mutable by design: it is a running accumulator, not a value object. Every
    other model in this module is frozen; this one is the exception.
    """

    started_at: datetime = field(default_factory=utcnow)
    search_queries: int = 0
    pages_fetched: int = 0
    pages_from_cache: int = 0
    bytes_downloaded: int = 0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    robots_denials: int = 0
    blocked_urls: int = 0
    fetch_errors: int = 0

    @property
    def elapsed(self) -> float:
        return max(0.0, (utcnow() - self.started_at).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": round(self.elapsed, 3),
            "search_queries": self.search_queries,
            "pages_fetched": self.pages_fetched,
            "pages_from_cache": self.pages_from_cache,
            "bytes_downloaded": self.bytes_downloaded,
            "llm_calls": self.llm_calls,
            "llm_input_tokens": self.llm_input_tokens,
            "llm_output_tokens": self.llm_output_tokens,
            "robots_denials": self.robots_denials,
            "blocked_urls": self.blocked_urls,
            "fetch_errors": self.fetch_errors,
        }


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One search result, before the URL has been fetched."""

    url: str
    title: str
    snippet: str
    provider: str
    rank: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", self.url.strip())
        object.__setattr__(self, "title", _WS.sub(" ", self.title).strip())
        object.__setattr__(self, "snippet", _WS.sub(" ", self.snippet).strip())

    @property
    def host(self) -> str:
        return (urlparse(self.url).hostname or "").lower()

    @property
    def dedupe_key(self) -> str:
        """Stable identity that ignores scheme, ``www.`` and trailing slashes."""
        parsed = urlparse(self.url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/")
        return _sha(host, path)


@dataclass(frozen=True, slots=True)
class Source:
    """A page that was fetched and reduced to readable text."""

    url: str
    final_url: str
    text: str
    title: str = ""
    status: int = 200
    fetched_at: datetime = field(default_factory=utcnow)
    elapsed_ms: int = 0
    from_cache: bool = False
    content_type: str = "text/html"
    links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _WS.sub(" ", self.text).strip())
        object.__setattr__(self, "title", _WS.sub(" ", self.title).strip())

    @property
    def host(self) -> str:
        return (urlparse(self.final_url).hostname or "").lower()

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def excerpt(self, limit: int = 280) -> str:
        text = self.text[:limit]
        return text + "…" if len(self.text) > limit else text

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.final_url,
            "title": self.title,
            "status": self.status,
            "word_count": self.word_count,
            "from_cache": self.from_cache,
            "fetched_at": self.fetched_at.isoformat(),
            "excerpt": self.excerpt(),
        }


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Raw HTTP outcome before HTML reduction."""

    url: str
    status: int
    content_type: str
    body: bytes
    final_url: str
    elapsed_ms: int
    from_cache: bool = False
    etag: str | None = None
    last_modified: str | None = None
    #: Raw ``Location`` target, present only on a 3xx. Carried explicitly so
    #: callers do not need the whole header bag just to follow a redirect.
    location: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def is_html(self) -> bool:
        mime = self.content_type.split(";", 1)[0].strip().lower()
        return mime in {"text/html", "application/xhtml+xml", "text/plain", "application/xml"}


@dataclass(frozen=True, slots=True)
class Evidence:
    """A verbatim quote proving a value, anchored to its source and offset."""

    source_url: str
    quote: str
    source_title: str = ""
    char_start: int = 0
    char_end: int = 0

    def __post_init__(self) -> None:
        # A blank quote is not weak evidence, it is no evidence. The whole
        # reporting model promises a verbatim snippet behind every value, so
        # refuse to construct one without a snippet rather than emitting a
        # citation with nothing in it.
        if not self.quote.strip():
            msg = "evidence requires a non-empty verbatim quote"
            raise ValueError(msg)
        if not self.source_url.strip():
            msg = "evidence requires a source_url"
            raise ValueError(msg)
        if self.char_start < 0 or self.char_end < self.char_start:
            msg = f"invalid evidence offsets {self.char_start}:{self.char_end}"
            raise ValueError(msg)

    @property
    def key(self) -> str:
        return _sha(self.source_url, _WS.sub(" ", self.quote).lower())

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "source_title": self.source_title,
            "quote": self.quote,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


@dataclass(frozen=True, slots=True)
class FieldCandidate:
    """A single proposed value for one spec field, from one page."""

    field: str
    value: str
    confidence: float
    evidence: Evidence
    extractor: str = "llm"

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _WS.sub(" ", self.value).strip())
        clamped = min(1.0, max(0.0, self.confidence))
        object.__setattr__(self, "confidence", clamped)

    @property
    def normalized(self) -> str:
        """Case- and punctuation-insensitive form used for agreement counting."""
        return _WS.sub(" ", re.sub(r"[^\w\s]", "", self.value)).strip().lower()

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "extractor": self.extractor,
            "evidence": [self.evidence.as_dict()],
        }


@dataclass(frozen=True, slots=True)
class Conflict:
    """A minority value that lost to the winning one, retained for transparency."""

    value: str
    support: int
    source_urls: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "support": self.support,
            "source_urls": list(self.source_urls),
        }


class Confidence(StrEnum):
    """Bucketed confidence so humans can scan a report quickly."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"

    @classmethod
    def from_score(cls, score: float) -> Confidence:
        if score >= 0.75:
            return cls.HIGH
        if score >= 0.45:
            return cls.MEDIUM
        if score > 0.0:
            return cls.LOW
        return cls.UNKNOWN


@dataclass(frozen=True, slots=True)
class Claim:
    """The reconciled answer for one field, with its supporting and dissenting evidence."""

    field: str
    value: str
    confidence: float
    support: int
    independent_domains: tuple[str, ...]
    evidence: tuple[Evidence, ...] = ()
    conflicts: tuple[Conflict, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _WS.sub(" ", self.value).strip())
        object.__setattr__(self, "confidence", min(1.0, max(0.0, self.confidence)))

    @property
    def label(self) -> Confidence:
        return Confidence.from_score(self.confidence)

    @property
    def corroborated(self) -> bool:
        """True when two or more distinct domains agree, the real signal of trust."""
        return len(self.independent_domains) >= 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "confidence_label": str(self.label),
            "support": self.support,
            "corroborated": self.corroborated,
            "independent_domains": list(self.independent_domains),
            "evidence": [e.as_dict() for e in self.evidence],
            "conflicts": [c.as_dict() for c in self.conflicts],
        }


@dataclass(frozen=True, slots=True)
class ResearchReport:
    """The full result of one agent run."""

    subject: str
    claims: tuple[Claim, ...]
    sources: tuple[Source, ...]
    usage: Usage
    budget: Budget
    queries: tuple[str, ...] = ()
    unresolved_fields: tuple[str, ...] = ()
    started_at: datetime = field(default_factory=utcnow)
    duration_seconds: float = 0.0

    @property
    def answered(self) -> int:
        return sum(1 for c in self.claims if c.value)

    @property
    def coverage(self) -> float:
        if not self.claims:
            return 0.0
        return round(self.answered / len(self.claims), 4)

    def claim(self, name: str) -> Claim | None:
        for c in self.claims:
            if c.field == name:
                return c
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "started_at": self.started_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 3),
            "queries": list(self.queries),
            "coverage": self.coverage,
            "claims": [c.as_dict() for c in self.claims],
            "unresolved_fields": list(self.unresolved_fields),
            "sources": [s.as_dict() for s in self.sources],
            "usage": self.usage.as_dict(),
            "budget": self.budget.as_dict(),
        }

    def to_json(self, *, indent: int = 2) -> str:
        import json

        return json.dumps(self.as_dict(), indent=indent, ensure_ascii=False, default=str)

    def with_sources(self, sources: Iterable[Source]) -> ResearchReport:
        return replace(self, sources=tuple(sources))


def wilson_lower_bound(support: int, agreements: int) -> float:
    """Wilson score lower bound for a binomial proportion.

    Used instead of a raw ratio so a single observation cannot masquerade as
    certainty. Returns a value in ``[0, 1]``.
    """
    if agreements <= 0:
        return 0.0
    n = max(1, support)
    z = 1.96
    phat = agreements / n
    denominator = 1 + z**2 / n
    centre = phat + z**2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z**2 / (4 * n)) / n)
    return max(0.0, (centre - margin) / denominator)


def dedupe_hits(hits: Sequence[SearchHit]) -> list[SearchHit]:
    """Drop duplicate URLs, keeping the highest-ranked occurrence of each."""
    best: dict[str, SearchHit] = {}
    for hit in hits:
        existing = best.get(hit.dedupe_key)
        if existing is None or hit.rank < existing.rank:
            best[hit.dedupe_key] = hit
    return sorted(best.values(), key=lambda h: (h.rank, h.url))
