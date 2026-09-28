"""SSRF guard: reject URLs that would reach unintended network destinations.

A scraper that accepts arbitrary URLs is a server-side request forgery
primitive. This module validates scheme, credentials, and — critically —
resolves the hostname to reject loopback, link-local, private and
carrier-grade-NAT addresses. DNS rebinding is mitigated by resolving the host
here *and* pinning the resolved address for the connection via httpx's
``transport`` hooks where supported.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

from ..errors import NetworkBlocked, UnsafeURLError

__all__ = ["SSRFGuard", "UrlVerdict", "is_public_address"]

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}

#: IANA IPv4 Special-Purpose Address Registry. Anything here is denied.
_BLOCKED_V4: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.IPv4Network(net)
    for net in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # private (RFC 1918)
        "100.64.0.0/10",  # shared address space / CGNAT (RFC 6598)
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link local, incl. cloud metadata at 169.254.169.254
        "172.16.0.0/12",  # private (RFC 1918)
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # 6to4 relay anycast (deprecated)
        "192.168.0.0/16",  # private (RFC 1918)
        "198.18.0.0/15",  # benchmarking (RFC 2544)
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, incl. 255.255.255.255 broadcast
    )
)

#: IANA IPv6 Special-Purpose Address Registry. Anything here is denied.
_BLOCKED_V6: tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.IPv6Network(net)
    for net in (
        "::/128",  # unspecified
        "::1/128",  # loopback
        "64:ff9b::/96",  # NAT64
        "64:ff9b:1::/48",  # local-use NAT64
        "100::/64",  # discard-only
        "2001::/23",  # IETF protocol assignments
        "2001:db8::/32",  # documentation
        "fc00::/7",  # unique local
        "fe80::/10",  # link local
        "ff00::/8",  # multicast
    )
)

#: Address blocks that carry an embedded IPv4 address. The embedded address is
#: what actually gets connected to, so it must be checked on its own.
_V4_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


def _embedded_v4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Extract the IPv4 address tunnelled inside an IPv6 address, if any.

    Without this, ``http://[::ffff:127.0.0.1]/`` reaches loopback and
    ``http://[::ffff:a00:1]/`` reaches ``10.0.0.1`` while looking like a
    perfectly ordinary IPv6 literal.
    """
    if addr in _V4_MAPPED:
        mapped = addr.ipv4_mapped
        return mapped if mapped is not None else ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr in _NAT64:
        # Well-known prefix: the last 32 bits are the IPv4 address.
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr in _SIX_TO_FOUR:
        # 6to4: bits 16..48 hold the IPv4 address, in network byte order.
        return ipaddress.IPv4Address((int(addr) >> 80) & 0xFFFFFFFF)
    return None


