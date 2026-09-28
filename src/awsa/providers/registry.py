"""Provider registry: resolve a provider name to a live instance.

Resolution is a three-step ladder, chosen so a run degrades instead of failing:

1. the explicitly requested provider, if it is available;
2. the best *keyed* provider whose credentials are present;
3. the keyless default, which always works.

``auto`` names walk that ladder. Every resolved provider is recorded in
:meth:`Registry.describe` so the CLI can print exactly which stack ran and why.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..errors import ProviderError
from ..observability import get_logger
from .base import ExtractedField, ExtractRequest, LLMProvider, LLMUsage, PlanRequest, SearchProvider
from .llm_extractive import ExtractiveLLM
from .search_ddg import DuckDuckGoSearch

__all__ = ["LLMStack", "Registry", "SearchStack"]

log = get_logger("providers.registry")

_SEARCH_ORDER = ("brave", "brightdata-serp", "duckduckgo")
_LLM_ORDER = ("openai", "extractive")


@dataclass(slots=True)
class SearchStack:
    """Primary search provider plus fallbacks, tried in order."""

    primary: SearchProvider
    fallbacks: tuple[SearchProvider, ...] = ()
    notes: list[str] = field(default_factory=list)

    def candidates(self) -> Iterator[SearchProvider]:
        yield self.primary
        yield from self.fallbacks

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": getattr(p, "name", type(p).__name__),
                "requires_key": bool(getattr(p, "requires_key", False)),
                "reliability": getattr(p, "reliability", None),
            }
            for p in self.candidates()
        ]


@dataclass(slots=True)
class LLMStack:
    """Primary LLM provider plus fallbacks, tried in order."""

    primary: LLMProvider
    fallbacks: tuple[LLMProvider, ...] = ()
    notes: list[str] = field(default_factory=list)

    def candidates(self) -> Iterator[LLMProvider]:
        yield self.primary
        yield from self.fallbacks

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": getattr(p, "name", type(p).__name__),
                "requires_key": bool(getattr(p, "requires_key", False)),
            }
            for p in self.candidates()
        ]


class Registry:
    """Builds provider stacks from configuration, lazily and defensively."""

    def __init__(self, config: Config) -> None:
        self.config = config
        # Separate caches per kind: each provider is built exactly once, so its
        # HTTP client is created once. Keyed by name so a rebuild never leaks.
        self._search_cache: dict[str, SearchProvider] = {}
        self._llm_cache: dict[str, LLMProvider] = {}
        self._notes: list[str] = []

    def _note(self, message: str) -> None:
        if message not in self._notes:
            self._notes.append(message)
        log.debug("%s", message)

    @property
    def notes(self) -> list[str]:
        return list(self._notes)

    def _build_search(self, name: str) -> SearchProvider | None:
        cache_key = name
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]
        provider: SearchProvider | None = None
        # Every provider gets the run's network switch, so `--no-network`
        # cannot be defeated by a provider that talks to httpx directly.
        net = self.config.network_enabled
        try:
            if name == "duckduckgo":
                provider = DuckDuckGoSearch(
                    user_agent=self.config.network.user_agent, network_enabled=net
                )
            elif name == "brave":
                key = self.config.providers.brave_api_key
                if not key:
                    self._note("brave requested but AWSA_BRAVE_API_KEY is unset")
                    return None
                from .search_brave import BraveSearch

                provider = BraveSearch(key, network_enabled=net)
            elif name == "brightdata-serp":
                creds = self._brightdata_credentials()
                if creds is None:
                    self._note("brightdata-serp requested but credentials are incomplete")
                    return None
                from .brightdata import BrightDataSerp

                provider = BrightDataSerp(creds, network_enabled=net)
            else:
                self._note(f"unknown search provider {name!r}")
                return None
        except ProviderError as exc:
            self._note(f"could not build search provider {name!r}: {exc}")
            return None
        self._search_cache[cache_key] = provider
        return provider

    def _brightdata_credentials(self) -> Any:
        from .brightdata import BrightDataCredentials

        settings = self.config.providers
        try:
            return BrightDataCredentials(
                api_token=settings.brightdata_api_token or "",
                zone=settings.brightdata_zone or "",
                username=settings.brightdata_username or "",
                password=settings.brightdata_password or "",
            )
        except Exception as exc:
            self._note(f"brightdata credentials unusable: {exc}")
            return None

    def search_stack(self, name: str | None = None) -> SearchStack:
        """Resolve the search provider stack, honouring ``auto`` and overrides."""
        requested = (name or self.config.providers.search_provider or "auto").lower()
        primary: SearchProvider | None = None
        fallbacks: list[SearchProvider] = []

        if requested != "auto":
            primary = self._build_search(requested)
            if primary is None:
                self._note(f"falling back to duckduckgo; {requested!r} unavailable")
                primary = self._build_search("duckduckgo")
            if primary is None:
                self._note("no search provider could be built; creating keyless default")
                primary = DuckDuckGoSearch(
                    user_agent=self.config.network.user_agent,
                    network_enabled=self.config.network_enabled,
                )
            if requested != "duckduckgo":
                alt = self._build_search("duckduckgo")
                if alt is not None and alt is not primary:
                    fallbacks.append(alt)
        else:
            for candidate in _SEARCH_ORDER:
                provider = self._build_search(candidate)
                if provider is None:
                    continue
                if primary is None:
                    primary = provider
                elif provider is not primary:
                    fallbacks.append(provider)
            if primary is None:
                self._note("no search provider could be built; creating keyless default")
                primary = DuckDuckGoSearch(
                    user_agent=self.config.network.user_agent,
                    network_enabled=self.config.network_enabled,
                )

        return SearchStack(primary=primary, fallbacks=tuple(fallbacks), notes=self.notes)

    def _build_llm(self, name: str) -> LLMProvider | None:
        cache_key = f"{name}:{self.config.providers.llm_model}"
        if cache_key in self._llm_cache:
            return self._llm_cache[cache_key]
        provider: LLMProvider | None = None
        if name == "openai":
            if self.config.offline or not self.config.providers.openai_api_key:
                self._note("openai requested but no API key is set")
                return None
            from .llm_openai import OpenAICompatLLM

            provider = OpenAICompatLLM(self.config)
        elif name == "extractive":
            provider = ExtractiveLLM()
        else:
            self._note(f"unknown llm provider {name!r}")
            return None
        self._llm_cache[cache_key] = provider
        return provider

    def llm_stack(self, name: str | None = None) -> LLMStack:
        """Resolve the LLM provider stack, honouring ``auto`` and overrides."""
        requested = (name or self.config.providers.llm_provider or "auto").lower()
        primary: LLMProvider | None = None
        fallbacks: list[LLMProvider] = []

        if requested != "auto":
            primary = self._build_llm(requested)
            if primary is None:
                self._note(f"falling back to extractive; {requested!r} unavailable")
                primary = self._build_llm("extractive")
            if primary is None:
                self._note("no LLM provider available; using deterministic extractor")
                primary = ExtractiveLLM()
            if requested != "extractive":
                alt = self._build_llm("extractive")
                if alt is not None and alt is not primary:
                    fallbacks.append(alt)
        else:
            for candidate in _LLM_ORDER:
                provider = self._build_llm(candidate)
                if provider is None:
                    continue
                if primary is None:
                    primary = provider
                elif provider is not primary:
                    fallbacks.append(provider)
            if primary is None:
                primary = ExtractiveLLM()
                self._note("no LLM provider available; using deterministic extractor")

        return LLMStack(primary=primary, fallbacks=tuple(fallbacks), notes=self.notes)

    def unlocker(self) -> Any:
        """Return a Bright Data unlocker if configured, else ``None``."""
        if self.config.offline:
            return None
        creds = self._brightdata_credentials()
        if creds is None:
            return None
        from .brightdata import BrightDataUnlocker

        return BrightDataUnlocker(creds, network_enabled=self.config.network_enabled)

    async def aclose(self) -> None:
        """Close every provider that owns an HTTP client."""
        for provider in (*self._search_cache.values(), *self._llm_cache.values()):
            closer = getattr(provider, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:
                log.debug("closing %s failed: %s", provider, exc)

    def describe(self) -> dict[str, Any]:
        """Human-readable description of the resolved stack, for the CLI banner."""
        search = self.search_stack()
        llm = self.llm_stack()
        return {
            "search": search.describe(),
            "llm": llm.describe(),
            "notes": self.notes,
        }


def merge_usage(stacks: tuple[LLMProvider, ...]) -> LLMUsage:
    """Sum usage across every LLM provider that was consulted."""
    total = LLMUsage()
    for provider in stacks:
        usage = provider.usage()
        total.calls += usage.calls
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens
        total.failures += usage.failures
        total.estimated_cost_usd += usage.estimated_cost_usd
    return total


__all__ += ["ExtractRequest", "ExtractedField", "PlanRequest", "merge_usage"]
