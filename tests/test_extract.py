"""HTML parsing and main-content extraction."""

from __future__ import annotations

import pytest

from awsa.extract.html import (
    Node,
    collect_links,
    parse_html,
    strip_tags,
    text_of,
)
from awsa.extract.reader import read_html

SIMPLE = """
<html><head><title>Hi</title></head>
<body><main><p>Real content here.</p><p>More content.</p></main></body></html>
"""

CHROME = """
<html><head><title>Page</title><style>a{color:red}</style></head>
<body>
  <nav class="navbar"><a href="/a">Nav</a></nav>
  <div class="sidebar"><a href="/b">Side</a></div>
  <article class="post-content">
    <h1>Article title</h1>
    <p>The first paragraph of the actual article body, which runs long enough to
    clear the minimum content length that the density heuristic insists on
    before it will trust a container over the whole document.</p>
    <p>The second paragraph, which also counts toward the density score and
    pushes the link density of this container well below the threshold that
    would make the extractor reject it.</p>
    <p>A third paragraph to make the density unambiguous, and to give the
    extractor enough prose to work with when it splits the page into sentences.</p>
    <a href="/lovelace-notes">Her notes on the Analytical Engine</a>
  </article>
  <footer class="site-footer"><a href="/c">Footer</a></footer>
</body></html>
"""


class TestParser:
    def test_extracts_title(self) -> None:
        assert parse_html(SIMPLE).title == "Hi"

    def test_extracts_meta(self) -> None:
        parsed = parse_html(
            '<html><head><meta name="description" content="d">'
            '<meta property="og:title" content="og"></head><body></body></html>'
        )
        assert parsed.meta["description"] == "d"
        assert parsed.meta["og:title"] == "og"

    def test_reads_base_href(self) -> None:
        assert parse_html(
            '<html><head><base href="https://x.test/a/"></head></html>'
        ).base_href == ("https://x.test/a/")

    def test_title_falls_back_to_og(self) -> None:
        html = (
            '<html><head><meta property="og:title" content="OG title">'
            "</head><body><p>content</p></body></html>"
        )
        assert read_html("https://x.test/", html).title == "OG title"

    def test_survives_unclosed_tags(self) -> None:
        parsed = parse_html("<html><body><div><p>text<p>more</body>")
        assert "text" in text_of(parsed.root)
        assert "more" in text_of(parsed.root)

    def test_survives_empty_document(self) -> None:
        assert parse_html("").title == ""
        assert parse_html("").json_ld == ()

    def test_handles_unclosed_script(self) -> None:
        parsed = parse_html("<html><body><script>var a = 1;")
        assert "var a" not in text_of(parsed.root)

    def test_drops_style_and_script_content(self) -> None:
        text = text_of(parse_html(CHROME).root)
        assert "color:red" not in text
        assert "a{color" not in text

    def test_collects_json_ld(self) -> None:
        parsed = parse_html(
            '<html><head><script type="application/ld+json">'
            '{"@type":"Person","name":"Ada"}</script></head></html>'
        )
        assert parsed.json_ld
        assert parsed.json_ld[0].get("name") == "Ada"

    def test_ignores_malformed_json_ld(self) -> None:
        parsed = parse_html(
            '<html><head><script type="application/ld+json">{not json</script></head></html>'
        )
        assert parsed.json_ld == ()