def is_public_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for addresses safe to reach from a server-side crawler.

    Deny-by-default: an address is reachable only if it falls inside globally
    routable space. ``is_global`` alone is not trusted because its meaning has
    shifted between CPython versions, so the special-purpose registries are
    spelled out explicitly above.
    """
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = _embedded_v4(addr)
        if embedded is not None:
            # ::ffff:127.0.0.1 and 2002:7f00:0001:: are loopback in disguise.
            return is_public_address(embedded)
        if any(addr in net for net in _BLOCKED_V6):
            return False
        return addr.is_global

    if any(addr in net for net in _BLOCKED_V4):
        return False
    return addr.is_global


@dataclass(frozen=True, slots=True)
class UrlVerdict:
    """Outcome of inspecting a URL."""

    allowed: bool
    reason: str = ""
    host: str = ""
    resolved: tuple[str, ...] = ()
    scheme: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class SSRFGuard:
    """Validates URLs before any socket is opened.

    Args:
        allow_private_hosts: When true, private/loopback destinations are
            permitted. Required for testing against a local fixture server.
        allowlist: If non-empty, only these hosts (or their subdomains) pass.
        blocklist: Hosts that always fail, even if allowlisted.
    """

    def __init__(
        self,
        *,
        allow_private_hosts: bool = False,
        allowlist: Iterable[str] = (),
        blocklist: Iterable[str] = (),
    ) -> None:
        self.allow_private_hosts = allow_private_hosts
        self.allowlist = frozenset(h.lower().lstrip(".") for h in allowlist if h.strip())
        self.blocklist = frozenset(h.lower().lstrip(".") for h in blocklist if h.strip())

    @staticmethod
    @lru_cache(maxsize=2048)
    def _resolve(host: str) -> tuple[str, ...]:
        return SSRFGuard._resolve_now(host)

    @staticmethod
    def _resolve_now(host: str) -> tuple[str, ...]:
        """Resolve a host right now, bypassing the cache."""
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError, OSError):
            return ()
        # sockaddr's first element is typed as str | int because AF_UNIX-style
        # tuples are allowed, so normalise to str before de-duplicating. The
        # dedupe has to happen on the strings, not on a stringified set.
        return tuple(dict.fromkeys(str(info[4][0]) for info in infos))

    def _host_passes_lists(self, host: str) -> str:
        bare = host.lower().rstrip(".")
        if any(bare == b or bare.endswith(f".{b}") for b in self.blocklist):
            return f"host {bare!r} is blocklisted"
        if self.allowlist and not any(bare == a or bare.endswith(f".{a}") for a in self.allowlist):
            return f"host {bare!r} is not in the allowlist"
        return ""

    def check(self, url: str) -> UrlVerdict:
        """Return a verdict for ``url`` without raising."""
        raw = url.strip()
        if not raw:
            return UrlVerdict(False, "empty URL", scheme="")
        if any(ch in raw for ch in ("\n", "\r", "\t", " ", "\x00")):
            return UrlVerdict(False, "URL contains whitespace or control characters")

        try:
            parts = urlsplit(raw)
        except ValueError as exc:
            return UrlVerdict(False, f"unparseable URL: {exc}")

        scheme = parts.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            return UrlVerdict(False, f"scheme {scheme!r} not allowed", scheme=scheme)
        if parts.username or parts.password:
            return UrlVerdict(False, "URLs with embedded credentials are rejected", scheme=scheme)

        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return UrlVerdict(False, "URL has no host", scheme=scheme)

        list_reason = self._host_passes_lists(host)
        if list_reason:
            return UrlVerdict(False, list_reason, host=host, scheme=scheme)

        try:
            port = parts.port or _DEFAULT_PORTS[scheme]
        except ValueError:
            return UrlVerdict(False, "invalid port", host=host, scheme=scheme)
        if not 1 <= port <= 65535:
            return UrlVerdict(False, f"port {port} out of range", host=host, scheme=scheme)

        resolved = self._resolve(host)
        if not resolved:
            return UrlVerdict(False, f"host {host!r} does not resolve", host=host, scheme=scheme)

        if not self.allow_private_hosts:
            for literal in resolved:
                try:
                    addr = ipaddress.ip_address(literal)
                except ValueError:
                    return UrlVerdict(
                        False, f"unparseable DNS answer {literal!r}", host=host, scheme=scheme
                    )
                if not is_public_address(addr):
                    return UrlVerdict(
                        False,
                        f"{host!r} resolves to non-public address {literal}",
                        host=host,
                        resolved=resolved,
                        scheme=scheme,
                    )

        return UrlVerdict(True, host=host, resolved=resolved, scheme=scheme)

    def assert_allowed(self, url: str) -> UrlVerdict:
        """Like :meth:`check` but raises :class:`UnsafeURLError` on rejection."""
        verdict = self.check(url)
        if not verdict.allowed:
            raise UnsafeURLError(f"refusing to fetch {url!r}", detail=verdict.reason)
        return verdict

    def recheck(self, url: str, *, approved: tuple[str, ...] = ()) -> UrlVerdict:
        """Re-resolve ``url`` immediately before a socket is opened.

        A hostname that was public a moment ago can resolve to a private address
        by the time the client connects - DNS rebinding. Validating once and then
        letting the HTTP client resolve the name again leaves that window open, so
        the answer is taken a second time here, with the cache bypassed, and
        compared against the set that was approved.

        Args:
            url: The URL about to be requested.
            approved: Addresses returned by the earlier :meth:`check`. When
                empty, the fresh answer is validated on its own merits.

        Returns:
            A verdict. On a changed answer the verdict is a rejection, so the
            caller must abort rather than connect.
        """
        verdict = self.check(url)
        if not verdict.allowed:
            return verdict
        if not approved:
            return verdict
        # Literal IPs never move, so there is nothing to re-resolve.
        try:
            ipaddress.ip_address(verdict.host)
        except ValueError:
            pass
        else:
            return verdict

        fresh = self._resolve_now(verdict.host)
        if not fresh:
            return UrlVerdict(
                False,
                f"host {verdict.host!r} stopped resolving before the request",
                host=verdict.host,
                scheme=verdict.scheme,
            )
        if not set(fresh) <= set(approved):
            return UrlVerdict(
                False,
                f"host {verdict.host!r} re-resolved to {fresh} (approved {approved})",
                host=verdict.host,
                resolved=fresh,
                scheme=verdict.scheme,
            )
        return verdict

    def assert_network_allowed(self, url: str) -> UrlVerdict:
        """Alias of :meth:`assert_allowed` that signals a policy, not a syntax, block."""
        try:
            return self.assert_allowed(url)
        except UnsafeURLError as exc:
            raise NetworkBlocked(exc.message, detail=exc.detail) from exc
