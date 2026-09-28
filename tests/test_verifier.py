"""Evidence reconciliation: grouping, conflicts and confidence scoring.

These tests pin the promises the README makes about grounded output:

* every claim carries a quote and a URL,
* values differing only in case or punctuation count as agreement,
* corroboration across *distinct domains* outranks repetition on one site,
* minority values survive as explicit conflicts instead of vanishing,
* one observation can never score high confidence.
"""

from __future__ import annotations

from collections.abc import Sequence

from awsa.agent.spec import ResearchSpec
from awsa.agent.verifier import Reconciler, reconcile
from awsa.models import Claim, Evidence, FieldCandidate, ResearchReport, Source, Usage


def _evidence(url: str, quote: str = "the quote") -> Evidence:
    return Evidence(source_url=url, quote=quote, source_title="Title", char_start=0, char_end=9)


def _candidate(
    field: str,
    value: str,
    url: str,
    *,
    confidence: float = 0.8,
) -> FieldCandidate:
    return FieldCandidate(
        field=field,
        value=value,
        confidence=confidence,
        evidence=_evidence(url, f"quote for {value}"),
    )


def _spec(*fields: str, **kwargs: object) -> ResearchSpec:
    return ResearchSpec.build("Ada Lovelace", list(fields), **kwargs)


def _by_field(claims: Sequence[Claim]) -> dict[str, Claim]:
    return {claim.field: claim for claim in claims}


class TestGrouping:
    def test_case_and_punctuation_insensitive_values_agree(self) -> None:
        spec = _spec("city")
        claims, unresolved = reconcile(
            spec,
            [
                _candidate("city", "London", "https://a.test/x"),
                _candidate("city", "london.", "https://b.test/y"),
                _candidate("city", "  LONDON  ", "https://c.test/z"),
            ],
        )
        assert unresolved == ()
        claim = claims[0]
        assert claim.support == 3
        assert claim.conflicts == ()
        # Display keeps the longest surface form of the agreeing group.
        assert claim.value == "london."

    def test_genuinely_different_values_form_separate_groups(self) -> None:
        spec = _spec("city")
        claims, _ = reconcile(
            spec,
            [
                _candidate("city", "London", "https://a.test/x"),
                _candidate("city", "Paris", "https://b.test/y"),
            ],
        )
        claim = claims[0]
        assert claim.support == 1
        assert len(claim.conflicts) == 1
        assert claim.conflicts[0].value in {"London", "Paris"}

    def test_under_length_candidates_are_dropped(self) -> None:
        spec = _spec("city")
        claims, unresolved = reconcile(spec, [_candidate("city", "x", "https://a.test/x")])
        assert unresolved == ("city",)
        assert claims[0].value == ""
        assert claims[0].confidence == 0.0


class TestConflicts:
    def test_minority_value_is_kept_not_discarded(self) -> None:
        spec = _spec("city")
        claims, _ = reconcile(
            spec,
            [
                _candidate("city", "London", "https://a.test/1"),
                _candidate("city", "London", "https://a.test/2"),
                _candidate("city", "Paris", "https://b.test/1"),
            ],
        )
        conflict = claims[0].conflicts[0]
        assert conflict.value == "Paris"
        assert conflict.support == 1
        assert conflict.source_urls == ("https://b.test/1",)

    def test_equal_support_is_broken_by_independent_domains(self) -> None:
        spec = _spec("city")
        claims, _ = reconcile(
            spec,
            [
                # London: two pages, but one site.
                _candidate("city", "London", "https://a.test/1"),
                _candidate("city", "London", "https://a.test/2"),
                # Paris: two pages, two sites.
                _candidate("city", "Paris", "https://b.test/1"),
                _candidate("city", "Paris", "https://c.test/1"),
            ],
        )
        # Equal support, so the tie-break on distinct domains decides: two
        # independent sites beat one site seen twice.
        claim = claims[0]
        assert claim.value == "Paris"
        assert claim.independent_domains == ("b.test", "c.test")
        conflict = claim.conflicts[0]
        assert conflict.value == "London"
        assert conflict.support == 2
        assert set(conflict.source_urls) == {"https://a.test/1", "https://a.test/2"}


