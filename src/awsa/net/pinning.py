"""Connection-level DNS pinning.

:mod:`awsa.net.guard` decides *which addresses are acceptable*, and
:meth:`~awsa.net.guard.SSRFGuard.recheck` re-decides that immediately before a
socket opens. Both leave one gap: ``httpx`` resolves the hostname a second time,
by itself, when it dials. A name validated as public a millisecond earlier can
have been rebound by then, and the connection lands on the attacker's address
anyway.

This module closes that gap by making the *validated* address the one dialled.
``httpcore`` asks its network backend to connect to a hostname, so this backend
substitutes the address the guard approved. Because ``httpcore`` still sees the
original hostname, the ``Host`` header and TLS SNI are unaffected -- which is
exactly what a naive "rewrite the URL to an IP literal" fix would break.

The backend **fails closed**: a host the guard has not approved is refused
rather than resolved. That is stricter than resolving for real, and it is the
property that makes the pinning worth having.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any, Final

import httpcore
import httpx

from ..errors import NetworkBlocked
from ..observability import get_logger

__all__ = ["ApprovedHosts", "PinnedNetworkBackend", "PinningTransport"]

log = get_logger("net.pinning")

#: The pool arguments this module reconstructs. Checked at construction so an
#: httpcore upgrade that drops one fails loudly here instead of silently
#: reverting the client to unpinned defaults.
_POOL_KWARGS: Final[tuple[str, ...]] = (
    "ssl_context",
    "max_connections",
    "max_keepalive_connections",
    "keepalive_expiry",
    "http1",
    "http2",
    "uds",
    "local_address",
    "retries",
    "socket_options",
    "network_backend",
)


class ApprovedHosts:
    """The addresses the guard has approved, keyed by hostname.

    Shared mutable state is safe here for a specific reason: entries are keyed by
    host, and every value stored for a host has already passed the same check, so
    two concurrent fetches of one host can only ever agree on what is safe to
    dial. The map narrows; it never widens.
    """

    def __init__(self) -> None:
        self._approved: dict[str, tuple[str, ...]] = {}

    def approve(self, host: str, addresses: Iterable[str]) -> None:
        """Record the addresses a just-completed recheck cleared for ``host``."""
        pinned = tuple(addresses)
        if pinned:
            self._approved[host.lower().rstrip(".")] = pinned

    def get(self, host: str) -> tuple[str, ...]:
        """Return the approved addresses for ``host``, or ``()`` if unvetted."""
        return self._approved.get(host.lower().rstrip("."), ())

    def forget(self, host: str) -> None:
        """Drop a host's approval, e.g. once its connection is closed."""
        self._approved.pop(host.lower().rstrip("."), None)


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Dial only the addresses the guard approved, for the host it was asked for.

    Args:
        approved: The guard's current verdicts. A host missing from this mapping
            is refused rather than resolved.
        inner: The backend that actually connects. Defaults to httpcore's AnyIO
            backend; tests pass a recording double.
    """

    def __init__(
        self, approved: ApprovedHosts, inner: httpcore.AsyncNetworkBackend | None = None
    ) -> None:
        self._approved = approved
        self._inner: httpcore.AsyncNetworkBackend = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> Any:
        """Connect to a vetted address for ``host``.

        Raises:
            NetworkBlocked: ``host`` was never approved by the guard. The name is
                not resolved -- the refusal is the point of failing closed.
            httpcore.ConnectError: every approved address was tried and none
                accepted the connection. This is a network failure, not a policy
                decision, so the caller's retry logic sees it as one.
        """
        pinned = self._approved.get(host)
        if not pinned:
            # Fail closed. A name the guard cleared is recorded before the
            # request is issued, so reaching here means the request bypassed the
            # guarded path -- which is exactly where resolving for real would
            # hand the connection back to whoever controls the DNS record.
            msg = f"refusing to connect to {host!r}: not approved by the SSRF guard"
            raise NetworkBlocked(msg, detail="host was never vetted; refusing to resolve it")

        last: Exception | None = None
        for address in pinned:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last = exc
                log.debug("pinned address %s for %s failed: %s", address, host, exc)
        # The host was vetted; only the connections failed. Report this as a
        # connection error so the caller's existing retry/backoff logic handles
        # it, rather than as a policy block, which would abort on the first try
        # and misreport a busy server as a security decision.
        msg = f"no approved address for {host!r} accepted the connection"
        if last is not None:
            raise httpcore.ConnectError(msg) from last
        raise NetworkBlocked(msg, detail="all approved addresses failed")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> Any:
        """Delegate. Unix sockets are not part of the HTTP fetch path."""
        return await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        """Delegate. ``httpcore`` calls this for connection-pool backoff."""
        await self._inner.sleep(seconds)


class PinningTransport(httpx.AsyncHTTPTransport):
    """An ``httpx`` transport whose connections go only to vetted addresses.

    ``httpx`` does not expose ``httpcore``'s ``network_backend`` parameter, so the
    pool is rebuilt here with one. That is a dependency on httpcore internals;
    :func:`_assert_pool_contract` turns a future incompatibility into an
    immediate failure at construction rather than a silent loss of pinning.

    When a proxy is configured, pinning is **not** applied: the proxy resolves
    the hostname, not this process, so pinning would only pin the proxy's own
    address. That case logs a warning rather than pretending to a guarantee.
    """

    def __init__(self, approved: ApprovedHosts, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Exact type, not isinstance: httpcore's AsyncHTTPProxy is a *subclass* of
        # AsyncConnectionPool, so isinstance would match both.
        if type(self._pool) is not httpcore.AsyncConnectionPool:
            log.warning(
                "DNS pinning is not applied when a proxy is configured: the proxy "
                "resolves the hostname, not this process"
            )
            return
        _assert_pool_contract()
        limits = kwargs.get("limits") or httpx.Limits()
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(
                verify=kwargs.get("verify", True),
                cert=kwargs.get("cert"),
                trust_env=kwargs.get("trust_env", True),
            ),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=kwargs.get("http1", True),
            http2=kwargs.get("http2", False),
            uds=kwargs.get("uds"),
            local_address=kwargs.get("local_address"),
            retries=kwargs.get("retries", 0),
            socket_options=kwargs.get("socket_options"),
            network_backend=PinnedNetworkBackend(approved),
        )


def _assert_pool_contract() -> None:
    """Fail loudly if httpcore no longer accepts the pool arguments used here.

    Without this, an httpcore upgrade that drops a parameter would raise a
    ``TypeError`` at construction -- which is at least loud, but with an
    unhelpful message. Checking up front names the dependency and the way out.
    """
    parameters = inspect.signature(httpcore.AsyncConnectionPool.__init__).parameters
    missing = sorted(name for name in _POOL_KWARGS if name not in parameters)
    if missing:
        msg = (
            f"httpcore.AsyncConnectionPool no longer accepts {', '.join(missing)}; "
            "DNS pinning cannot be constructed safely. Pin awsa's httpcore "
            "dependency to a version whose pool API matches, or turn pinning off "
            "explicitly rather than connecting unpinned by accident."
        )
        raise RuntimeError(msg)
