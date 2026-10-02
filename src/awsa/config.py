"""Configuration loading, validation and secret redaction.

Configuration comes from three places, in increasing precedence: built-in
defaults, environment variables (prefixed ``AWSA_``), then explicit keyword
overrides. Nothing here ever raises on a *missing* optional credential — the
agent is designed to run with zero secrets and degrade to offline extractors.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Final, Self

from .errors import ConfigError
from .models import Budget

__all__ = ["DEFAULT_USER_AGENT", "Config", "redact"]

DEFAULT_USER_AGENT: Final = "awsa/0.1 (+https://github.com/ogc16/WebScraper)"

_SECRET_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "openai_api_key",
        "brave_api_key",
        "brightdata_api_token",
        "brightdata_password",
        "firecrawl_api_key",
    }
)

_SECRET_PATTERNS: Final = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
)

_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


def redact(value: str | None) -> str:
    """Mask a secret for display: keep a short prefix, drop the rest."""
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…({len(value)} chars)"


def _scrub(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Resolved provider endpoints, credentials and per-provider switches."""

    search_provider: str = "auto"
    fetch_provider: str = "http"
    llm_provider: str = "auto"

    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    brave_api_key: str | None = None

    brightdata_api_token: str | None = None
    brightdata_zone: str | None = None
    brightdata_username: str | None = None
    brightdata_password: str | None = None

    firecrawl_api_key: str | None = None


@dataclass(frozen=True, slots=True)
class NetworkSettings:
    """Politeness, safety and resource limits applied to every outbound request."""

    user_agent: str = DEFAULT_USER_AGENT
    #: Extra user agents to rotate through, beyond ``user_agent``. Empty by
    #: default, which means one honest, stable identifier -- the intended
    #: behaviour. Populating this is an opt-in decision to present several
    #: identities, so it is never inferred and never set implicitly.
    user_agent_rotation: tuple[str, ...] = ()
    timeout_seconds: float = 20.0
    connect_timeout_seconds: float = 10.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    max_page_bytes: int = 2 * 1024 * 1024
    max_redirects: int = 5
    per_host_delay_seconds: float = 1.0
    respect_robots: bool = True
    cache_ttl_seconds: float = 86_400.0
    cache_dir: Path | None = None
    verify_tls: bool = True

    allow_private_hosts: bool = False
    host_allowlist: frozenset[str] = field(default_factory=frozenset)
    host_blocklist: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            msg = f"timeout_seconds must be > 0, got {self.timeout_seconds}"
            raise ConfigError(msg)
        if self.max_retries < 0:
            msg = f"max_retries must be >= 0, got {self.max_retries}"
            raise ConfigError(msg)
        if self.per_host_delay_seconds < 0:
            msg = "per_host_delay_seconds must be >= 0"
            raise ConfigError(msg)
        if not self.user_agent.strip():
            msg = "user_agent must not be blank"
            raise ConfigError(msg)
        if any(not agent.strip() for agent in self.user_agent_rotation):
            # A blank entry would send an empty UA, which some hosts treat as
            # "no automated client" and reject outright.
            msg = "user_agent_rotation entries must not be blank"
            raise ConfigError(msg)
        if self.user_agent in self.user_agent_rotation:
            # Harmless, but almost certainly a mistake: rotating a set that
            # contains the primary UA just makes the primary less likely.
            msg = "user_agent_rotation should not repeat user_agent"
            raise ConfigError(msg)
        if self.max_page_bytes < 1024:
            msg = "max_page_bytes must be >= 1024"
            raise ConfigError(msg)
        overlap = self.host_allowlist & self.host_blocklist
        if overlap:
            msg = f"host(s) {sorted(overlap)} are both allowed and blocked"
            raise ConfigError(msg)


@dataclass(frozen=True, slots=True)
class ExtractionSettings:
    """Heuristics for reducing raw HTML to the text an LLM can reason about."""

    min_content_chars: int = 200
    min_content_density: float = 0.35
    max_chars_per_page: int = 60_000
    min_snippet_chars: int = 40
    max_snippet_chars: int = 400
    extract_json_ld: bool = True
    extract_meta: bool = True


