"""Keyless web search.

Uses DuckDuckGo's no-JS HTML endpoint, which needs no API key and no account.
That is what lets ``awsa`` run on a fresh clone with zero configuration, and it
is why this provider is the default rather than a hosted SERP API.

Practical caveats, stated plainly: this endpoint is rate-limited, occasionally
returns an interstitial challenge page, and its result markup is not a stable
contract. :func:`search` therefore treats an unparseable response as an empty
result rather than an error, and :class:`DuckDuckGoSearch` reports a low
``reliability`` score so the agent can prefer a keyed provider when one exists.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from ..errors import ProviderError
from ..models import SearchHit
from ..net.client import build_async_client
from ..observability import get_logger
from .htmlresult import iter_result_blocks

__all__ = ["DuckDuckGoSearch", "html_search", "parse_ddg_html"]

log = get_logger("providers.search.ddg")

_ENDPOINT = "https://html.duckduckgo.com/html/"
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_CHALLENGE_MARKERS = ("anomaly.js", "unfortunately, bots", "are you a robot", "captcha")


@dataclass(slots=True)
class SearchHealth:
    """Observed success rate of a search provider, used to rank fallbacks.

    Mutable by design: the counters are updated in place as calls are made.
    """

    attempts: int = 0
    successes: int = 0
    challenge_blocks: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0


def detect_challenge(html: str) -> bool:
    """True when the body is a bot-challenge page rather than a results page.

    Anonymous endpoints answer a refused query with an interstitial that looks
    superficially like a result list. Treating that as "no results" hides the
    real cause, so it is detected and reported separately.
    """
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def parse_ddg_html(html: str, *, limit: int = 10) -> list[SearchHit]:
    """Parse DuckDuckGo's HTML result page into :class:`SearchHit` objects.

    Args:
        html: Raw response body.
        limit: Maximum results to return.

    Returns:
        Ranked hits. Returns an empty list when the body is a bot challenge
        rather than a results page, so callers degrade instead of crashing.
    """
    if detect_challenge(html):
        log.debug("DuckDuckGo body is a bot challenge, not a result list")
        return []

    hits: list[SearchHit] = []
    for rank, (href, title) in enumerate(iter_result_blocks(html)):
        if len(hits) >= limit:
            break
        url = _clean_redirect(href)
        if not url:
            continue
        hits.append(
            SearchHit(
                url=url,
                title=title,
                snippet="",
                provider="duckduckgo",
                rank=rank,
            )
        )
    return hits


def _clean_redirect(href: str) -> str:
    """Unwrap DuckDuckGo's ``/l/?uddg=`` redirect, or pass through a direct link."""
    href = href.strip()
    if not href:
        return ""
    # Normalise protocol-relative URLs *before* looking for the redirect
    # wrapper, otherwise the unwrap below is never reached for `//duckduckgo.com/...`
    # links, which is the form DuckDuckGo actually serves.
    if href.startswith("//"):
        href = f"https:{href}"
    parts = urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg")
        if target:
            return unquote(target[0])
    if parts.scheme in {"http", "https"}:
        return unquote(href)
    return ""


async def html_search(
    client: httpx.AsyncClient,
    query: str,
    *,
    limit: int = 10,
    user_agent: str = "awsa/0.1",
    max_retries: int = 2,
) -> tuple[list[SearchHit], bool]:
    """POST a query to DuckDuckGo's HTML endpoint with bounded retries.

    Returns the hits and whether the response was a bot challenge, so callers
    can tell "nothing matched" apart from "we were refused".
    """
    payload = {"q": query}
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://duckduckgo.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_status = 0
    for attempt in range(max_retries + 1):
        try:
            response = await client.post(_ENDPOINT, data=payload, headers=headers)
        except httpx.HTTPError as exc:
            if attempt >= max_retries:
                msg = f"DuckDuckGo search failed: {exc}"
                raise ProviderError(msg) from exc
            await asyncio.sleep(1.0 + attempt)
            continue
        last_status = response.status_code
        if response.status_code in _RETRYABLE_STATUS and attempt < max_retries:
            # Jitter de-synchronises concurrent retries; not security-relevant.
            await asyncio.sleep(1.5 * (attempt + 1) + random.random() * 0.5)  # noqa: S311
            continue
        if response.status_code >= 400:
            log.debug("DuckDuckGo returned status %d", response.status_code)
            return [], False
        body = response.text
        return parse_ddg_html(body, limit=limit), detect_challenge(body)
    log.debug("DuckDuckGo exhausted retries, last status %d", last_status)
    return [], False


class DuckDuckGoSearch:
    """Keyless :class:`~awsa.providers.base.SearchProvider` implementation."""

    name = "duckduckgo"
    requires_key = False
    reliability = 0.6

    def __init__(
        self,
        *,
        user_agent: str = "awsa/0.1",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
        network_enabled: bool = True,
    ) -> None:
        self._user_agent = user_agent
        self._client = build_async_client(
            network_enabled=network_enabled,
            timeout=httpx.Timeout(timeout),
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
        self.health = SearchHealth()
        #: Set when the endpoint answered with a bot challenge rather than
        #: results. Read by the agent loop to emit an actionable note.
        self.blocked_by_challenge = False

    async def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        self.health.attempts += 1
        results, challenged = await html_search(
            self._client,
            query,
            limit=limit,
            user_agent=self._user_agent,
        )
        self.blocked_by_challenge = bool(challenged and not results)
        if results:
            self.health.successes += 1
        elif challenged:
            self.health.challenge_blocks += 1
        return results

    async def aclose(self) -> None:
        await self._client.aclose()
