"""HTML reduction: parse a document and recover its readable main content."""

from __future__ import annotations

from .html import Node, ParseResult, collect_links, iter_text, parse_html, strip_tags, text_of
from .reader import (
    PageContent,
    decode_body,
    extract_structured_fields,
    node_similarity,
    read_html,
    read_page,
)

__all__ = [
    "Node",
    "PageContent",
    "ParseResult",
    "collect_links",
    "decode_body",
    "extract_structured_fields",
    "iter_text",
    "node_similarity",
    "parse_html",
    "read_html",
    "read_page",
    "strip_tags",
    "text_of",
]
