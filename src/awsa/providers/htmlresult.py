"""Shared SERP parsing helpers.

Result markup differs per provider but follows the same two-step shape: find
each result container, then pull a link, a title and a snippet. Isolating that
here keeps each provider file to its transport concerns.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from html import unescape

__all__ = ["RESULT_LINK_RE", "first_link", "iter_result_blocks", "strip_tags"]

RESULT_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

_BLOCK_RE = re.compile(
    r'<div[^>]+class="[^"]*result(?![-\w])[^"]*"[^>]*>(?P<body>.*?)'
    r'(?=<div[^>]+class="[^"]*result(?![-\w])[^"]*"|\Z)',
    re.IGNORECASE | re.DOTALL,
)

_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(?P<body>.*?)</a>', re.IGNORECASE | re.DOTALL
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_tags(fragment: str) -> str:
    """Remove tags and collapse whitespace."""
    return _WS.sub(" ", unescape(_TAG_RE.sub(" ", fragment))).strip()


def iter_result_blocks(html: str) -> Iterator[tuple[str, str]]:
    """Yield ``(href, title)`` for each result link found in ``html``.

    Falls back to a page-wide scan when the container regex finds nothing, so a
    provider tweaking its layout degrades to noisier results rather than none.
    """
    blocks = list(_BLOCK_RE.finditer(html))
    if blocks:
        for block in blocks:
            body = block.group("body")
            match = RESULT_LINK_RE.search(body)
            if match:
                yield match.group("href"), strip_tags(match.group("title"))
        return
    for match in RESULT_LINK_RE.finditer(html):
        yield match.group("href"), strip_tags(match.group("title"))


def first_link(html: str, pattern: str) -> str:
    """Return the href of the first anchor whose class matches ``pattern``."""
    regex = re.compile(
        rf'<a[^>]+class="[^"]*{pattern}[^"]*"[^>]+href="(?P<href>[^"]+)"', re.IGNORECASE
    )
    match = regex.search(html)
    return match.group("href") if match else ""


def snippet_for(html: str) -> str:
    """Return the first result snippet found in ``html``, if any."""
    match = _SNIPPET_RE.search(html)
    return strip_tags(match.group("body")) if match else ""
