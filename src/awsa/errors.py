"""Exception hierarchy for :mod:`awsa`.

Every error carries a machine-readable ``code`` so the CLI can map failures onto
exit statuses and JSON reports without string matching.
"""

from __future__ import annotations

from typing import ClassVar

__all__ = [
    "AwsaError",
    "BudgetExhausted",
    "ConfigError",
    "FetchError",
    "NetworkBlocked",
    "ProviderError",
    "RobotsDenied",
    "UnsafeURLError",
]


class AwsaError(Exception):
    """Base class for every error raised by this package."""

    # Stable machine-readable identifier. ClassVar, not Final: each subclass
    # deliberately overrides it, and `Final` forbids that at the type level.
    code: ClassVar[str] = "awsa_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message} ({self.detail})"
        return self.message


class ConfigError(AwsaError):
    """Invalid or contradictory configuration supplied by the caller."""

    code: ClassVar[str] = "config_error"


class UnsafeURLError(AwsaError):
    """A URL was rejected before any network I/O by the SSRF guard."""

    code: ClassVar[str] = "unsafe_url"


class NetworkBlocked(AwsaError):
    """The SSRF guard or a host allowlist denied the target host."""

    code: ClassVar[str] = "network_blocked"


class RobotsDenied(AwsaError):
    """``robots.txt`` disallows fetching the requested path for our user-agent."""

    code: ClassVar[str] = "robots_denied"


class FetchError(AwsaError):
    """A page could not be retrieved after exhausting retries."""

    code: ClassVar[str] = "fetch_error"


class ProviderError(AwsaError):
    """A pluggable provider (search, LLM, unblocking proxy) failed."""

    code: ClassVar[str] = "provider_error"


class BudgetExhausted(AwsaError):
    """The agent hit a configured resource ceiling and must stop."""

    code: ClassVar[str] = "budget_exhausted"
