"""SSRF guard: every way a URL can point somewhere it should not.

These are table-driven rather than example-driven because the failure mode is
exactly the case nobody enumerated.
"""

from __future__ import annotations

import ipaddress

import pytest

from awsa.errors import NetworkBlocked, UnsafeURLError
from awsa.net.guard import SSRFGuard, is_public_address

# Reserved, private and special-purpose space that a crawler must never reach.
BLOCKED_ADDRESSES = [
    # IPv4
    "0.0.0.0",  # noqa: S104 - test data for the "unspecified address" case, not a bind
    "0.255.255.255",
    "10.0.0.1",
    "10.255.255.255",
    "100.64.0.1",
    "100.127.255.255",
    "127.0.0.1",
    "127.1.2.3",
    "169.254.0.1",
    "169.254.169.254",  # cloud instance metadata
    "172.16.0.1",
    "172.20.10.1",
    "172.31.255.255",
    "192.0.0.1",
    "192.0.2.1",
    "192.88.99.1",
    "192.168.0.1",
    "192.168.1.1",
    "198.18.0.1",
    "198.19.255.255",
    "198.51.100.1",
    "203.0.113.1",
    "224.0.0.1",
    "239.255.255.255",
    "240.0.0.1",
    "255.255.255.255",
    # IPv6
    "::",
    "::1",
    "100::1",
    "2001::1",
    "2001:db8::1",
    "fc00::1",
    "fd12:3456:789a::1",
    "fe80::1",
    "ff02::1",
    # IPv4 tunnelled inside IPv6 — the ones that slip past naive checks.
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:192.168.0.1",
    "::ffff:169.254.169.254",
    "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
    "2002:7f00:0001::",  # 6to4 wrapping 127.0.0.1
    "2002:a00:1::",  # 6to4 wrapping 10.0.0.1
]

ALLOWED_ADDRESSES = ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2606:2800:220:1::1"]


class TestIsPublicAddress:
    @pytest.mark.parametrize("raw", BLOCKED_ADDRESSES)
    def test_rejects_special_use_space(self, raw: str) -> None:
        assert not is_public_address(ipaddress.ip_address(raw)), raw

    @pytest.mark.parametrize("raw", ALLOWED_ADDRESSES)
    def test_accepts_public_space(self, raw: str) -> None:
        assert is_public_address(ipaddress.ip_address(raw)), raw

    def test_covers_every_rfc1918_block(self) -> None:
        for raw in ("10.1.2.3", "172.16.5.5", "172.31.255.254", "192.168.42.42"):
            assert not is_public_address(ipaddress.ip_address(raw)), raw


class TestSchemeAndSyntax:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://example.com/",
            "ftp://example.com/x",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "dict://example.com:11211/",
            "",
            "   ",
        ],
    )
    def test_rejects_non_http_schemes(self, url: str) -> None:
        assert not SSRFGuard().check(url)

    def test_rejects_embedded_credentials(self) -> None:
        verdict = SSRFGuard().check("http://user:secret@example.com/")
        assert not verdict.allowed
        assert "credential" in verdict.reason.lower()

    def test_rejects_whitespace_and_control_characters(self) -> None:
        for url in ("http://exa\nmple.com/", "http://example.com/ a", "http://ex\x00.com/"):
            assert not SSRFGuard().check(url)

    def test_rejects_missing_host(self) -> None:
        assert not SSRFGuard().check("http:///path")

    def test_rejects_unresolvable_host(self) -> None:
        verdict = SSRFGuard().check("https://this-host-does-not-exist.invalid/")
        assert not verdict.allowed
        assert "resolve" in verdict.reason.lower()

    def test_rejects_invalid_port(self) -> None:
        assert not SSRFGuard().check("http://example.com:99999/")

    def test_accepts_a_public_url(self) -> None:
        assert SSRFGuard().check("https://1.1.1.1/").allowed


class TestHostLists:
    """Host lists are checked before any DNS lookup.

    These use IP literals so the suite stays hermetic — a rejection must be
    reachable without a resolver, which is also what makes the check cheap
    enough to sit in front of every request.
    """

    def test_blocklist_wins_over_allowlist(self) -> None:
        guard = SSRFGuard(allowlist=["1.1.1.0/24", "1.1.1.1"], blocklist=["1.1.1.2"])
        assert not guard.check("https://1.1.1.2/").allowed
        assert guard.check("https://1.1.1.1/").allowed

    def test_allowlist_excludes_everything_else(self) -> None:
        guard = SSRFGuard(allowlist=["1.1.1.1"])
        assert not guard.check("https://8.8.8.8/").allowed

    def test_allowlist_matches_subdomains(self) -> None:
        guard = SSRFGuard(allowlist=["1.1.1.1"])
        # Subdomain matching is name-based; exercise it through the name path.
        assert guard._host_passes_lists("docs.1.1.1.1") == ""
        assert guard._host_passes_lists("1.1.1.1") == ""

    def test_allowlist_does_not_match_suffix_tricks(self) -> None:
        guard = SSRFGuard(allowlist=["example.com"])
        assert "not in the allowlist" in guard._host_passes_lists("notexample.com")
        assert "not in the allowlist" in guard._host_passes_lists("example.com.evil.net")

    def test_blocklist_rejection_needs_no_resolver(self) -> None:
        guard = SSRFGuard(blocklist=["metadata.internal"])
        verdict = guard.check("http://metadata.internal/latest/meta-data/")
        assert not verdict.allowed
        assert "blocklist" in verdict.reason