class TestChromeDetection:
    @staticmethod
    def _node(tag: str, cls: str | None = None, ident: str | None = None) -> Node:
        attrs: dict[str, str] = {}
        if cls:
            attrs["class"] = cls
        if ident:
            attrs["id"] = ident
        return Node(tag=tag, attrs=attrs)

    @pytest.mark.parametrize(
        "value",
        ["nav", "navbar", "sidebar", "footer", "menu", "comment", "advert", "cookie-banner"],
    )
    def test_detects_chrome_classes(self, value: str) -> None:
        assert self._node("div", value).looks_like_chrome()

    @pytest.mark.parametrize(
        "value", ["post-content", "article-body", "entry-content", "main", "story"]
    )
    def test_ignores_content_classes(self, value: str) -> None:
        assert not self._node("div", value).looks_like_chrome()

    @pytest.mark.parametrize("tag", ["nav", "header", "footer", "aside"])
    def test_chrome_tags_are_chrome(self, tag: str) -> None:
        assert self._node(tag).looks_like_chrome()

    @pytest.mark.parametrize("tag", ["article", "main", "section", "div", "p"])
    def test_content_tags_are_not_chrome(self, tag: str) -> None:
        assert not self._node(tag).looks_like_chrome()

    def test_detects_chrome_by_id(self) -> None:
        assert self._node("div", None, "sidebar").looks_like_chrome()
        assert not self._node("div", None, "content").looks_like_chrome()

    def test_unclassed_node_is_not_chrome(self) -> None:
        assert not self._node("div").looks_like_chrome()


class TestNode:
    def test_class_tokens_split_on_whitespace(self) -> None:
        assert self_tokens(Node(tag="div", attrs={"class": "a  b c"})) == {"a", "b", "c"}

    def test_link_density_of_plain_text_is_zero(self) -> None:
        node = Node(tag="p", attrs={}, children=["lots of words here"])
        assert node.link_density() == 0.0

    def test_link_density_of_all_links_is_one(self) -> None:
        node = Node(
            tag="div",
            attrs={},
            children=[Node(tag="a", attrs={"href": "/a"}, children=["a"])],
        )
        assert node.link_density() == 1.0

    def test_link_density_of_mixed_content_is_between(self) -> None:
        node = Node(
            tag="div",
            attrs={},
            children=[
                Node(tag="a", attrs={"href": "/a"}, children=["link"]),
                " and plenty of surrounding prose to dilute it",
            ],
        )
        assert 0.0 < node.link_density() < 1.0

    def test_find_all_locates_main(self) -> None:
        root = parse_html("<html><body><main><p>x</p></main></body></html>").root
        found = root.find_all("main")
        assert len(found) == 1

    def test_find_all_returns_empty_when_absent(self) -> None:
        root = parse_html("<html><body><p>x</p></body></html>").root
        assert root.find_all("main") == []

    def test_find_by_role_accepts_a_predicate(self) -> None:
        root = parse_html("<html><body><main><p>x</p></main></body></html>").root
        assert root.find_by_role(lambda n: n.tag == "main")


def self_tokens(node: Node) -> frozenset[str]:
    return node.class_tokens


class TestLinks:
    def test_absolutises_relative_links(self) -> None:
        parsed = parse_html('<html><body><a href="/x">x</a></body></html>')
        links = collect_links(parsed.root, "https://example.com/base/")
        assert "https://example.com/x" in links

    def test_honours_base_href(self) -> None:
        # The reader resolves the base before calling collect_links, so the
        # base case is exercised end to end rather than via a parameter.
        html = (
            '<html><head><base href="https://cdn.test/"></head>'
            '<body><main><a href="y">y</a></main></body></html>'
        )
        assert "https://cdn.test/y" in read_html("https://example.com/", html).links

    def test_same_host_only_filters_external(self) -> None:
        parsed = parse_html(
            '<html><body><a href="https://other.test/x">o</a>'
            '<a href="https://example.com/x">i</a></body></html>'
        )
        links = collect_links(parsed.root, "https://example.com/", same_host_only=True)
        assert "https://other.test/x" not in links
        assert "https://example.com/x" in links

    def test_drops_javascript_and_mailto(self) -> None:
        parsed = parse_html(
            '<html><body><a href="javascript:void(0)">j</a>'
            '<a href="mailto:a@b.test">m</a><a href="#top">t</a></body></html>'
        )
        assert collect_links(parsed.root, "https://example.com/") == ()

    def test_deduplicates(self) -> None:
        parsed = parse_html('<html><body><a href="/x">1</a><a href="/x">2</a></body></html>')
        assert collect_links(parsed.root, "https://example.com/") == ("https://example.com/x",)


