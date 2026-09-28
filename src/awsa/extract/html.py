"""A small, dependency-free HTML parser producing a queryable tree.

Exists so main-content extraction can reason about structure (paragraph density,
link density, heading placement) rather than regex-mangling markup. Deliberately
lenient: real-world HTML is malformed and this parser must never raise on it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Final
from urllib.parse import urldefrag, urljoin

__all__ = [
    "Node",
    "ParseResult",
    "iter_text",
    "parse_html",
    "strip_tags",
    "text_of",
]

_VOID_TAGS: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

_SKIP_TAGS: Final = frozenset(
    {"script", "style", "noscript", "template", "svg", "canvas", "iframe"}
)

_BLOCK_TAGS: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "div",
        "dl",
        "dd",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

_CHROME_TAGS: Final = frozenset(
    {
        "nav",
        "aside",
        "header",
        "footer",
        "form",
        "iframe",
        "button",
        "select",
        "noscript",
        "menu",
        "dialog",
        "svg",
        "canvas",
    }
)

_TAGS_WITH_URL: Final = {"a": "href", "link": "href", "img": "src", "script": "src"}

#: Exact class/id tokens that mark a container as site furniture.
_CHROME_TOKENS: Final = frozenset(
    {
        "nav",
        "navbar",
        "navigation",
        "menu",
        "sidebar",
        "side-bar",
        "footer",
        "header",
        "masthead",
        "banner",
        "advert",
        "ads",
        "ad",
        "cookie",
        "consent",
        "popup",
        "modal",
        "social",
        "share",
        "sharing",
        "subscribe",
        "newsletter",
        "related",
        "recommend",
        "comments",
        "comment",
        "breadcrumb",
        "pagination",
        "widget",
        "toolbar",
        "search",
        "skip-link",
        "sr-only",
        "screen-reader",
        "hidden",
    }
)

#: Substrings that mark chrome even inside a compound token. Deliberately
#: narrower than :data:`_CHROME_TOKENS` so that words merely containing them —
#: "advertorial", "commentary" — are not mistaken for furniture.
_CHROME_SUBSTRINGS: Final = (
    "navbar",
    "navigation",
    "sidebar",
    "side-bar",
    "footer",
    "header",
    "menu",
    "cookie",
    "popup",
    "modal",
    "breadcrumb",
    "pagination",
    "newsletter",
    "comment",
)

_WS: Final = re.compile(r"[ \t\r\f\v]+")
_BLANKS: Final = re.compile(r"\n{3,}")


@dataclass(slots=True)
class Node:
    """One element in the parsed tree. Text children are stored as plain ``str``."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)
    depth: int = 0

    @property
    def class_tokens(self) -> frozenset[str]:
        return frozenset((self.attrs.get("class") or "").lower().split())

    @property
    def id_token(self) -> str:
        return (self.attrs.get("id") or "").lower()

    def iter_elements(self, *, skip_chrome: bool = False) -> Iterator[Node]:
        """Depth-first walk over descendant elements, optionally skipping chrome."""
        for child in self.children:
            if isinstance(child, str):
                continue
            if skip_chrome and child.tag in _CHROME_TAGS:
                continue
            yield child
            yield from child.iter_elements(skip_chrome=skip_chrome)

    def iter_all(self) -> Iterator[Node]:
        for child in self.children:
            if isinstance(child, str):
                continue
            yield child
            yield from child.iter_all()

    def find_all(self, *tags: str) -> list[Node]:
        wanted = {t.lower() for t in tags}
        return [n for n in self.iter_all() if n.tag in wanted]

    def find_by_role(self, predicate: Callable[[Node], bool]) -> list[Node]:
        return [n for n in self.iter_all() if predicate(n)]

    def attr(self, name: str, default: str = "") -> str:
        return self.attrs.get(name.lower(), default)

    def looks_like_chrome(self) -> bool:
        """Heuristic: nav/menu/sidebar-ish container rather than article body."""
        if self.tag in {"nav", "aside", "footer", "header"}:
            return True
        if self.tag in {"div", "section"}:
            tokens = self.class_tokens | {self.id_token}
            if tokens & _CHROME_TOKENS:
                return True
            # Real class names are compound — "cookie-banner", "site-footer",
            # "main-navigation" — so a token-level comparison alone misses the
            # containers we most want to drop.
            blob = " ".join(t for t in tokens if t)
            if any(word in blob for word in _CHROME_SUBSTRINGS):
                return True
        if self.attr("role").lower() in {"navigation", "banner", "complementary", "search"}:
            return True
        if self.attr("aria-hidden", "").lower() == "true":
            return True
        if self.tag in {"div", "span", "p", "section"} and "hidden" in self.class_tokens:
            return True
        style = self.attr("style").lower().replace(" ", "")
        return "display:none" in style or "visibility:hidden" in style

    def link_density(self) -> float:
        """Fraction of this node's text that sits inside anchors."""
        total = len(text_of(self).strip())
        if total == 0:
            return 1.0
        linked = 0
        for anchor in self.find_all("a"):
            linked += len(text_of(anchor).strip())
        return min(1.0, linked / total)

    def class_weight(self) -> float:
        """Small positive/negative prior from class and id names.

        Mirrors the signal real readability implementations use: ``article`` or
        ``post-content`` deserves a boost, ``sidebar`` or ``comment`` a penalty.
        """
        tokens = self.class_tokens | {self.id_token}
        if not tokens:
            return 0.0
        weight = 0.0
        positives = {
            "article",
            "articlebody",
            "post",
            "postbody",
            "entry",
            "content",
            "main",
            "page",
            "story",
            "text",
            "body",
            "markdown",
            "prose",
            "blog",
        }
        negatives = {
            "comment",
            "comments",
            "sidebar",
            "footer",
            "nav",
            "menu",
            "meta",
            "widget",
            "share",
            "social",
            "promo",
            "ad",
            "ads",
            "banner",
            "related",
            "recirc",
            "trending",
            "newsletter",
            "subscribe",
            "cta",
            "pagination",
            "pager",
            "author",
            "byline",
            "tags",
            "breadcrumb",
        }
        for token in tokens:
            if token in positives or any(
                p in token for p in ("article", "content", "post-", "entry-")
            ):
                weight += 25.0
            if token in negatives or any(
                n in token for n in ("comment", "sidebar", "promo", "share")
            ):
                weight -= 25.0
        if "hidden" in tokens or "sr-only" in tokens or "screen-reader" in tokens:
            weight -= 50.0
        return weight


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node(tag="#document", depth=0)
        self.current = self.root
        self.title = ""
        self.meta: dict[str, str] = {}
        self.json_ld: list[dict[str, object]] = []
        self.base_href: str | None = None
        self._in_title = False
        self._script_buf: list[str] = []
        self._script_type: str | None = None
        self._in_json_ld = False
        self.title_tag: Node | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {k.lower(): (v or "") for k, v in attrs}
        node = Node(tag=tag, attrs=attributes, parent=self.current, depth=self.current.depth + 1)

        if tag == "base" and attributes.get("href"):
            self.base_href = attributes["href"]
        if tag == "title":
            self._in_title = True
            self.title_tag = node
        if tag == "meta":
            name = (attributes.get("name") or attributes.get("property") or "").lower()
            if name and attributes.get("content"):
                self.meta.setdefault(name, attributes["content"])
        if tag == "script":
            self._script_type = attributes.get("type", "").lower()
            self._script_buf = []
            self._in_json_ld = "ld+json" in self._script_type

        if tag in _SKIP_TAGS:
            # Descend into the skipped node so its text lands *inside* it and is
            # dropped with it. Leaving `current` on the parent would strand the
            # contents as a sibling string, which is how <style> CSS and
            # <noscript> fallback copy end up in the extracted article.
            self.current.children.append(node)
            if tag not in _VOID_TAGS:
                self.current = node
            return
        self.current.children.append(node)
        if tag not in _VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag == "script":
            if self._in_json_ld and self._script_buf:
                self._collect_json_ld("".join(self._script_buf))
            self._in_json_ld = False
            self._script_buf = []
        if tag in _VOID_TAGS:
            return
        node: Node | None = self.current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._script_buf.append(data)
            return
        if self._in_title:
            self.title += data
            return
        if not data.strip():
            if self.current.children and isinstance(self.current.children[-1], str):
                return
            if self.current is self.root:
                return
        self.current.children.append(data)

    def _collect_json_ld(self, raw: str) -> None:
        import json

        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(parsed, dict):
            self.json_ld.append(parsed)
        elif isinstance(parsed, list):
            self.json_ld.extend(item for item in parsed if isinstance(item, dict))


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Everything the reader needs from one HTML document."""

    root: Node
    title: str
    meta: dict[str, str]
    json_ld: tuple[dict[str, object], ...]
    base_href: str | None

    @property
    def description(self) -> str:
        for key in ("og:description", "description", "twitter:description"):
            value = self.meta.get(key)
            if value:
                return value
        return ""

    @property
    def site_name(self) -> str:
        return self.meta.get("og:site_name", "")

    @property
    def canonical(self) -> str:
        for node in self.root.find_all("link"):
            if node.attr("rel").lower() == "canonical" and node.attr("href"):
                return node.attr("href")
        return ""


def parse_html(html: str) -> ParseResult:
    """Parse ``html`` into a :class:`ParseResult`. Never raises on malformed input."""
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except (AssertionError, ValueError, RecursionError):
        pass
    return ParseResult(
        root=builder.root,
        title=_WS.sub(" ", builder.title).strip(),
        meta=builder.meta,
        json_ld=tuple(builder.json_ld),
        base_href=builder.base_href,
    )


def text_of(node: Node, *, block_separator: str = "\n") -> str:
    """Render visible text of ``node`` with sensible block spacing."""
    out: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
        elif child.tag in _SKIP_TAGS:
            continue
        elif child.tag == "br":
            out.append("\n")
        elif child.tag in _BLOCK_TAGS:
            inner = text_of(child, block_separator=block_separator).strip()
            if inner:
                out.append(f"{block_separator}{inner}{block_separator}")
        else:
            out.append(text_of(child, block_separator=block_separator))
    text = "".join(out)
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def iter_text(node: Node) -> Iterator[str]:
    """Yield visible text fragments in document order."""
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                yield child
        elif child.tag not in _SKIP_TAGS:
            yield from iter_text(child)


def strip_tags(html: str) -> str:
    """Fast path: reduce HTML to plain text without building a tree."""
    without_scripts = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", html)
    with_breaks = re.sub(
        r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article)>",
        "\n",
        without_scripts,
    )
    stripped = re.sub(r"(?s)<[^>]+>", " ", with_breaks)
    return _BLANKS.sub("\n\n", unescape(stripped)).strip()


def collect_links(root: Node, base_url: str, *, same_host_only: bool = True) -> tuple[str, ...]:
    """Collect absolute, http(s) hyperlinks, de-duplicated in document order."""
    from urllib.parse import urlsplit

    base_host = (urlsplit(base_url).hostname or "").lower()
    seen: set[str] = set()
    links: list[str] = []
    for node in root.iter_all():
        attr = _TAGS_WITH_URL.get(node.tag)
        if attr is None:
            continue
        raw = node.attr(attr)
        if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute = urldefrag(urljoin(base_url, raw))[0]
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"}:
            continue
        if same_host_only:
            host = (parts.hostname or "").lower().removeprefix("www.")
            if host != base_host.removeprefix("www."):
                continue
        normalised = absolute.rstrip("/") or absolute
        if normalised in seen:
            continue
        seen.add(normalised)
        links.append(normalised)
    return tuple(links)