class TestConfidence:
    def test_single_observation_cannot_score_high(self) -> None:
        # Even with a maximally confident extractor, one source is one source.
        spec = _spec("city")
        claims, _ = reconcile(
            spec,
            [_candidate("city", "London", "https://a.test/x", confidence=1.0)],
        )
        assert claims[0].confidence < 0.75

    def test_more_support_raises_confidence(self) -> None:
        spec = _spec("city")
        one, _ = reconcile(spec, [_candidate("city", "London", "https://a.test/1")])
        three, _ = reconcile(
            spec,
            [
                _candidate("city", "London", "https://a.test/1"),
                _candidate("city", "London", "https://b.test/2"),
                _candidate("city", "London", "https://c.test/3"),
            ],
        )
        assert three[0].confidence > one[0].confidence

    def test_distinct_domains_beat_repetition_on_one_site(self) -> None:
        spec = _spec("city")
        # Three pages, one domain: repetition should not masquerade as corroboration.
        repeated, _ = reconcile(
            spec,
            [
                _candidate("city", "London", "https://same.test/1"),
                _candidate("city", "London", "https://same.test/2"),
                _candidate("city", "London", "https://same.test/3"),
            ],
        )
        # Two pages, two domains: genuine independence.
        independent, _ = reconcile(
            spec,
            [
                _candidate("city", "London", "https://one.test/1"),
                _candidate("city", "London", "https://two.test/2"),
            ],
        )
        assert independent[0].independent_domains == ("one.test", "two.test")
        assert len(repeated[0].independent_domains) == 1
        assert independent[0].confidence > repeated[0].confidence

    def test_www_prefix_is_not_a_distinct_domain(self) -> None:
        spec = _spec("city")
        claims, _ = reconcile(
            spec,
            [
                _candidate("city", "London", "https://example.test/1"),
                _candidate("city", "London", "https://www.example.test/2"),
            ],
        )
        assert claims[0].independent_domains == ("example.test",)

    def test_agreement_lowers_confidence_when_a_rival_value_exists(self) -> None:
        spec = _spec("city")
        unanimous, _ = reconcile(
            spec,
            [
                _candidate("city", "London", "https://a.test/1"),
                _candidate("city", "London", "https://b.test/2"),
            ],
        )
        contested, _ = reconcile(
            spec,
            [
                _candidate("city", "London", "https://a.test/1"),
                _candidate("city", "London", "https://b.test/2"),
                _candidate("city", "Paris", "https://c.test/3"),
            ],
        )
        assert contested[0].confidence < unanimous[0].confidence

    def test_confidence_is_capped_when_sources_are_too_few(self) -> None:
        # spec requires 3 independent sources; we supply one.
        spec = _spec("city", min_independent_sources=3)
        claims, _ = reconcile(spec, [_candidate("city", "London", "https://a.test/1")])
        assert claims[0].independent_domains == ("a.test",)
        assert claims[0].confidence <= 0.72

    def test_confidence_never_exceeds_one(self) -> None:
        spec = _spec("city")
        claims, _ = reconcile(
            spec,
            [
                _candidate("city", "London", f"https://d{i}.test/{i}", confidence=1.0)
                for i in range(8)
            ],
        )
        assert 0.0 <= claims[0].confidence <= 1.0

    def test_optional_field_is_not_capped_by_source_count(self) -> None:
        # The 0.72 cap is a "required field, thin evidence" signal. An optional
        # field with one source should not be treated as a warning.
        spec = ResearchSpec.build("Ada", ["city:where she lived:optional"])
        spec = ResearchSpec(
            subject=spec.subject,
            fields=(type(spec.fields[0])(name="city", required=False),),
            min_independent_sources=3,
        )
        claims, _ = reconcile(spec, [_candidate("city", "London", "https://a.test/1")])
        assert claims[0].confidence > 0.0


