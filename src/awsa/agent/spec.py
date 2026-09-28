"""The research request: what to find out, and what is out of bounds.

Kept separate from the loop so a spec is a plain value that can be saved,
diffed, and unit-tested without any I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import urlsplit

from ..errors import ConfigError
from ..models import Budget

__all__ = ["FieldSpec", "ResearchSpec"]

_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One value to find, with an optional hint that sharpens the query."""

    name: str
    hint: str = ""
    required: bool = True

    def __post_init__(self) -> None:
        cleaned = _WS.sub(" ", self.name).strip()
        if not cleaned:
            msg = "field name must not be empty"
            raise ConfigError(msg)
        object.__setattr__(self, "name", cleaned)
        object.__setattr__(self, "hint", _WS.sub(" ", self.hint).strip())

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_")


@dataclass(frozen=True, slots=True)
class ResearchSpec:
    """A complete research request.

    Args:
        subject: Who or what is being researched.
        fields: Values to find.
        budget: Resource ceilings for the run.
        include_domains: If set, only pages on these hosts may be used as evidence.
        exclude_domains: Hosts never used as evidence.
        max_pages_per_domain: Cap that stops one site from dominating a report.
        min_independent_sources: Claims backed by fewer distinct domains than
            this are reported below ``high`` confidence.
    """

    subject: str
    fields: tuple[FieldSpec, ...]
    budget: Budget = field(default_factory=Budget)
    include_domains: frozenset[str] = field(default_factory=frozenset)
    exclude_domains: frozenset[str] = field(default_factory=frozenset)
    max_pages_per_domain: int = 3
    min_independent_sources: int = 2
    language: str = "en"
    notes: str = ""

    def __post_init__(self) -> None:
        cleaned = _WS.sub(" ", self.subject).strip()
        if len(cleaned) < 2:
            msg = f"subject must be at least 2 characters, got {self.subject!r}"
            raise ConfigError(msg)
        if not self.fields:
            msg = "a research spec needs at least one field"
            raise ConfigError(msg)
        if self.max_pages_per_domain < 1:
            msg = "max_pages_per_domain must be >= 1"
            raise ConfigError(msg)
        if self.min_independent_sources < 1:
            msg = "min_independent_sources must be >= 1"
            raise ConfigError(msg)
        overlap = self.include_domains & self.exclude_domains
        if overlap:
            msg = f"domain(s) {sorted(overlap)} are both included and excluded"
            raise ConfigError(msg)
        object.__setattr__(self, "subject", cleaned)
        object.__setattr__(
            self, "include_domains", frozenset(_norm(d) for d in self.include_domains)
        )
        object.__setattr__(
            self, "exclude_domains", frozenset(_norm(d) for d in self.exclude_domains)
        )
        object.__setattr__(self, "language", self.language.strip().lower() or "en")

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    @property
    def field_hints(self) -> tuple[str, ...]:
        return tuple(f.hint for f in self.fields)

    def allows_domain(self, url: str) -> bool:
        """Whether ``url``'s host is admissible as evidence for this spec."""
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        if not host:
            return False
        host = _norm(host)
        if _host_in(host, self.exclude_domains):
            return False
        return not (self.include_domains and not _host_in(host, self.include_domains))

    def domain_of(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        return host.removeprefix("www.") or "unknown"

    def with_budget(self, budget: Budget) -> Self:
        from dataclasses import replace

        return replace(self, budget=budget)

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "fields": [
                {"name": f.name, "hint": f.hint, "required": f.required} for f in self.fields
            ],
            "budget": self.budget.as_dict(),
            "include_domains": sorted(self.include_domains),
            "exclude_domains": sorted(self.exclude_domains),
            "max_pages_per_domain": self.max_pages_per_domain,
            "min_independent_sources": self.min_independent_sources,
            "language": self.language,
            "notes": self.notes,
        }

    @classmethod
    def build(
        cls,
        subject: str,
        fields: Iterable[str | FieldSpec],
        **kwargs: Any,
    ) -> Self:
        """Convenience constructor accepting plain strings or :class:`FieldSpec`.

        ``fields`` is deliberately typed as ``Iterable[str | FieldSpec]`` rather
        than a union of concrete containers: a string is itself iterable, so a
        ``str``/``Sequence[str]`` union invites silently iterating a subject
        name character by character.
        """
        specs: list[FieldSpec] = []
        for item in fields:
            if isinstance(item, FieldSpec):
                specs.append(item)
            elif isinstance(item, str) and ":" in item:
                name, _, hint = item.partition(":")
                specs.append(FieldSpec(name=name, hint=hint))
            elif isinstance(item, str):
                specs.append(FieldSpec(name=item))
            else:
                # Unreachable per the annotation, but real callers are untyped
                # (JSON specs, CLI passthrough) so fail loudly instead of
                # silently dropping the field.
                msg = f"field must be a string or FieldSpec, got {type(item).__name__}"  # type: ignore[unreachable]
                raise ConfigError(msg)
        return cls(subject=subject, fields=tuple(specs), **kwargs)


def _norm(domain: str) -> str:
    return domain.strip().lower().lstrip(".").removeprefix("www.")


def _host_in(host: str, domains: frozenset[str]) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in domains)
