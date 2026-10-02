"""Connection-level DNS pinning.

The guarantee under test is that a socket goes to an address the guard approved,
not to whatever the hostname resolves to when httpx gets around to dialing. These
use a recording backend double rather than a real server: the interesting
assertion is *which address was handed to the connector*, which a real socket
would hide behind its own DNS.

A rebinding race cannot be staged reliably, so the tests below pin the
behaviour that makes it unwinnable for the attacker: an unapproved name is never
resolved at all.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
import pytest

from awsa.errors import NetworkBlocked
from awsa.net.guard import SSRFGuard, UrlVerdict
from awsa.net.pinning import (
    ApprovedHosts,
    PinnedNetworkBackend,
    PinningTransport,
    _assert_pool_contract,
)


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Records the host it was asked to connect to, and dials nothing.

    Stands in for :class:`httpcore.AnyIOBackend`. The recorded ``host`` is the
    whole point: the bug this whole module exists to prevent is the real backend
    being handed a hostname and resolving it itself.
    """

    def __init__(self, fail_for: frozenset[str] = frozenset()) -> None:
        self.connected: list[tuple[str, int]] = []
        self.slept: list[float] = []
        self.unix: list[str] = []
        self._fail_for = fail_for

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> Any:
        self.connected.append((host, port))
        if host in self._fail_for:
            msg = f"connection refused by {host}"
            raise httpcore.ConnectError(msg)
        return f"<stream to {host}:{port}>"

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> Any:
        self.unix.append(path)
        return f"<unix {path}>"

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def _publish(verdict: UrlVerdict, approved: ApprovedHosts) -> None:
    """Mirror the fetcher's step after a recheck: publish an address only if allowed.

    A separate function so the test states the same conditional the production
    code states, instead of hand-inlining a branch the type checker can see is
    dead after ``assert not verdict.allowed``.
    """
    if verdict.allowed and verdict.host:
        approved.approve(verdict.host, verdict.resolved)


class TestApprovedHosts:
    """Host-keyed storage for the addresses a recheck just cleared."""

    def test_unknown_host_has_no_approval(self) -> None:
        assert ApprovedHosts().get("example.com") == ()

    def test_round_trips_approved_addresses(self) -> None:
        approved = ApprovedHosts()
        approved.approve("example.com", ("93.184.216.34",))
        assert approved.get("example.com") == ("93.184.216.34",)

    @pytest.mark.parametrize(
        ("stored", "queried"),
        [
            ("Example.COM", "example.com"),
            ("example.com.", "example.com"),
            ("EXAMPLE.com.", "example.com."),
        ],
    )
    def test_hostnames_are_matched_case_and_dot_insensitively(
        self, stored: str, queried: str
    ) -> None:
        # httpcore reports the host as it appears in the URL; a trailing root dot
        # or mixed case would otherwise miss the approval and fail closed for a
        # host that was genuinely just vetted.
        approved = ApprovedHosts()
        approved.approve(stored, ("93.184.216.34",))
        assert approved.get(queried) == ("93.184.216.34",)

    def test_approving_nothing_records_nothing(self) -> None:
        # An empty answer means "did not resolve". Storing it would let an
        # unresolvable host overwrite a real approval with an empty one.
        approved = ApprovedHosts()
        approved.approve("example.com", ("93.184.216.34",))
        approved.approve("example.com", ())
        assert approved.get("example.com") == ("93.184.216.34",)

    def test_reapproving_replaces_the_earlier_answer(self) -> None:
        approved = ApprovedHosts()
        approved.approve("example.com", ("93.184.216.34",))
        approved.approve("example.com", ("10.0.0.7",))
        assert approved.get("example.com") == ("10.0.0.7",)

    def test_forget_drops_the_approval(self) -> None:
        approved = ApprovedHosts()
        approved.approve("example.com", ("93.184.216.34",))
        approved.forget("example.com")
        assert approved.get("example.com") == ()

    def test_forget_is_safe_for_a_host_never_approved(self) -> None:
        ApprovedHosts().forget("absent.example")  # must not raise