@dataclass(frozen=True, slots=True)
class Config:
    """Fully-resolved run configuration."""

    providers: ProviderSettings = field(default_factory=ProviderSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    extraction: ExtractionSettings = field(default_factory=ExtractionSettings)
    budget: Budget = field(default_factory=Budget)
    log_level: str = "INFO"
    offline: bool = False
    network_enabled: bool = True

    def __post_init__(self) -> None:
        # `--no-network` implies `--offline`: refusing to open a socket already
        # rules out calling a hosted model, so honouring both avoids the
        # contradictory "network off but use OpenAI" combination.
        if not self.network_enabled:
            object.__setattr__(self, "offline", True)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        /,
        **overrides: object,
    ) -> Self:
        """Build a config from ``AWSA_*`` environment variables plus overrides.

        Raises:
            ConfigError: if a value is present but unparseable or out of range.
        """
        src: Mapping[str, str] = env if env is not None else os.environ
        get = src.get

        raw_cache_dir = get("AWSA_CACHE_DIR") or None

        def as_int(name: str, default: int) -> int:
            raw = get(name)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc

        def as_float(name: str, default: float) -> float:
            raw = get(name)
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a number, got {raw!r}") from exc

        def as_bool(name: str, default: bool) -> bool:
            raw = get(name)
            if raw is None or raw == "":
                return default
            lowered = raw.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
            raise ConfigError(f"{name} must be a boolean, got {raw!r}")

        def as_opt(name: str) -> str | None:
            raw = get(name)
            return raw.strip() or None if raw else None

        providers = ProviderSettings(
            search_provider=get("AWSA_SEARCH_PROVIDER") or "auto",
            fetch_provider=get("AWSA_FETCH_PROVIDER") or "http",
            llm_provider=get("AWSA_LLM_PROVIDER") or "auto",
            openai_api_key=as_opt("AWSA_OPENAI_API_KEY"),
            openai_base_url=get("AWSA_OPENAI_BASE_URL") or "https://api.openai.com/v1",
            llm_model=get("AWSA_LLM_MODEL") or "gpt-4o-mini",
            brave_api_key=as_opt("AWSA_BRAVE_API_KEY"),
            brightdata_api_token=as_opt("AWSA_BRIGHTDATA_API_TOKEN"),
            brightdata_zone=as_opt("AWSA_BRIGHTDATA_ZONE"),
            brightdata_username=as_opt("AWSA_BRIGHTDATA_USERNAME"),
            brightdata_password=as_opt("AWSA_BRIGHTDATA_PASSWORD"),
            firecrawl_api_key=as_opt("AWSA_FIRECRAWL_API_KEY"),
        )

        network = NetworkSettings(
            user_agent=get("AWSA_USER_AGENT") or DEFAULT_USER_AGENT,
            # Comma-separated, e.g. "awsa/0.1 (+https://...), my-bot/2".
            # Unset by default, which leaves a single stable identity in place.
            user_agent_rotation=tuple(
                agent.strip()
                for agent in (get("AWSA_USER_AGENT_ROTATION") or "").split(",")
                if agent.strip()
            ),
            timeout_seconds=as_float("AWSA_TIMEOUT", 20.0),
            max_retries=as_int("AWSA_MAX_RETRIES", 3),
            per_host_delay_seconds=as_float("AWSA_PER_HOST_DELAY", 1.0),
            respect_robots=as_bool("AWSA_RESPECT_ROBOTS", True),
            cache_ttl_seconds=as_float("AWSA_CACHE_TTL", 86_400.0),
            cache_dir=Path(raw_cache_dir) if raw_cache_dir else None,
            verify_tls=as_bool("AWSA_VERIFY_TLS", True),
            allow_private_hosts=as_bool("AWSA_ALLOW_PRIVATE_HOSTS", False),
        )

        extraction = ExtractionSettings(
            min_content_chars=as_int("AWSA_MIN_CONTENT_CHARS", 200),
            max_chars_per_page=as_int("AWSA_MAX_CHARS_PER_PAGE", 60_000),
        )

        budget = Budget(
            max_search_queries=as_int("AWSA_MAX_SEARCH_QUERIES", 8),
            max_pages=as_int("AWSA_MAX_PAGES", 20),
            max_llm_calls=as_int("AWSA_MAX_LLM_CALLS", 24),
            max_wall_seconds=as_float("AWSA_MAX_WALL_SECONDS", 120.0),
        )

        scalar_fields = {"log_level", "offline", "providers", "network", "extraction", "budget"}
        known = {f.name for f in fields(cls)}
        for key in overrides:
            if key not in known:
                msg = (
                    f"unknown config override {key!r}; valid keys: {sorted(known - scalar_fields)}"
                )
                raise ConfigError(msg)

        # Network is on unless it is explicitly refused, by override or env var.
        network_enabled = (
            bool(overrides["network_enabled"])
            if "network_enabled" in overrides
            else not as_bool("AWSA_NO_NETWORK", False)
        )

        return cls(
            providers=overrides.get("providers", providers),  # type: ignore[arg-type]
            network=overrides.get("network", network),  # type: ignore[arg-type]
            extraction=overrides.get("extraction", extraction),  # type: ignore[arg-type]
            budget=overrides.get("budget", budget),  # type: ignore[arg-type]
            log_level=str(overrides.get("log_level") or (get("AWSA_LOG_LEVEL") or "INFO")).upper(),
            offline=bool(overrides.get("offline", as_bool("AWSA_OFFLINE", False))),
            network_enabled=network_enabled,
        )

    # -- credential introspection ------------------------------------------

    def has_llm(self) -> bool:
        return bool(self.providers.openai_api_key) and not self.offline

    def has_paid_search(self) -> bool:
        return bool(self.providers.brave_api_key) and not self.offline

    def has_unblocker(self) -> bool:
        return bool(self.providers.brightdata_api_token) and not self.offline

    def with_budget(self, budget: Budget) -> Config:
        return Config(
            providers=self.providers,
            network=self.network,
            extraction=self.extraction,
            budget=budget,
            log_level=self.log_level,
            offline=self.offline,
        )

    def redacted(self) -> dict[str, object]:
        """A dict safe to log or print: every secret replaced by a mask."""
        out: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "providers":
                out[f.name] = {
                    pf.name: redact(getattr(value, pf.name))
                    if pf.name in _SECRET_FIELDS
                    else getattr(value, pf.name)
                    for pf in fields(value)
                }
            elif f.name == "network":
                out[f.name] = {
                    nf.name: (
                        str(getattr(value, nf.name))
                        if isinstance(getattr(value, nf.name), Path)
                        else getattr(value, nf.name)
                    )
                    for nf in fields(value)
                }
            elif f.name == "budget":
                out[f.name] = value.as_dict()
            else:
                out[f.name] = value
        return out
