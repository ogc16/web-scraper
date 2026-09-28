"""Reconcile per-page candidates into one claim per field.

This is where the agent earns the word *grounded*. Every value carries a quote
and a URL, values that only differ in case or punctuation count as agreement,
agreement across *distinct domains* counts for much more than three pages of the
same site, and minority values are kept as explicit conflicts rather than
silently dropped.

Confidence is a Wilson lower bound on the agreement rate, which means a single
observation can never score high no matter how confident the extractor was.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from ..models import (
    Claim,
    Conflict,
    Evidence,
    FieldCandidate,
    ResearchReport,
    Source,
    Usage,
    wilson_lower_bound,
)
from .spec import ResearchSpec

__all__ = ["Reconciler", "reconcile"]

_MIN_VALUE_LEN = 2
_MAX_EVIDENCE_PER_FIELD = 6


@dataclass(frozen=True, slots=True)
class _Group:
    value: str
    candidates: list[FieldCandidate]

    @property
    def support(self) -> int:
        return len(self.candidates)

    @property
    def domains(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for candidate in self.candidates:
            host = _host(candidate.evidence.source_url)
            if host:
                seen.setdefault(host, None)
        return tuple(seen)

    @property
    def best_confidence(self) -> float:
        return max(c.confidence for c in self.candidates)


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def reconcile(
    spec: ResearchSpec,
    candidates: Sequence[FieldCandidate],
) -> tuple[tuple[Claim, ...], tuple[str, ...]]:
    """Reduce candidates to one :class:`Claim` per spec field.

    Args:
        spec: The research request; fields with no candidate still appear in
            the output with an empty value so coverage is honest.
        candidates: Every proposed value gathered this run.

    Returns:
        ``(claims, unresolved_fields)``. A field is unresolved when no
        candidate survived validation.
    """
    by_field: dict[str, list[FieldCandidate]] = defaultdict(list)
    for candidate in candidates:
        value = candidate.value.strip()
        if len(value) < _MIN_VALUE_LEN:
            continue
        by_field[candidate.field].append(candidate)

    claims: list[Claim] = []
    unresolved: list[str] = []

    for field_spec in spec.fields:
        pool = by_field.get(field_spec.name, [])
        if not pool:
            claims.append(_empty_claim(field_spec.name))
            unresolved.append(field_spec.name)
            continue

        groups = _group_candidates(pool)
        if not groups:
            claims.append(_empty_claim(field_spec.name))
            unresolved.append(field_spec.name)
            continue

        groups.sort(key=lambda g: (g.support, len(g.domains), g.best_confidence), reverse=True)
        winner = groups[0]
        runner_up = groups[1] if len(groups) > 1 else None

        support = winner.support
        independent = len(winner.domains)
        agreement = 1.0 if runner_up is None else support / (support + runner_up.support)
        score = wilson_lower_bound(support, round(agreement * support))

        domain_factor = min(1.0, independent / max(1, spec.min_independent_sources))
        extractor_quality = max(c.confidence for c in winner.candidates)
        score = min(1.0, score * 0.65 + domain_factor * 0.25 + extractor_quality * 0.10)

        if independent < spec.min_independent_sources and field_spec.required:
            score = min(score, 0.72)

        evidence = _collect_evidence(winner)
        conflicts = tuple(
            Conflict(
                value=group.value,
                support=group.support,
                source_urls=tuple(c.evidence.source_url for c in group.candidates),
            )
            for group in groups[1:]
            if group.support >= 1
        )

        claims.append(
            Claim(
                field=field_spec.name,
                value=winner.value,
                confidence=score,
                support=support,
                independent_domains=winner.domains,
                evidence=evidence,
                conflicts=conflicts,
            )
        )

    return tuple(claims), tuple(unresolved)


def _empty_claim(name: str) -> Claim:
    return Claim(
        field=name,
        value="",
        confidence=0.0,
        support=0,
        independent_domains=(),
    )


def _group_candidates(pool: list[FieldCandidate]) -> list[_Group]:
    buckets: dict[str, list[FieldCandidate]] = defaultdict(list)
    display: dict[str, str] = {}
    for candidate in pool:
        key = candidate.normalized
        if not key:
            continue
        buckets[key].append(candidate)
        current = display.get(key)
        if current is None or len(candidate.value) > len(current):
            display[key] = candidate.value
    return [_Group(value=display[key], candidates=items) for key, items in buckets.items()]


def _collect_evidence(winner: _Group) -> tuple[Evidence, ...]:
    """Pick the most diverse evidence: one per domain first, then by confidence."""
    by_domain: dict[str, FieldCandidate] = {}
    overflow: list[FieldCandidate] = []
    for candidate in sorted(winner.candidates, key=lambda c: -c.confidence):
        host = _host(candidate.evidence.source_url)
        if host and host not in by_domain:
            by_domain[host] = candidate
        else:
            overflow.append(candidate)

    ordered = list(by_domain.values()) + overflow
    return tuple(
        Evidence(
            source_url=c.evidence.source_url,
            quote=c.evidence.quote,
            source_title=c.evidence.source_title,
            char_start=c.evidence.char_start,
            char_end=c.evidence.char_end,
        )
        for c in ordered[:_MAX_EVIDENCE_PER_FIELD]
    )


class Reconciler:
    """Object wrapper around :func:`reconcile` for dependency injection in tests."""

    def __init__(self, spec: ResearchSpec) -> None:
        self.spec = spec

    def __call__(
        self,
        candidates: Sequence[FieldCandidate],
        *,
        sources: Sequence[Source] = (),
        usage: Usage | None = None,
    ) -> ResearchReport:
        claims, unresolved = reconcile(self.spec, candidates)
        return ResearchReport(
            subject=self.spec.subject,
            claims=claims,
            sources=tuple(sources),
            usage=usage or Usage(),
            budget=self.spec.budget,
            unresolved_fields=unresolved,
        )
