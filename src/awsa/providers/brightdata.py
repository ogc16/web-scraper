"""Bright Data Web Unlocker and SERP API adapters (optional, keyed).

These exist so the upstream demo's capability survives the rewrite without
becoming a dependency. Nothing in the agent imports this module unless
credentials are present, and the neutral ``Fetcher`` protocol means a run using
this provider and a run using direct HTTP are otherwise identical.

Two separate capabilities are exposed:

* :class:`BrightDataUnlocker` — route a page fetch through a Web Unlocker zone,
  which handles bot-protected targets direct HTTP cannot reach.
* :class:`BrightDataSerp` — obtain search results from the SERP API, which
  survives the bot challenges that make keyless scraping unreliable.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..errors import ConfigError, ProviderError
from ..models import SearchHit
from ..net.client import build_async_client
from .htmlresult import strip_tags

__all__ = ["BrightDataCredentials", "BrightDataSerp", "BrightDataUnlocker"]

_UNLOCKER_ENDPOINT = "https://api.brightdata.com/request"
_SERP_ENDPOINT = "https://api.brightdata.com/serp/v1/search"


@dataclass(frozen=True, slots=True)
class BrightDataCredentials:
    """Credentials for a Bright Data Web Unlocker zone."""

    api_token: str
    zone: str
    username: str = ""
    password: str = ""

    def __post_init__(self) -> None:
        if not self.api_token.strip():
            msg = "brightdata api_token is required"
            raise ConfigError(msg)
        if not self.zone.strip():
            msg = "brightdata zone is required"
            raise ConfigError(msg)

    def auth_header(self) -> str:
        """Return the ``Authorization`` header value, preferring the API token."""
        if self.api_token:
            return f"Bearer {self.api_token}"
        if self.username and self.password:
            raw = f"{self.username}:{self.password}".encode()
            return f"Basic {base64.b64encode(raw).decode()}"
        msg = "no usable Bright Data credential"
        raise ConfigError(msg)

    def redacted(self) -> dict[str, str]:
        return {
            "zone": self.zone,
            "api_token": f"{self.api_token[:4]}…({len(self.api_token)} chars)"
            if self.api_token
            else "<unset>",
        }


class BrightDataUnlocker:
    """Fetch a page through a Web Unlocker zone, returning raw HTML."""

    name = "brightdata-unlocker"
    requires_key = True
    reliability = 0.9

    def __init__(
        self,
        credentials: BrightDataCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
        network_enabled: bool = True,
    ) -> None:
        self._creds = credentials
        self._client = build_async_client(
            network_enabled=network_enabled,
            timeout=httpx.Timeout(timeout),
            transport=transport,
            headers={
                "Authorization": credentials.auth_header(),
                "Content-Type": "application/json",
            },
        )

    async def get_html(self, url: str, *, user_agent: str = "awsa/0.1") -> str:
        """Return the rendered HTML for ``url`` as seen through the unlocker zone."""
        payload: dict[str, Any] = {
            "url": url,
            "zone": self._creds.zone,
            "format": "raw",
        }
        if user_agent:
            payload["headers"] = {"User-Agent": [user_agent]}
        try:
            response = await self._client.post(_UNLOCKER_ENDPOINT, json=payload)
        except httpx.HTTPError as exc:
            msg = f"Bright Data unlocker request failed: {exc}"
            raise ProviderError(msg) from exc
        if response.status_code >= 400:
            msg = (
                f"Bright Data unlocker returned HTTP {response.status_code}: {response.text[:200]}"
            )
            raise ProviderError(msg)
        return response.text

    async def aclose(self) -> None:
        await self._client.aclose()


class BrightDataSerp:
    """Search via the Bright Data SERP API."""

    name = "brightdata-serp"
    requires_key = True
    reliability = 0.92

    def __init__(
        self,
        credentials: BrightDataCredentials,
        *,
        search_engine: str = "bing",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 45.0,
        network_enabled: bool = True,
    ) -> None:
        self._creds = credentials
        self._engine = search_engine
        self._client = build_async_client(
            network_enabled=network_enabled,
            timeout=httpx.Timeout(timeout),
            transport=transport,
            headers={
                "Authorization": credentials.auth_header(),
                "Content-Type": "application/json",
            },
        )

    async def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        """Run a SERP query and normalise organic results into :class:`SearchHit`."""
        payload = {
            "search_engine": self._engine,
            "query": query,
            "zone": self._creds.zone,
            "format": "json",
        }
        try:
            response = await self._client.post(_SERP_ENDPOINT, json=payload)
        except httpx.HTTPError as exc:
            msg = f"Bright Data SERP request failed: {exc}"
            raise ProviderError(msg) from exc
        if response.status_code >= 400:
            msg = f"Bright Data SERP returned HTTP {response.status_code}: {response.text[:200]}"
            raise ProviderError(msg)

        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            msg = "Bright Data SERP returned non-JSON body"
            raise ProviderError(msg) from exc

        return _normalise_serp(data, provider=self.name, limit=limit)

    async def aclose(self) -> None:
        await self._client.aclose()


def _normalise_serp(data: dict[str, Any], *, provider: str, limit: int) -> list[SearchHit]:
    """Pull organic results out of the several shapes the SERP API can return."""
    organic: Any = None
    body = data.get("body") if isinstance(data.get("body"), dict) else data
    if isinstance(body, dict):
        for key in ("organic", "organic_results", "results", "web"):
            candidate = body.get(key)
            if isinstance(candidate, list):
                organic = candidate
                break
            if isinstance(candidate, dict) and isinstance(candidate.get("results"), list):
                organic = candidate["results"]
                break
    if organic is None and isinstance(data.get("knowledge"), dict):
        organic = data["knowledge"].get("organic")
    if not isinstance(organic, list):
        return []

    hits: list[SearchHit] = []
    for index, item in enumerate(organic):
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("link") or ""
        if not url:
            continue
        hits.append(
            SearchHit(
                url=url,
                title=strip_tags(str(item.get("title") or "")),
                snippet=strip_tags(str(item.get("description") or item.get("snippet") or "")),
                provider=provider,
                rank=index,
            )
        )
        if len(hits) >= limit:
            break
    return hits