class TestPrivateOverride:
    def test_allows_loopback_when_enabled(self) -> None:
        assert SSRFGuard(allow_private_hosts=True).check("http://127.0.0.1:8080/").allowed

    def test_scheme_still_enforced_even_with_override(self) -> None:
        guard = SSRFGuard(allow_private_hosts=True)
        assert not guard.check("file:///etc/passwd")

    def test_credentials_still_rejected_even_with_override(self) -> None:
        guard = SSRFGuard(allow_private_hosts=True)
        assert not guard.check("http://a:b@127.0.0.1/")


class TestRebindingDefence:
    """`recheck` closes the window between validating a name and connecting.

    The HTTP client resolves the hostname itself, so a name that was public at
    check time can point somewhere private by the time the socket opens. The
    only place to catch that is a second, uncached resolution immediately
    before the request.
    """

    def test_unchanged_answer_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guard = SSRFGuard()
        monkeypatch.setattr(guard, "_resolve", staticmethod(lambda h: ("93.184.216.34",)))
        monkeypatch.setattr(SSRFGuard, "_resolve_now", staticmethod(lambda h: ("93.184.216.34",)))
        verdict = guard.recheck("http://rebind.test/", approved=("93.184.216.34",))
        assert verdict.allowed

    def test_rebound_to_a_private_address_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guard = SSRFGuard()
        # First answer looks public, so the URL is approved...
        monkeypatch.setattr(guard, "_resolve", staticmethod(lambda h: ("93.184.216.34",)))
        approved = guard.check("http://rebind.test/").resolved
        assert approved == ("93.184.216.34",)
        # ...then the attacker repoints the name before we connect.
        monkeypatch.setattr(SSRFGuard, "_resolve_now", staticmethod(lambda h: ("127.0.0.1",)))
        verdict = guard.recheck("http://rebind.test/", approved=approved)
        assert not verdict.allowed
        assert "re-resolved" in verdict.reason

    def test_rebound_to_a_metadata_endpoint_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guard = SSRFGuard()
        monkeypatch.setattr(guard, "_resolve", staticmethod(lambda h: ("93.184.216.34",)))
        approved = guard.check("http://rebind.test/").resolved
        monkeypatch.setattr(SSRFGuard, "_resolve_now", staticmethod(lambda h: ("169.254.169.254",)))
        assert not guard.recheck("http://rebind.test/", approved=approved).allowed

    def test_a_superset_of_the_approved_set_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even a *new* public address is refused: the approved set is a
        # whitelist for this request, not a lower bound.
        guard = SSRFGuard()
        monkeypatch.setattr(guard, "_resolve", staticmethod(lambda h: ("93.184.216.34",)))
        approved = guard.check("http://rebind.test/").resolved
        monkeypatch.setattr(
            SSRFGuard, "_resolve_now", staticmethod(lambda h: ("93.184.216.34", "10.1.2.3"))
        )
        assert not guard.recheck("http://rebind.test/", approved=approved).allowed

    def test_dns_failure_before_connecting_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guard = SSRFGuard()
        monkeypatch.setattr(guard, "_resolve", staticmethod(lambda h: ("93.184.216.34",)))
        approved = guard.check("http://rebind.test/").resolved
        monkeypatch.setattr(SSRFGuard, "_resolve_now", staticmethod(lambda h: ()))
        verdict = guard.recheck("http://rebind.test/", approved=approved)
        assert not verdict.allowed
        assert "stopped resolving" in verdict.reason

    def test_ip_literals_skip_re_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A literal cannot be rebound, so it must not pay for a second lookup.
        guard = SSRFGuard(allow_private_hosts=True)
        calls: list[str] = []

        def _record(host: str) -> tuple[str, ...]:
            calls.append(host)
            return ()

        monkeypatch.setattr(SSRFGuard, "_resolve_now", staticmethod(_record))
        assert guard.recheck("http://127.0.0.1:9/", approved=("127.0.0.1",)).allowed
        assert calls == []

    def test_no_approved_set_falls_back_to_a_plain_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guard = SSRFGuard()
        monkeypatch.setattr(guard, "_resolve", staticmethod(lambda h: ("93.184.216.34",)))
        monkeypatch.setattr(SSRFGuard, "_resolve_now", staticmethod(lambda h: ("127.0.0.1",)))
        assert guard.recheck("http://rebind.test/").allowed

    def test_a_blocked_url_is_still_blocked_by_recheck(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guard = SSRFGuard()
        assert not guard.recheck("http://127.0.0.1/", approved=("127.0.0.1",)).allowed


class TestExceptions:
    def test_assert_allowed_raises_unsafe_url_error(self) -> None:
        with pytest.raises(UnsafeURLError):
            SSRFGuard().assert_allowed("http://127.0.0.1/")

    def test_assert_network_allowed_raises_network_blocked(self) -> None:
        with pytest.raises(NetworkBlocked):
            SSRFGuard().assert_network_allowed("http://10.0.0.1/")

    def test_error_preserves_the_reason(self) -> None:
        with pytest.raises(UnsafeURLError) as excinfo:
            SSRFGuard().assert_allowed("file:///etc/passwd")
        assert "scheme" in (excinfo.value.detail or "")
