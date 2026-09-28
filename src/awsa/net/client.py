"""Single construction point for every outbound HTTP client.

``--no-network`` promises to open no sockets at all. Enforcing that only in
:class:`~awsa.net.http.HttpFetcher` would be a lie: the search, LLM and
unblocking providers each hold their own :class:`httpx.AsyncClient`, and a
future one would too. Routing every client through :func:`build_async_client`
means the switch has exactly one implementation and one place to audit.

The refusal is raised at transport level, so it also covers requests made by
library code we do not control, and it fires before a socket exists.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import NetworkBlocked

__all__ = ["NetworkOffTransport", "build_async_client"]


class NetworkOffTransport(httpx.AsyncBaseTransport):
    """A transport that refuses every request instead of dialling out.

    Used when the run is configured with ``network_enabled=False``. It is
    deliberately total: there is no allowlist escape hatch, because a flag
    promising no network traffic that sometimes permits it is worse than no
    flag at all.
    """

    def __init__(self, reason: str = "network access is disabled by --no-network") -> None:
        self._reason = reason

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # The URL goes in the message and the cause in `detail`, so that
        # `str(exc)` renders the reason exactly once.
        raise NetworkBlocked(f"refusing to request {str(request.url)!r}", detail=self._reason)

    async def aclose(self) -> None:
        """No resources to release: this transport never acquires any."""


def build_async_client(*, network_enabled: bool = True, **kwargs: Any) -> httpx.AsyncClient:
    """Build an :class:`httpx.AsyncClient` that respects the network switch.

    Args:
        network_enabled: when false, the client is given a transport that
            raises :class:`~awsa.errors.NetworkBlocked` instead of connecting.
        **kwargs: forwarded to :class:`httpx.AsyncClient`.

    Returns:
        A client. Callers still own it and must close it.
    """
    if not network_enabled:
        # Override rather than merge: a caller-supplied transport is a real
        # socket, so it cannot coexist with a promise of no sockets.
        kwargs["transport"] = NetworkOffTransport()
    return httpx.AsyncClient(**kwargs)
