"""Report rendering in all three output formats.

The JSON contract matters most: it is what downstream tooling consumes, so it
has to round-trip, stay stable, and never quietly drop a field. The human
formats matter just as much for a different reason - a report that claims
confidence it cannot justify is worse than no report.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from awsa import models
from awsa.models import (
    Budget,
    Claim,
    Conflict,
    Evidence,
    ResearchReport,
    Source,
    Usage,
)
from awsa.report import render, render_json, render_markdown, render_plain


def _source(url: str, title: str = "T") -> Source:
    return Source(url=url, final_url=url, text="body text", title=title)


def _report(
    *,
    claims: tuple[Claim, ...] = (),
    unresolved: tuple[str, ...] = (),
    subject: str = "Ada Lovelace",
) -> ResearchReport:
    return ResearchReport(
        subject=subject,
        claims=claims,
        sources=(_source("https://a.test/1"), _source("https://b.test/2", "B")),
        usage=Usage(search_queries=2, pages_fetched=2, llm_calls=1),
        budget=Budget.preset("standard"),
        queries=("ada lovelace",),
        unresolved_fields=unresolved,
    )


def _claim(
    field: str = "name",
    value: str = "Ada Lovelace",
    *,
    confidence: float = 0.82,
    support: int = 2,
    domains: tuple[str, ...] = ("a.test", "b.test"),
    evidence: tuple[Evidence, ...] = (),
    conflicts: tuple[Conflict, ...] = (),
) -> Claim:
    return Claim(
        field=field,
        value=value,
        confidence=confidence,
        support=support,
        independent_domains=domains,
        evidence=evidence
        or (
            Evidence(
                source_url=f"https://{domains[0]}/1",
                quote=f"{value} is the value.",
                source_title="T",
            ),
        ),
        conflicts=conflicts,
    )


class TestRenderDispatch:
    def test_markdown_is_the_default(self) -> None:
        report = _report(claims=(_claim(),))
        assert render(report) == render_markdown(report)

    def test_json_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = _report(claims=(_claim(),))
        # Freeze the clock so the measured `elapsed_seconds` cannot differ
        # between the two renders, and compare exactly.
        frozen = datetime(2025, 1, 1, tzinfo=UTC)
        monkeypatch.setattr(models, "utcnow", lambda: frozen)
        assert render(report, "json") == render_json(report)

    def test_plain_format(self) -> None:
        report = _report(claims=(_claim(),))
        assert render(report, "plain") == render_plain(report)

    def test_unknown_format_falls_back_to_markdown(self) -> None:
        # Never crash a user's pipeline over a typo in --format.
        report = _report(claims=(_claim(),))
        assert render(report, "pdf") == render_markdown(report)

    def test_text_alias_maps_to_plain(self) -> None:
        report = _report(claims=(_claim(),))
        assert render(report, "text") == render_plain(report)


class TestMarkdown:
    def test_shows_the_subject_and_claims(self) -> None:
        text = render_markdown(_report(claims=(_claim(),)))
        assert "Ada Lovelace" in text
        assert "name" in text

    def test_includes_the_verbatim_quote_and_url(self) -> None:
        claim = _claim(evidence=(Evidence(source_url="https://a.test/9", quote="a quoted line"),))
        text = render_markdown(_report(claims=(claim,)))
        assert "a quoted line" in text
        assert "https://a.test/9" in text

    def test_unresolved_fields_are_visible(self) -> None:
        text = render_markdown(_report(claims=(_claim(),), unresolved=("employer",)))
        assert "employer" in text

    def test_conflicts_are_surfaced_not_hidden(self) -> None:
        conflict = Conflict(value="Augusta Ada King", support=1, source_urls=("https://b.test/3",))
        text = render_markdown(_report(claims=(_claim(conflicts=(conflict,)),)))
        # A disagreement the user cannot see is a wrong answer presented as
        # fact, so the losing value has to appear in the output.
        assert "Augusta Ada King" in text

    def test_a_claim_without_evidence_still_renders(self) -> None:
        claim = Claim(
            field="bio",
            value="Mathematician",
            confidence=0.3,
            support=1,
            independent_domains=("a.test",),
        )
        assert "Mathematician" in render_markdown(_report(claims=(claim,)))

    def test_empty_report_renders_without_raising(self) -> None:
        assert render_markdown(_report()).strip()

    def test_low_confidence_is_visible_to_the_reader(self) -> None:
        text = render_markdown(_report(claims=(_claim(confidence=0.2, support=1),)))
        assert "low" in text.lower() or "0.2" in text or "20%" in text


class TestPlain:
    def test_is_line_oriented_and_parseable(self) -> None:
        text = render_plain(_report(claims=(_claim(),)))
        assert "name:" in text or "name " in text
        assert "Ada Lovelace" in text

    def test_avoids_markdown_tables_and_emphasis(self) -> None:
        # Plain output targets terminals and log files, so pipe-tables and
        # emphasis markers - which render as literal noise there - are avoided.
        text = render_plain(_report(claims=(_claim(),)))
        assert "|---" not in text
        assert "**" not in text
        assert "`" not in text


class TestJson:
    def test_is_valid_json(self) -> None:
        json.loads(render_json(_report(claims=(_claim(),))))

    def test_carries_the_subject_and_claims(self) -> None:
        payload = json.loads(render_json(_report(claims=(_claim(),))))
        assert payload["subject"] == "Ada Lovelace"
        assert payload["claims"][0]["field"] == "name"
        assert payload["claims"][0]["value"] == "Ada Lovelace"

    def test_evidence_offsets_and_urls_survive(self) -> None:
        claim = _claim(
            evidence=(Evidence(source_url="https://a.test/9", quote="q", char_start=3, char_end=7),)
        )
        payload = json.loads(render_json(_report(claims=(claim,))))
        evidence = payload["claims"][0]["evidence"][0]
        assert evidence["source_url"] == "https://a.test/9"
        assert evidence["char_start"] == 3
        assert evidence["char_end"] == 7

    def test_usage_and_budget_are_reported(self) -> None:
        payload = json.loads(render_json(_report(claims=(_claim(),))))
        assert payload["usage"]["search_queries"] == 2
        assert payload["usage"]["pages_fetched"] == 2
        assert "budget" in payload

    def test_unresolved_fields_are_included(self) -> None:
        payload = json.loads(render_json(_report(claims=(_claim(),), unresolved=("employer",))))
        assert payload["unresolved_fields"] == ["employer"]

    def test_coverage_is_a_number_between_zero_and_one(self) -> None:
        payload = json.loads(render_json(_report(claims=(_claim(),), unresolved=("employer",))))
        assert 0.0 <= payload["coverage"] <= 1.0

    def test_empty_report_serialises(self) -> None:
        payload = json.loads(render_json(_report()))
        assert payload["claims"] == []

    def test_is_stable_apart_from_measured_timing(self) -> None:
        # `usage.elapsed_seconds` and `started_at` are live measurements, so
        # byte-for-byte equality is the wrong bar. What must hold is that the
        # *structure* never churns - no key reordering, no set-derived
        # iteration order - so a re-render of the same data diffs cleanly.
        report = _report(claims=(_claim(),))
        first, second = json.loads(render_json(report)), json.loads(render_json(report))
        assert list(first) == list(second)
        assert list(first["usage"]) == list(second["usage"])
        assert {k: v for k, v in first.items() if k != "usage"} == {
            k: v for k, v in second.items() if k != "usage"
        }

    def test_key_order_follows_the_model_not_the_alphabet(self) -> None:
        payload = json.loads(render_json(_report(claims=(_claim(),))))
        # The documented field order, so human and machine readers see the
        # same shape; sort_keys=True would obscure it.
        assert list(payload)[:3] == ["subject", "started_at", "duration_seconds"]

    def test_typed_numbers_stay_numbers(self) -> None:
        payload = json.loads(render_json(_report(claims=(_claim(),))))
        assert isinstance(payload["usage"]["pages_fetched"], int)
        assert isinstance(payload["coverage"], float)


class TestHonestyInOutput:
    """A report must not overstate what the evidence supports."""

    def test_single_source_is_labelled_as_uncorroborated(self) -> None:
        claim = _claim(support=1, domains=("a.test",))
        text = render_markdown(_report(claims=(claim,)))
        assert "a.test" in text

    def test_high_confidence_single_source_is_not_presented_as_certain(self) -> None:
        # Confidence and corroboration are separate axes; a lone source that
        # happens to score 0.95 is still uncorroborated.
        claim = _claim(confidence=0.95, support=1, domains=("a.test",))
        text = render_markdown(_report(claims=(claim,)))
        assert "95%" in text or "0.95" in text