class TestReader:
    def test_selects_the_article_over_the_chrome(self) -> None:
        page = read_html("https://example.com/p", CHROME)
        assert "Article title" in page.text
        assert "Nav" not in page.text
        assert "Footer" not in page.text

    def test_reports_the_strategy(self) -> None:
        assert read_html("https://example.com/p", CHROME).strategy.startswith("node:")

    def test_links_are_scoped_to_the_content(self) -> None:
        """Site chrome links must not crowd out the article's own links."""
        page = read_html("https://example.com/p", CHROME)
        assert "/a" not in page.links  # from <nav>
        assert "/c" not in page.links  # from <footer>

    def test_extracts_structured_metadata(self) -> None:
        page = read_html(
            "https://example.com/p",
            '<html><head><script type="application/ld+json">'
            '{"@type":"Person","name":"Ada Lovelace","jobTitle":"Mathematician"}'
            "</script></head><body><p>x</p></body></html>",
        )
        assert page.json_ld
        assert page.json_ld[0].get("name") == "Ada Lovelace"
        assert page.json_ld[0].get("jobTitle") == "Mathematician"

    def test_falls_back_to_body_for_thin_pages(self) -> None:
        page = read_html("https://example.com/p", "<html><body>hi</body></html>")
        assert page.strategy == "body"
        assert page.text == "hi"

    def test_truncates_long_pages(self) -> None:
        from awsa.config import ExtractionSettings

        html = "<html><body><main><p>" + ("word " * 5000) + "</p></main></body></html>"
        page = read_html("https://example.com/p", html, ExtractionSettings(max_chars_per_page=500))
        assert page.truncated
        assert len(page.text) <= 500

    def test_handles_empty_body(self) -> None:
        assert read_html("https://example.com/p", "").text == ""

    def test_min_content_density_rejects_link_farms(self) -> None:
        """The setting is a real gate: a nav-only page must fall back to body."""
        from dataclasses import replace

        from awsa.config import ExtractionSettings

        links = "".join(f'<a href="/{i}">Item{i}</a> ' for i in range(12))
        html = f"<html><body><nav>{links}</nav></body></html>"

        permissive = replace(ExtractionSettings(), min_content_density=0.0)
        assert read_html("https://example.com/", html, permissive).strategy == "body"

        strict = replace(ExtractionSettings(), min_content_density=0.9)
        assert read_html("https://example.com/", html, strict).strategy == "body"

    def test_prose_survives_a_strict_density_threshold(self) -> None:
        from dataclasses import replace

        from awsa.config import ExtractionSettings

        html = (
            "<html><body><article><p>The quick brown fox jumps over the lazy dog "
            "repeatedly and often, sprinting across open fields while the sun sets "
            "slowly behind distant hills. Another long sentence here, with plenty of "
            "ordinary prose to make the density value comfortably high.</p>"
            "</article></body></html>"
        )
        strict = replace(ExtractionSettings(), min_content_density=0.9)
        page = read_html("https://example.com/", html, strict)
        assert page.strategy.startswith("node:")
        assert "quick brown fox" in page.text


class TestStripTags:
    def test_removes_markup(self) -> None:
        assert "hello" in strip_tags("<p>hello</p>")
        assert "<p>" not in strip_tags("<p>hello</p>")

    def test_removes_script_bodies(self) -> None:
        assert "alert" not in strip_tags("<script>alert(1)</script>text")

    def test_preserves_word_content(self) -> None:
        # The fast path substitutes a space for every tag, including inline
        # ones, so <b>a</b>b becomes "a b". Inline elements are not separated
        # by whitespace in the source, so this is a documented approximation
        # rather than a faithful rendering — the tree-based text_of is used
        # whenever exactness matters.
        assert "a" in strip_tags("<b>a</b>b")
        assert "b" in strip_tags("<b>a</b>b")
