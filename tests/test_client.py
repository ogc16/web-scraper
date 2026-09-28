from __future__ import annotations

import httpx
import pytest

from awsa.errors import NetworkBlocked
from awsa.net.client import NetworkOffTransport, build_async_client


class TestNetworkOffTransport:
    async def test_it_refuses_every_request(self) -> None:
        transport = NetworkOffTransport()
        request = httpx.Request("GET", "https://example.com/")
        with pytest.raises(NetworkBlocked) as excinfo:
            await transport.handle_async_request(request)
        assert "example.com" in str(excinfo.value)

    async def test_the_reason_appears_exactly_once(self) -> None:
        # `AwsaError.__str__` appends the detail, so putting the reason in both
        # the message and the detail would print it twice.
        transport = NetworkOffTransport()
        request = httpx.Request("GET", "https://example.com/")
        with pytest.raises(NetworkBlocked) as excinfo:
            await transport.handle_async_request(request)
        assert str(excinfo.value).count("disabled") == 1

    async def test_it_refuses_post_bodies_too(self) -> None:
        # Refusing only GETs would leave a data-exfiltration path open.
        transport = NetworkOffTransport()
        request = httpx.Request("POST", "https://example.com/", json={"k": "v"})
        with pytest.raises(NetworkBlocked):
            await transport.handle_async_request(request)

    async def test_closing_it_is_a_no_op(self) -> None:
        await NetworkOffTransport().aclose()


class TestBuildAsyncClient:
    async def test_a_normal_client_is_untouched(self) -> None:
        async with build_async_client(network_enabled=True) as client:
            assert not isinstance(client._transport, NetworkOffTransport)

    async def test_it_injects_the_blocking_transport(self) -> None:
        async with build_async_client(network_enabled=False) as client:
            assert isinstance(client._transport, NetworkOffTransport)

    async def test_it_overrides_a_caller_supplied_transport(self) -> None:
        # A caller-supplied transport is a real socket, so honouring it while
        # promising no network would be a hole in the guarantee.
        real = httpx.MockTransport(lambda request: httpx.Response(200))
        async with build_async_client(network_enabled=False, transport=real) as client:
            with pytest.raises(NetworkBlocked):
                await client.get("https://example.com/")

    async def test_a_blocked_client_raises_instead_of_connecting(self) -> None:
        async with build_async_client(network_enabled=False) as client:
            with pytest.raises(NetworkBlocked):
                await client.get("https://example.com/")

    async def test_an_enabled_client_reaches_its_transport(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
        async with build_async_client(network_enabled=True, transport=transport) as client:
            response = await client.get("https://example.com/")
        assert response.status_code == 200
