"""robots.txt parsing and matching (RFC 9309)."""

from __future__ import annotations

import pytest

from awsa.net.robots import RobotsCache, RobotsPolicy, decide, parse_robots

SAMPLE = """
# a comment
User-agent: *
Disallow: /private/
Disallow: /admin
Allow: /admin/public
Crawl-delay: 2

User-agent: BadBot
Disallow: /

User-agent: Googlebot
Disallow: /nope

Sitemap: https://example.com/sitemap.xml
Sitemap: https://example.com/other.xml
"""


def policy_for(user_agent: str = "awsa/0.1", text: str = SAMPLE) -> RobotsPolicy:
    return parse_robots(text, user_agent=user_agent)


class TestParsing:
    def test_parses_multiple_sitemaps(self) -> None:
        assert policy_for().sitemaps == (
            "https://example.com/sitemap.xml",
            "https://example.com/other.xml",
        )

    def test_parses_crawl_delay(self) -> None:
        assert policy_for().rules.crawl_delay == 2.0

    def test_ignores_comments_and_blank_lines(self) -> None:
        assert policy_for(text="# only a comment\n\n").sitemaps == ()

    def test_empty_body_allows_everything(self) -> None:
        assert decide(policy_for(text=""), "https://x.test/anything").allowed

    def test_ignores_malformed_lines(self) -> None:
        parsed = policy_for(text="this is not a directive\nDisallow: /a\n")
        # A directive with no preceding User-agent group belongs to no group,
        # so it applies to nobody and the URL stays allowed.
        assert parsed.matched_group == "* (no groups)"
        assert decide(parsed, "https://x.test/a").allowed

    def test_malformed_line_does_not_break_the_group(self) -> None:
        parsed = policy_for(
            text="User-agent: *\ngarbage line\nDisallow: /a\nDisallow: /b\n",
        )
        assert not decide(parsed, "https://x.test/a").allowed
        assert not decide(parsed, "https://x.test/b").allowed

    def test_records_raw_size(self) -> None:
        assert policy_for(text="User-agent: *\nDisallow: /").raw_bytes == len(
            b"User-agent: *\nDisallow: /"
        )

    def test_marks_absent_file_as_unavailable(self) -> None:
        assert policy_for(text="").available is True

    def test_non_numeric_crawl_delay_is_dropped(self) -> None:
        assert policy_for(text="User-agent: *\nCrawl-delay: soon\n").rules.crawl_delay is None


class TestGroupSelection:
    def test_wildcard_group_applies(self) -> None:
        p = policy_for("SomeBot/1.0")
        assert not decide(p, "https://example.com/private/x").allowed
        assert not decide(p, "https://example.com/admin").allowed

    def test_specific_group_overrides_wildcard(self) -> None:
        p = policy_for("BadBot/1.0")
        # BadBot has a blanket Disallow: / that the * group does not.
        assert not decide(p, "https://example.com/anything").allowed
        assert p.matched_group == "badbot"
        # ...while the same URL is permitted for an unlisted agent.
        assert policy_for("OtherBot/1.0").matched_group == "*"

    def test_named_group_applies(self) -> None:
        assert not decide(policy_for("Googlebot/2.1"), "https://example.com/nope").allowed
        assert decide(policy_for("Other/1.0"), "https://example.com/nope").allowed

    def test_agent_match_is_case_insensitive(self) -> None:
        assert not decide(policy_for("GOOGLEBOT"), "https://example.com/nope").allowed

    def test_version_suffix_is_ignored(self) -> None:
        assert policy_for("BadBot/9.9 (+http://x)").matched_group == "badbot"

    def test_substring_does_not_match_a_different_agent(self) -> None:
        """A ``bot`` group must not capture ``Googlebot``."""
        text = "User-agent: bot\nDisallow: /\n\nUser-agent: *\nDisallow: /nothing\n"
        assert policy_for("Googlebot/2.1", text).matched_group == "*"
        assert policy_for("bot/1.0", text).matched_group == "bot"
        assert policy_for("mybot/1.0", text).matched_group == "*"

    def test_longest_match_wins_between_allow_and_disallow(self) -> None:
        p = policy_for()
        assert not decide(p, "https://example.com/admin/other").allowed
        assert decide(p, "https://example.com/admin/public/page").allowed

    def test_unlisted_paths_are_allowed(self) -> None:
        p = policy_for()
        assert decide(p, "https://example.com/").allowed
        assert decide(p, "https://example.com/articles/1").allowed

    @pytest.mark.parametrize(
        ("rule", "path", "allowed"),
        [
            ("/*/private", "/a/private/b", False),
            ("/private$", "/private", False),
            ("/private$", "/private/sub", True),
            ("/a/", "/a/b/c", False),
            ("/*.php$", "/index.php", False),
            ("/*.php$", "/index.php?x=1", True),
        ],
    )
    def test_wildcard_patterns(self, rule: str, path: str, *, allowed: bool) -> None:
        p = policy_for(text=f"User-agent: *\nDisallow: {rule}\n")
        assert decide(p, f"https://x.test{path}").allowed is allowed


class TestDecision:
    def test_explains_the_matched_rule(self) -> None:
        p = policy_for()
        decision = decide(p, "https://example.com/private/x")
        assert decision.allowed is False
        assert decision.matched_rule == "/private/"
        assert "disallow" in decision.reason.lower()

    def test_reason_when_nothing_matched(self) -> None:
        decision = decide(policy_for(), "https://example.com/open")
        assert decision.allowed is True
        assert decision.matched_rule is None


class TestCache:
    def test_stores_and_returns_a_policy(self) -> None:
        cache = RobotsCache()
        p = policy_for()
        assert cache.known("https://example.com") is False
        cache.put("https://example.com", p)
        assert cache.known("https://example.com") is True
        assert cache.get("https://example.com") is p

    def test_unknown_origin_returns_none(self) -> None:
        assert RobotsCache().get("https://nope.test") is None

    def test_keyed_by_origin_not_full_url(self) -> None:
        cache = RobotsCache()
        cache.put("https://example.com", policy_for())
        assert cache.known("https://example.com/deep/path?q=1") is True