class TestEvidence:
    def test_every_claim_carries_quote_and_url(self) -> None:
        spec = _spec("city")
        claims, _ = reconcile(spec, [_candidate("city", "London", "https://a.test/x")])
        evidence = claims[0].evidence
        assert len(evidence) == 1
        assert evidence[0].source_url == "https://a.test/x"
        assert evidence[0].quote

    def test_evidence_takes_the_best_quote_per_domain_first(self) -> None:
        spec = _spec("city")
        claims, _ = reconcile(
            spec,
            [
                _candidate("city", "London", "https://a.test/1", confidence=0.5),
                _candidate("city", "London", "https://a.test/2", confidence=0.9),
                _candidate("city", "London", "https://b.test/3", confidence=0.7),
            ],
        )
        urls = [e.source_url for e in claims[0].evidence]
        # Diversity first: the leading entries are the strongest quote from each
        # distinct domain, so a single chatty site cannot crowd out a second
        # source. The redundant a.test/1 is still included, after them.
        assert urls[:2] == ["https://a.test/2", "https://b.test/3"]
        assert set(urls) == {"https://a.test/1", "https://a.test/2", "https://b.test/3"}

    def test_evidence_is_capped(self) -> None:
        spec = _spec("city")
        claims, _ = reconcile(
            spec,
            [_candidate("city", "London", f"https://d{i}.test/{i}") for i in range(12)],
        )
        assert len(claims[0].evidence) <= 6


class TestCoverage:
    def test_fields_with_no_candidates_still_appear(self) -> None:
        spec = _spec("city", "employer")
        claims, unresolved = reconcile(spec, [_candidate("city", "London", "https://a.test/1")])
        # An unanswered field must be visible as unanswered, not omitted.
        assert [c.field for c in claims] == ["city", "employer"]
        assert unresolved == ("employer",)

    def test_candidates_for_unknown_fields_are_ignored(self) -> None:
        spec = _spec("city")
        claims, _ = reconcile(
            spec,
            [
                _candidate("city", "London", "https://a.test/1"),
                _candidate("nickname", "Countess", "https://a.test/2"),
            ],
        )
        assert [c.field for c in claims] == ["city"]

    def test_every_field_reports_resolved_or_unresolved(self) -> None:
        spec = _spec("city", "employer", "country")
        _, unresolved = reconcile(spec, [_candidate("employer", "Analytical Engines", "https://a")])
        assert set(unresolved) == {"city", "country"}


class TestReconciler:
    def test_wraps_claims_into_a_report(self) -> None:
        spec = _spec("city", "employer")
        report = Reconciler(spec)(
            [_candidate("city", "London", "https://a.test/1")],
            sources=[
                Source(
                    url="https://a.test/1",
                    final_url="https://a.test/1",
                    text="body",
                    title="A",
                )
            ],
            usage=Usage(search_queries=1, pages_fetched=1),
        )
        assert isinstance(report, ResearchReport)
        assert report.subject == "Ada Lovelace"
        assert report.usage.pages_fetched == 1
        assert report.usage.search_queries == 1
        assert [s.url for s in report.sources] == ["https://a.test/1"]
        assert report.unresolved_fields == ("employer",)
        assert [c.field for c in report.claims] == ["city", "employer"]

    def test_defaults_usage_when_omitted(self) -> None:
        spec = _spec("city")
        report = Reconciler(spec)([_candidate("city", "London", "https://a.test/1")])
        # Usage stamps started_at per instance, so compare the counters only.
        assert report.usage.search_queries == 0
        assert report.usage.pages_fetched == 0
        assert report.usage.bytes_downloaded == 0
        assert report.sources == ()
