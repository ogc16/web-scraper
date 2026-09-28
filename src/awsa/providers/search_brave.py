"""Brave Search provider (requires ``AWSA_BRAVE_API_KEY``).

Preferred over the keyless endpoint when a key is present: higher reliability,
proper snippets, and results that include the snippet text itself — which lets
the agent decide whether a page is worth fetching before spending the budget.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from ..errors import ProviderError
from ..models import SearchHit
from ..net.client import build_async_client
from .htmlresult import strip_tags

__all__ = ["BraveSearch"]

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveSearch:
    """Keyed :class:`~awsa.providers.base.SearchProvider` implementation."""

    name = "brave"
    requires_key = True
    reliability = 0.95

    def __init__(
        self,
        api_key: str,
        *,
        country: str = "us",
        safesearch: str = "moderate",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
        network_enabled: bool = True,
    ) -> None:
        if not api_key:
            msg = "BraveSearch requires a non-empty API key"
            raise ProviderError(msg)
        self._country = country
        self._safesearch = safesearch
        self._client = build_async_client(
            network_enabled=network_enabled,
            timeout=httpx.Timeout(timeout),
            transport=transport,
            headers={
                "X-Subscription-Token": api_key,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )

    async def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        """Query the Brave web-search API and map results to :class:`SearchHit`."""
        try:
            response = await self._client.get(
                _ENDPOINT,
                params={
                    "q": query,
                    "count": min(20, max(1, limit)),
                    "country": self._country,
                    "safesearch": self._safesearch,
                },
            )
        except httpx.HTTPError as exc:
            msg = f"Brave search failed: {exc}"
            raise ProviderError(msg) from exc

        if response.status_code in {401, 403}:
            msg = f"Brave rejected the API key (HTTP {response.status_code})"
            raise ProviderError(msg)
        if response.status_code >= 400:
            msg = f"Brave search returned HTTP {response.status_code}: {response.text[:200]}"
            raise ProviderError(msg)

        payload = response.json()
        results = (payload.get("web") or {}).get("results") or []
        hits: list[SearchHit] = []
        for index, item in enumerate(results[:limit]):
            url = item.get("url") or ""
            if not url:
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=strip_tags(item.get("title") or ""),
                    snippet=strip_tags(item.get("description") or ""),
                    provider=self.name,
                    rank=index,
                )
            )
        return hits

    async def aclose(self) -> None:
        await self._client.aclose()
