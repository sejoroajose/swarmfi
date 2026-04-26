"""
tests/test_axl_client.py
Unit tests for core/axl_client.py using respx to mock the AXL HTTP bridge.
No live AXL nodes required.
"""

from __future__ import annotations

import json
import pytest
import respx
import httpx

from core.axl_client import AXLClient, NodeInfo, ReceivedMessage
from core.schema import AgentRole, MessageType, SwarmMessage, make_ping

VALID_PUBKEY = "a" * 64
OTHER_PUBKEY = "b" * 64
API_URL      = "http://127.0.0.1:9002"


def _make_topology_response(pubkey: str = VALID_PUBKEY) -> dict:
    return {"our_public_key": pubkey, "our_ipv6": "200:aabb::1"}


def _make_test_message() -> SwarmMessage:
    return make_ping(AgentRole.RESEARCHER, VALID_PUBKEY)


# ── topology ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestTopology:
    @pytest.mark.asyncio
    async def test_returns_node_info(self, respx_mock):
        respx_mock.get(f"{API_URL}/topology").mock(
            return_value=httpx.Response(200, json=_make_topology_response())
        )
        async with AXLClient(API_URL, "researcher") as client:
            info = await client.topology()

        assert isinstance(info, NodeInfo)
        assert info.public_key == VALID_PUBKEY
        assert info.api_url    == API_URL

    @pytest.mark.asyncio
    async def test_is_healthy_true_when_node_up(self, respx_mock):
        respx_mock.get(f"{API_URL}/topology").mock(
            return_value=httpx.Response(200, json=_make_topology_response())
        )
        async with AXLClient(API_URL) as client:
            assert await client.is_healthy() is True

    @pytest.mark.asyncio
    async def test_is_healthy_false_when_node_down(self, respx_mock):
        respx_mock.get(f"{API_URL}/topology").mock(
            side_effect=httpx.ConnectError("refused")
        )
        async with AXLClient(API_URL) as client:
            assert await client.is_healthy() is False

    @pytest.mark.asyncio
    async def test_raises_on_non_200(self, respx_mock):
        respx_mock.get(f"{API_URL}/topology").mock(
            return_value=httpx.Response(500)
        )
        async with AXLClient(API_URL) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.topology()


# ── send ──────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSend:
    @pytest.mark.asyncio
    async def test_send_posts_correct_headers_and_body(self, respx_mock):
        route = respx_mock.post(f"{API_URL}/send").mock(
            return_value=httpx.Response(200)
        )
        msg = _make_test_message()

        async with AXLClient(API_URL, "researcher") as client:
            await client.send(OTHER_PUBKEY, msg)

        assert route.called
        req = route.calls.last.request
        assert req.headers["X-Destination-Peer-Id"] == OTHER_PUBKEY

        # Verify body deserialises correctly
        decoded = SwarmMessage.decode(req.content)
        assert decoded.message_id == msg.message_id

    @pytest.mark.asyncio
    async def test_send_raises_on_non_200(self, respx_mock):
        respx_mock.post(f"{API_URL}/send").mock(
            return_value=httpx.Response(503)
        )
        async with AXLClient(API_URL) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.send(OTHER_PUBKEY, _make_test_message())


# ── recv ──────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestRecv:
    @pytest.mark.asyncio
    async def test_recv_returns_none_on_204(self, respx_mock):
        respx_mock.get(f"{API_URL}/recv").mock(
            return_value=httpx.Response(204)
        )
        async with AXLClient(API_URL) as client:
            result = await client.recv()
        assert result is None

    @pytest.mark.asyncio
    async def test_recv_returns_none_on_empty_body(self, respx_mock):
        respx_mock.get(f"{API_URL}/recv").mock(
            return_value=httpx.Response(200, content=b"")
        )
        async with AXLClient(API_URL) as client:
            result = await client.recv()
        assert result is None

    @pytest.mark.asyncio
    async def test_recv_deserialises_valid_message(self, respx_mock):
        msg = _make_test_message()
        respx_mock.get(f"{API_URL}/recv").mock(
            return_value=httpx.Response(
                200,
                content=msg.encode(),
                headers={"X-From-Peer-Id": OTHER_PUBKEY},
            )
        )
        async with AXLClient(API_URL) as client:
            received = await client.recv()

        assert isinstance(received, ReceivedMessage)
        assert received.from_pubkey          == OTHER_PUBKEY
        assert received.message.message_id   == msg.message_id
        assert received.message.message_type == MessageType.PING

    @pytest.mark.asyncio
    async def test_recv_raises_on_invalid_json(self, respx_mock):
        respx_mock.get(f"{API_URL}/recv").mock(
            return_value=httpx.Response(200, content=b"not json at all")
        )
        async with AXLClient(API_URL) as client:
            with pytest.raises(ValueError, match="Invalid SwarmMessage"):
                await client.recv()

    @pytest.mark.asyncio
    async def test_recv_returns_none_on_timeout(self, respx_mock):
        respx_mock.get(f"{API_URL}/recv").mock(
            side_effect=httpx.TimeoutException("timeout")
        )
        async with AXLClient(API_URL) as client:
            result = await client.recv()
        assert result is None


# ── context manager guard ─────────────────────────────────────────────────────

@pytest.mark.unit
class TestContextManagerGuard:
    @pytest.mark.asyncio
    async def test_raises_if_not_entered(self):
        client = AXLClient(API_URL)
        with pytest.raises(RuntimeError, match="context manager"):
            await client.topology()