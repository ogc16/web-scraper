"""Provider contracts.

Three seams make the agent replaceable without touching agent logic:

* :class:`SearchProvider` — turns a query into candidate URLs.
* :class:`PageProvider` — turns a URL into readable text (or delegates to the
  default polite HTTP fetcher).
* :class:`LLMProvider` — plans queries and extracts fields.

The LLM contract is deliberately *task-shaped* rather than raw chat
completion. Each implementation decides for itself how to get from "here is a
page, find these fields" to structured output, and the agent never needs to know
whether that was a hosted model or a regex.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import SearchHit

__all__ = [
    "ExtractRequest",
    "ExtractedField",
    "LLMProvider",
    "LLMUsage",
    "PageProvider",
    "PlanRequest",
    "SearchProvider",
]


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Input to query planning."""

    subject: str
    fields: tuple[str, ...]
    field_hints: tuple[str, ...] = ()
    existing_queries: tuple[str, ...] = ()
    uncovered_fields: tuple[str, ...] = ()
    max_queries: int = 4
    language: str = "en"

    def as_prompt_context(self) -> str:
        lines = [f"Subject: {self.subject}"]
        if self.fields:
            lines.append("Fields to fill: " + ", ".join(self.fields))
        for index, hint in enumerate(self.field_hints):
            if index < len(self.fields):
                lines.append(f"  - {self.fields[index]}: {hint}")
        if self.uncovered_fields:
            lines.append("Still missing: " + ", ".join(self.uncovered_fields))
        if self.existing_queries:
            lines.append("Already tried: " + " | ".join(self.existing_queries))
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ExtractRequest:
    """Input to field extraction for a single page."""

    subject: str
    page_title: str
    page_url: str
    page_text: str
    fields: tuple[str, ...]
    field_hints: tuple[str, ...] = ()
    max_snippet_chars: int = 400

    def truncated(self, limit: int) -> ExtractRequest:
        if len(self.page_text) <= limit:
            return self
        clipped = self.page_text[:limit].rsplit(" ", 1)[0]
        return ExtractRequest(
            subject=self.subject,
            page_title=self.page_title,
            page_url=self.page_url,
            page_text=clipped,
            fields=self.fields,
            field_hints=self.field_hints,
            max_snippet_chars=self.max_snippet_chars,
        )


@dataclass(frozen=True, slots=True)
class ExtractedField:
    """One proposed value plus the verbatim quote that supports it."""

    field: str
    value: str
    quote: str
    confidence: float = 0.5
    char_start: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "value": self.value,
            "quote": self.quote,
            "confidence": round(self.confidence, 3),
            "char_start": self.char_start,
        }


@dataclass(slots=True)
class LLMUsage:
    """Token accounting for one provider, reported in the run summary."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failures: int = 0
    estimated_cost_usd: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "failures": self.failures,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }


@runtime_checkable
class SearchProvider(Protocol):
    """Finds candidate URLs for a query."""

    name: str
    requires_key: bool

    async def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        """Return ranked results. Raise :class:`ProviderError` on failure."""
        ...


@runtime_checkable
class PageProvider(Protocol):
    """Retrieves readable text for a URL."""

    name: str
    requires_key: bool

    async def get_text(self, url: str) -> str:
        """Return readable plain text for ``url``. Raise on failure."""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    """Plans queries and extracts structured fields from page text."""

    name: str
    requires_key: bool

    async def plan_queries(self, request: PlanRequest) -> Sequence[str]:
        """Propose up to ``request.max_queries`` distinct search queries."""
        ...

    async def extract_fields(self, request: ExtractRequest) -> Sequence[ExtractedField]:
        """Propose values for ``request.fields`` grounded in ``request.page_text``."""
        ...

    def usage(self) -> LLMUsage:
        """Return accumulated token usage for this provider."""
        ...