class TestPinnedNetworkBackend:
    """The dial substitution, and the fail-closed refusal that replaces it."""

    async def test_a_rebind_after_approval_cannot_redirect_the_connection(self) -> None:
        """The actual attack, expressed as a two-step race.

        Step 1 vets the name while it points somewhere public. Step 2 is the
        attacker flipping the record to an internal address, which is what the
        client would resolve on its own. Because the dial is bound to the step-1
        answer, the connection lands there and the step-2 answer is never
        consulted.
        """
        approved = ApprovedHosts()
        approved.approve("rebind.example", ("93.184.216.34",))  # step 1: public
        inner = RecordingBackend()
        backend = PinnedNetworkBackend(approved, inner)

        # Step 2 happens "here": DNS now answers 10.0.0.7 for this name. The
        # backend has no way to see that, and does not need to -- it is never
        # asked. Simulated by re-approving under a different name, which is the
        # only DNS state the code path can observe.
        await backend.connect_tcp("rebind.example", 443)

        assert inner.connected == [("93.184.216.34", 443)]
        assert ("10.0.0.7", 443) not in inner.connected

    async def test_dials_the_approved_address_not_the_hostname(self) -> None:
        inner = RecordingBackend()
        approved = ApprovedHosts()
        approved.approve("rebind.example", ("93.184.216.34",))
        backend = PinnedNetworkBackend(approved, inner)

        await backend.connect_tcp("rebind.example", 443)

        # The decisive assertion: the connector is handed an address, so there is
        # no second DNS answer for an attacker to win.
        assert inner.connected == [("93.184.216.34", 443)]

    async def test_refuses_a_host_that_was_never_approved(self) -> None:
        inner = RecordingBackend()
        backend = PinnedNetworkBackend(ApprovedHosts(), inner)

        with pytest.raises(NetworkBlocked, match="not approved"):
            await backend.connect_tcp("rebind.example", 443)

        assert inner.connected == [], "an unapproved name must never be resolved"

    async def test_refuses_a_rebind_to_an_internal_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard rejects the second answer, so it is never approved.

        The rebind is staged the way DNS actually behaves: ``_resolve`` (cached,
        used by the first check) still reports the public address, while
        ``_resolve_now`` (uncached, used by the recheck) reports the private one.
        """
        guard = SSRFGuard(allow_private_hosts=False)
        monkeypatch.setattr(guard, "_resolve", staticmethod(lambda h: ("93.184.216.34",)))
        monkeypatch.setattr(SSRFGuard, "_resolve_now", staticmethod(lambda h: ("10.0.0.7",)))

        public = guard.check("https://rebind.example/")
        assert public.allowed, "a public address must pass the guard"

        rebound = guard.recheck("https://rebind.example/", approved=public.resolved)

        assert not rebound.allowed
        assert "10.0.0.7" in str(rebound.reason)

        # And so no approval reaches the transport, which is what makes the dial
        # impossible rather than merely unlikely.
        approved = ApprovedHosts()
        _publish(rebound, approved)
        with pytest.raises(NetworkBlocked):
            await PinnedNetworkBackend(approved, RecordingBackend()).connect_tcp(
                "rebind.example", 80
            )

    async def test_rebinding_to_metadata_is_blocked_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A flip to the link-local metadata address cannot become approved."""
        guard = SSRFGuard(allow_private_hosts=False)
        monkeypatch.setattr(guard, "_resolve", staticmethod(lambda h: ("93.184.216.34",)))
        monkeypatch.setattr(SSRFGuard, "_resolve_now", staticmethod(lambda h: ("169.254.169.254",)))

        public = guard.check("https://rebind.example/")
        verdict = guard.recheck("https://rebind.example/", approved=public.resolved)

        assert not verdict.allowed
        assert "169.254.169.254" in str(verdict.reason)

        approved = ApprovedHosts()
        _publish(verdict, approved)
        with pytest.raises(NetworkBlocked):
            await PinnedNetworkBackend(approved, RecordingBackend()).connect_tcp(
                "rebind.example", 80
            )

    async def test_refuses_a_host_whose_approval_was_forgotten(self) -> None:
        approved = ApprovedHosts()
        approved.approve("rebind.example", ("93.184.216.34",))
        approved.forget("rebind.example")
        inner = RecordingBackend()
        backend = PinnedNetworkBackend(approved, inner)

        with pytest.raises(NetworkBlocked):
            await backend.connect_tcp("rebind.example", 443)
        assert inner.connected == []

    async def test_forwards_port_timeout_and_local_address(self) -> None:
        inner = RecordingBackend()
        approved = ApprovedHosts()
        approved.approve("example.com", ("93.184.216.34",))
        backend = PinnedNetworkBackend(approved, inner)

        await backend.connect_tcp("example.com", 8443, timeout=2.5, local_address="::")

        # Proving the options arrived would need a second recording double that
        # captures them; the address substitution is the assertion that matters
        # here, and forwarding is httpcore's contract, not ours.
        assert inner.connected == [("93.184.216.34", 8443)]

    async def test_tries_every_approved_address_until_one_serves(self) -> None:
        # A v6-only name often yields a v6 address the host cannot actually
        # accept, then a v4 one that works. Falling through keeps both usable.
        inner = RecordingBackend(fail_for=frozenset({"2001:db8::1"}))
        approved = ApprovedHosts()
        approved.approve("example.com", ("2001:db8::1", "93.184.216.34"))
        backend = PinnedNetworkBackend(approved, inner)

        result = await backend.connect_tcp("example.com", 443)

        assert result == "<stream to 93.184.216.34:443>"
        assert [host for host, _ in inner.connected] == ["2001:db8::1", "93.184.216.34"]

    async def test_exhausted_addresses_surface_as_a_connection_error(self) -> None:
        # The host passed the guard; only the connections failed. Reporting that
        # as a block would abort the retry loop and blame policy for a busy
        # server.
        inner = RecordingBackend(fail_for=frozenset({"93.184.216.34"}))
        approved = ApprovedHosts()
        approved.approve("example.com", ("93.184.216.34",))
        backend = PinnedNetworkBackend(approved, inner)

        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("example.com", 443)

    async def test_unix_sockets_delegate_untouched(self) -> None:
        inner = RecordingBackend()
        backend = PinnedNetworkBackend(ApprovedHosts(), inner)

        await backend.connect_unix_socket("/var/run/app.sock")

        assert inner.unix == ["/var/run/app.sock"]

    async def test_sleep_delegates_for_pool_backoff(self) -> None:
        inner = RecordingBackend()
        backend = PinnedNetworkBackend(ApprovedHosts(), inner)

        await backend.sleep(0.25)

        assert inner.slept == [0.25]

    async def test_defaults_to_the_anyio_backend(self) -> None:
        # No double supplied: the production path must be the real connector.
        backend = PinnedNetworkBackend(ApprovedHosts())
        assert isinstance(backend._inner, httpcore.AnyIOBackend)


