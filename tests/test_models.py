"""Value semantics of the core data model."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from awsa.models import (
    Budget,
    Claim,
    Confidence,
    Evidence,
    SearchHit,
    Source,
    Usage,
    dedupe_hits,
    wilson_lower_bound,
)


class TestConfidence:
    def test_is_str_enum(self) -> None:
        # StrEnum interop with plain strings is the point of the type, so this
        # comparison is deliberate; mypy's literal-overlap check is a false
        # positive for StrEnum members.
        assert Confidence.HIGH == "high"  # type: ignore[comparison-overlap]
        assert str(Confidence.MEDIUM) == "medium"

    def test_from_score_boundaries(self) -> None:
        assert Confidence.from_score(0.9) is Confidence.HIGH
        assert Confidence.from_score(0.75) is Confidence.HIGH
        assert Confidence.from_score(0.74) is Confidence.MEDIUM
        assert Confidence.from_score(0.45) is Confidence.MEDIUM
        assert Confidence.from_score(0.44) is Confidence.LOW
        assert Confidence.from_score(0.0) is Confidence.UNKNOWN


class TestBudget:
    def test_presets_are_ordered(self) -> None:
        tiny, standard, deep = (Budget.preset(n) for n in ("tiny", "standard", "deep"))
        assert tiny.max_pages < standard.max_pages < deep.max_pages
        assert tiny.max_wall_seconds < deep.max_wall_seconds

    def test_none_is_effectively_unbounded(self) -> None:
        assert Budget.preset("none").max_pages >= 10_000

    def test_unknown_preset_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown budget preset"):
            Budget.preset("enormous")

    def test_is_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            Budget().max_pages = 99  # type: ignore[misc]


class TestUsage:
    def test_is_mutable_accumulator(self) -> None:
        usage = Usage()
        usage.search_queries += 1
        assert usage.search_queries == 1

    def test_elapsed_is_non_negative(self) -> None:
        assert Usage().elapsed >= 0.0

    def test_round_trips_through_dict(self) -> None:
        usage = Usage()
        usage.pages_fetched = 3
        assert usage.as_dict()["pages_fetched"] == 3


class TestSource:
    def test_normalises_whitespace_in_title(self) -> None:
        source = Source(
            url="https://example.com/a",
            final_url="https://example.com/a",
            title="A   title\nwith\tspaces",
            text="body",
        )
        assert source.title == "A title with spaces"

    def test_is_hashable(self) -> None:
        source = Source(
            url="https://example.com/a",
            final_url="https://example.com/a",
            title="A",
            text="body",
        )
        assert {source, source} == {source}


class TestEvidence:
    def test_requires_a_quote(self) -> None:
        with pytest.raises(ValueError):
            Evidence(source_url="https://example.com", quote="   ")


class TestSearchHit:
    def _hit(self, url: str, *, title: str = "T", rank: int = 0) -> SearchHit:
        return SearchHit(url=url, title=title, snippet="s", provider="test", rank=rank)

    def test_dedupe_key_ignores_tracking_params(self) -> None:
        a = self._hit("https://example.com/p?utm_source=x", title="A")
        b = self._hit("https://example.com/p", title="B")
        assert a.dedupe_key == b.dedupe_key

    def test_dedupe_key_ignores_fragments(self) -> None:
        a = self._hit("https://example.com/p#section")
        b = self._hit("https://example.com/p")
        assert a.dedupe_key == b.dedupe_key

    def test_dedupe_key_distinguishes_paths(self) -> None:
        assert (
            self._hit("https://example.com/a").dedupe_key
            != self._hit("https://example.com/b").dedupe_key
        )


class TestDedupeHits:
    def test_keeps_the_best_ranked_duplicate(self) -> None:
        hits = [
            SearchHit(
                url="https://example.com/p",
                title="Third",
                snippet="c",
                provider="t",
                rank=3,
            ),
            SearchHit(
                url="https://example.com/p#frag",
                title="First",
                snippet="a",
                provider="t",
                rank=1,
            ),
            SearchHit(
                url="https://example.com/p?utm_source=twitter",
                title="Second",
                snippet="b",
                provider="t",
                rank=2,
            ),
        ]
        out = dedupe_hits(hits)
        assert len(out) == 1
        assert out[0].rank == 1
        assert out[0].title == "First"

    def test_orders_by_rank(self) -> None:
        hits = [
            SearchHit(url="https://a.example/1", title="", snippet="", provider="t", rank=5),
            SearchHit(url="https://b.example/2", title="", snippet="", provider="t", rank=1),
        ]
        assert [h.url for h in dedupe_hits(hits)] == [
            "https://b.example/2",
            "https://a.example/1",
        ]

    def test_empty_input(self) -> None:
        assert dedupe_hits([]) == []


class TestWilsonLowerBound:
    def test_zero_support_is_zero(self) -> None:
        assert wilson_lower_bound(0, 0) == 0.0

    def test_perfect_small_sample_stays_below_one(self) -> None:
        # The entire reason for using a Wilson bound: 1/1 must not read as 100%.
        bound = wilson_lower_bound(1, 1)
        assert 0.0 < bound < 0.5

    def test_grows_with_support(self) -> None:
        assert wilson_lower_bound(10, 10) > wilson_lower_bound(3, 3)

    def test_disagreement_lowers_the_bound(self) -> None:
        assert wilson_lower_bound(10, 6) < wilson_lower_bound(10, 10)

    def test_is_never_negative(self) -> None:
        assert wilson_lower_bound(4, 0) >= 0.0

    def test_is_finite_for_large_inputs(self) -> None:
        assert math.isfinite(wilson_lower_bound(1_000_000, 999_999))


class TestClaim:
    def test_label_is_derived_not_supplied(self) -> None:
        claim = Claim(
            field="name",
            value="Ada",
            confidence=0.86,
            support=2,
            independent_domains=("a.example", "b.example"),
            evidence=(
                Evidence(source_url="https://a.example", quote="Ada"),
                Evidence(source_url="https://b.example", quote="Ada"),
            ),
        )
        assert claim.label is Confidence.HIGH
        assert claim.corroborated
        assert len(claim.independent_domains) == 2

    def test_single_source_is_not_corroborated(self) -> None:
        claim = Claim(
            field="city",
            value="London",
            confidence=0.31,
            support=1,
            independent_domains=("a.example",),
            evidence=(Evidence(source_url="https://a.example", quote="London"),),
        )
        assert not claim.corroborated
        assert claim.label is Confidence.LOW
