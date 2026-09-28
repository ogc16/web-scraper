"""Reduce a parsed page to the text that actually carries information.

The scoring model is the one readability-style extractors use: score every
block container by its own text volume, penalised by link density and boosted
by class/id priors, then take the highest-scoring container and its textually
similar siblings. Falls back to whole-body text when the page is too small or
too link-heavy for scoring to be meaningful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from ..config import ExtractionSettings
from ..models import Source
from .html import Node, collect_links, parse_html, text_of

__all__ = ["PageContent", "read_html", "read_page"]

_SENTENCE_SPLIT: Final = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_WS: Final = re.compile(r"\s+")
_WORD: Final = re.compile(r"\w+", re.UNICODE)

# Containers that commonly hold one article but whose children carry the text.
_CANDIDATE_TAGS: Final = frozenset(
    {"div", "article", "section", "main", "td", "blockquote", "ul", "ol", "form", "body"}
)

# A node whose links account for more than this share of its words is navigation,
# not an article, however much text it contains.
_MAX_LINK_DENSITY: Final = 0.5


@dataclass(slots=True)
class PageContent:
    """Readable text plus provenance metadata for one page."""

    url: str
    title: str
    text: str
    description: str = ""
    links: tuple[str, ...] = ()
    json_ld: tuple[dict[str, object], ...] = ()
    strategy: str = "body"
    score: float = 0.0
    truncated: bool = False

    @property
    def word_count(self) -> int:
        return len(_WORD.findall(self.text))

    @property
    def sentences(self) -> list[str]:
        if not self.text:
            return []
        flat = _WS.sub(" ", self.text.replace("\n", " ")).strip()
        return [s.strip() for s in _SENTENCE_SPLIT.split(flat) if s.strip()]

    def to_source(self, **kwargs: object) -> Source:
        return Source(url=self.url, final_url=self.url, text=self.text, title=self.title, **kwargs)  # type: ignore[arg-type]


@dataclass(slots=True)
class _Score:
    node: Node
    base: float = 0.0
    link_density: float = 1.0
    link_penalty: float = 0.0
    class_prior: float = 0.0
    word_count: int = 0
    text: str = ""

    @property
    def total(self) -> float:
        return self.base + self.link_penalty + self.class_prior


def _score_node(node: Node) -> _Score:
    text = text_of(node)
    words = len(_WORD.findall(text))
    paragraphs = len(node.find_all("p"))
    # Count the CJK fullwidth comma too: CJK prose is comma-heavy, and scoring
    # an article as sentence-free would wrongly demote it.
    commas = text.count(",") + text.count("，")  # noqa: RUF001

    score = _Score(node=node, text=text, word_count=words)
    score.base = (1.0 / (words + 1)) * 50.0 if words else 0.0
    score.base += paragraphs * 3.0
    score.base += commas * 0.5
    score.link_density = node.link_density() if words else 1.0
    score.link_penalty = -score.link_density * 25.0
    score.class_prior = node.class_weight()
    return score


def _select_container(
    root: Node,
    max_link_density: float,
) -> tuple[Node, float, str]:
    """Pick the highest-scoring node that is prose rather than a link farm."""
    best: _Score | None = None
    for node in root.iter_elements(skip_chrome=True):
        if node.tag not in _CANDIDATE_TAGS:
            continue
        score = _score_node(node)
        if not score.text.strip() or score.word_count < 5:
            continue
        if score.link_density > max_link_density:
            continue
        if best is None or score.total > best.total:
            best = score
    if best is None:
        return root, 0.0, "body"
    return best.node, best.total, f"node:{best.node.tag}.{best.node.id_token or '+'}"


_SKIP_HARD = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "canvas"})


def _grow_container(chosen: Node, threshold: float = 0.05) -> str:
    """Append siblings whose vocabulary resembles the winner's.

    Split articles often spread one story across sibling ``<div>``s of equal
    class. Merging them recovers the full text without dragging in the rest of
    the page, which is the failure mode of simply taking the largest container.
    """
    base_text = _WS.sub(" ", text_of(chosen)).strip()
    if not base_text or chosen.parent is None:
        return base_text
    parts = [base_text]
    for sibling in chosen.parent.children:
        if not isinstance(sibling, Node) or sibling is chosen:
            continue
        if sibling.looks_like_chrome() or sibling.tag in _SKIP_HARD:
            continue
        text = _WS.sub(" ", text_of(sibling)).strip()
        if len(text) < 30:
            continue
        if node_similarity(chosen, sibling) >= threshold:
            parts.append(text)
    return "\n\n".join(parts)


def node_similarity(a: Node, b: Node) -> float:
    """Jaccard similarity over the word sets of two subtrees."""
    words_a = set(_WORD.findall(text_of(a).lower()))
    words_b = set(_WORD.findall(text_of(b).lower()))
    if not words_a or not words_b:
        return 0.0
    intersection = len(words_a & words_b)
    return intersection / float(len(words_a | words_b))


_SENTENCE_END: Final = re.compile(r"[.!?](?:\s|$)")


def _prose_ratio(text: str) -> float:
    """Fraction of words that belong to a punctuated, sentence-length run.

    Article prose is punctuated and runs long; navigation, tag clouds and link
    lists survive flattening as unpunctuated fragments. Only the parser's
    whitespace-flattened text survives by this point, so punctuation is the
    only signal left that distinguishes a sentence from a menu.
    """
    words = _WORD.findall(text)
    if not words:
        return 0.0
    flat = _WS.sub(" ", text)
    prose = 0
    consumed = 0
    for match in _SENTENCE_END.finditer(flat):
        segment = flat[consumed : match.start()]
        consumed = match.end()
        if len(_WORD.findall(segment)) >= 4:
            prose += len(_WORD.findall(segment))
    return prose / len(words)


def read_html(
    url: str,
    html: str,
    settings: ExtractionSettings | None = None,
) -> PageContent:
    """Extract readable text and metadata from an HTML document.

    Args:
        url: Final URL, used to absolutise relative links.
        html: Raw document decoded to text.
        settings: Tuning knobs for density thresholds and length caps.

    Returns:
        A :class:`PageContent`. A page that fails density checks still returns
        the whole visible text, flagged with ``strategy="body"``, because a
        little text beats no text.
    """
    cfg = settings or ExtractionSettings()
    parsed = parse_html(html)

    container, score, strategy = _select_container(parsed.root, _MAX_LINK_DENSITY)
    text = _WS.sub(" ", _grow_container(container)).strip()

    if len(text) < cfg.min_content_chars or _prose_ratio(text) < cfg.min_content_density:
        text = _WS.sub(" ", text_of(parsed.root)).strip()
        strategy = "body"
        score = 0.0

    truncated = len(text) > cfg.max_chars_per_page
    if truncated:
        text = text[: cfg.max_chars_per_page].rsplit(" ", 1)[0]

    base = parsed.base_href or url
    # Scope links to the selected container. Collecting from the whole document
    # returns site chrome — nav bars, stylesheets, footer legalese — which
    # crowds out the links that actually belong to the article.
    links = collect_links(container, base, same_host_only=True)

    return PageContent(
        url=url,
        title=parsed.title or parsed.meta.get("og:title", ""),
        text=text,
        description=parsed.description,
        links=links,
        json_ld=parsed.json_ld if cfg.extract_json_ld else (),
        strategy=strategy,
        score=round(score, 3),
        truncated=truncated,
    )


def read_page(
    url: str,
    body: bytes,
    settings: ExtractionSettings | None = None,
) -> PageContent:
    """Decode ``body`` and delegate to :func:`read_html`, guessing the charset."""
    return read_html(url, decode_body(body), settings)


_CHARSET_RE: Final = re.compile(rb"""charset=["']?\s*([\w\-]+)""", re.IGNORECASE)


