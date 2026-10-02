"""Exception hierarchy for :mod:`awsa`.

Every error carries a machine-readable ``code`` so the CLI can map failures onto
exit statuses and JSON reports without string matching.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from .models import FetchResult

__all__ = [
    "AwsaError",
    "BudgetExhausted",
    "ConfigError",
    "FetchError",
    "NetworkBlocked",
    "ProviderError",
    "RobotsDenied",
    "SerializationError",
    "UnsafeURLError",
]


class _RetryableStatus(Exception):
    """Internal signal: the host answered, but with a status worth retrying.

    Carries the already-built :class:`~awsa.models.FetchResult` so that when the
    retries run out the caller receives the final status and body rather than a
    generic failure. Not part of the public error surface: by the time this
    escapes the retry loop it has been converted into either a result or a
    :class:`FetchError`.
    """

    def __init__(self, *, status: int, delay: float, result: FetchResult) -> None:
        super().__init__(f"retryable status {status}")
        self.status = status
        self.delay = delay
        self.result = result


class _TransportFailure(Exception):
    """Internal signal: the request never produced a status at all.

    Kept apart from :class:`_RetryableStatus` because the two back off
    differently -- there is no ``Retry-After`` to honour and no host throttle
    history when the socket never opened.
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


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


class SerializationError(AwsaError):
    """A result held a value JSON cannot represent.

    Raised instead of coercing, so a malformed field is caught at the boundary
    where it was produced rather than by whoever consumes the output later.
    """

    code: ClassVar[str] = "serialization_error"