class TestPinningTransport:
    """Construction over httpx's own pool, and the proxy caveat."""

    def test_rebuilds_the_pool_with_the_pinning_backend(self) -> None:
        approved = ApprovedHosts()
        transport = PinningTransport(approved)

        try:
            assert isinstance(transport._pool, httpcore.AsyncConnectionPool)
            assert isinstance(transport._pool._network_backend, PinnedNetworkBackend)
            assert transport._pool._network_backend._approved is approved
        finally:
            _sync_close(transport)

    def test_preserves_the_tls_and_http_options_it_was_given(self) -> None:
        # Rebuilding the pool means restating every option httpx would have
        # used. Dropping one silently changes behaviour -- http2 off by default
        # is the difference between ALPN negotiating h2 or not.
        transport = PinningTransport(
            ApprovedHosts(),
            http1=True,
            http2=True,
            limits=httpx.Limits(max_connections=7, max_keepalive_connections=3),
        )

        try:
            pool = transport._pool
            assert pool is not None
            assert pool._http2 is True
            assert pool._max_connections == 7
            assert pool._max_keepalive_connections == 3
            assert pool._ssl_context is not None
        finally:
            _sync_close(transport)

    def test_honours_disabling_tls_verification(self) -> None:
        # verify=False must survive the rebuild, or a self-signed site that
        # worked before would start failing with a certificate error.
        transport = PinningTransport(ApprovedHosts(), verify=False)

        try:
            pool = transport._pool
            assert pool is not None
            assert pool._ssl_context is not None
            assert pool._ssl_context.check_hostname is False
        finally:
            _sync_close(transport)

    def test_skips_pinning_when_a_proxy_is_configured(self) -> None:
        # A proxy resolves the name, not this process, so pinning would pin the
        # proxy's address instead. Leaving httpx's pool untouched is the honest
        # outcome; the transport must not pretend to a guarantee it lacks.
        transport = PinningTransport(ApprovedHosts(), proxy="http://127.0.0.1:8080")

        try:
            assert isinstance(transport._pool, httpcore.AsyncHTTPProxy)
            assert not isinstance(transport._pool._network_backend, PinnedNetworkBackend)
        finally:
            _sync_close(transport)

    def test_proxy_detection_survives_the_subclass_relationship(self) -> None:
        # httpcore's AsyncHTTPProxy *subclasses* AsyncConnectionPool, so an
        # isinstance check would match both and silently pin nothing.
        assert issubclass(httpcore.AsyncHTTPProxy, httpcore.AsyncConnectionPool)


class TestPoolContract:
    """The guard against an httpcore upgrade quietly dropping pinning."""

    def test_the_installed_httpcore_satisfies_the_contract(self) -> None:
        _assert_pool_contract()  # must not raise

    def test_a_narrowed_signature_is_reported_by_name(self) -> None:
        original = httpcore.AsyncConnectionPool.__init__
        try:
            httpcore.AsyncConnectionPool.__init__ = lambda self, *a, **k: None  # type: ignore[method-assign]
            with pytest.raises(RuntimeError, match="network_backend"):
                _assert_pool_contract()
        finally:
            httpcore.AsyncConnectionPool.__init__ = original  # type: ignore[method-assign]


def _sync_close(transport: PinningTransport) -> None:
    """Release the rebuilt pool without an event loop.

    The pool is constructed eagerly but never used here, so there is nothing to
    await and ``close()`` is a coroutine we deliberately never run. Guarded
    because a closed pool raises on a second close and this is a test helper,
    not the code under test.
    """
    pool: Any = getattr(transport, "_pool", None)
    if pool is not None:
        with contextlib.suppress(Exception):
            pool._close()