def decode_body(body: bytes) -> str:
    """Decode bytes using the charset declared in the document, then in headers."""
    declared: Final = _CHARSET_RE.search(body[:4096])
    if declared:
        encoding = declared.group(1).decode("ascii", "ignore")
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            pass
    for candidate in ("utf-8", "cp1252", "latin-1"):
        try:
            return body.decode(candidate)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def extract_structured_fields(content: PageContent) -> dict[str, object]:
    """Pull high-signal values out of JSON-LD and OpenGraph meta.

    These are publisher-declared facts, so they are treated as stronger evidence
    than text an extractor had to infer.
    """
    fields: dict[str, object] = {}
    if content.description:
        fields["description"] = content.description
    for blob in content.json_ld:
        for key in ("name", "headline", "description", "url"):
            value = blob.get(key)
            if isinstance(value, str) and value and key not in fields:
                fields[key] = value
        author = blob.get("author")
        if isinstance(author, dict):
            name = author.get("name")
            if isinstance(name, str) and name:
                fields.setdefault("author", name)
        elif isinstance(author, str) and author:
            fields.setdefault("author", author)
        for key in ("addressCountry", "addressLocality"):
            address = blob.get("address")
            if isinstance(address, dict):
                value = address.get(key)
                if isinstance(value, str) and value:
                    fields.setdefault(key, value)
    return fields
