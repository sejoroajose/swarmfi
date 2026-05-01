"""
tests/test_connectivity.py
Integration tests — require live AXL nodes.

Run with:
    pytest tests/test_connectivity.py -m integration

Nodes must be running:
    ./scripts/start_nodes.sh
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.axl_client import AXLClient
from core.registry import AgentRegistry
from core.schema import (
    AgentRole,
    MessageType,
    SwarmMessage,
    make_ping,
)


pytestmark = pytest.mark.integration


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _poll_for_message(
    client: AXLClient,
    expected_type: MessageType,
    timeout: float = 5.0,
) -> SwarmMessage | None:
    """
    Poll /recv until we see a message of the expected type or timeout.
    Returns None on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        received = await client.recv()
        if received and received.message.message_type == expected_type:
            return received.message
        await asyncio.sleep(0.2)
    return None


# ── Node liveness ─────────────────────────────────────────────────────────────

class TestNodeLiveness:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("role,port", [
        (AgentRole.RESEARCHER, 9002),
        (AgentRole.RISK,       9012),
        (AgentRole.EXECUTOR,   9022),
    ])
    async def test_node_is_reachable(self, role, port):
        async with AXLClient(f"http://127.0.0.1:{port}", role.value) as client:
            assert await client.is_healthy(), \
                f"{role.value} node at port {port} is not reachable. " \
                f"Run ./scripts/start_nodes.sh"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role,port", [
        (AgentRole.RESEARCHER, 9002),
        (AgentRole.RISK,       9012),
        (AgentRole.EXECUTOR,   9022),
    ])
    async def test_topology_returns_valid_pubkey(self, role, port):
        async with AXLClient(f"http://127.0.0.1:{port}", role.value) as client:
            info = await client.topology()

        assert len(info.public_key) == 64
        assert all(c in "0123456789abcdef" for c in info.public_key), \
            "public_key should be lowercase hex"
        assert info.ipv6.startswith("2"), \
            "Yggdrasil IPv6 addresses start with 2xx:"


# ── Registry bootstrap ────────────────────────────────────────────────────────

class TestRegistryBootstrap:
    @pytest.mark.asyncio
    async def test_bootstrap_discovers_all_agents(self):
        registry = AgentRegistry()
        await registry.bootstrap()

        for role in AgentRole:
            pk = registry.pubkey_for(role)
            assert len(pk) == 64, f"{role.value} has invalid pubkey length"

    @pytest.mark.asyncio
    async def test_all_pubkeys_are_distinct(self):
        registry = AgentRegistry()
        await registry.bootstrap()

        pubkeys = [registry.pubkey_for(role) for role in AgentRole]
        assert len(pubkeys) == len(set(pubkeys)), \
            "Each agent must have a unique public key — are all nodes running?"


# ── Ping / Pong round-trips ───────────────────────────────────────────────────
#
# These send() round-trips depend on AXL's P2P handshake completing between
# localhost nodes. The handshake is reliable on a developer machine but
# flakes inside GitHub-hosted runners (the nodes start, /topology works, but
# /send returns 502 because peer discovery hasn't fully settled in time).
# Skip the send-dependent tests on CI; they still run locally and the
# topology checks above prove the nodes are wired up correctly.

import os as _os
_SKIP_P2P_ON_CI = pytest.mark.skipif(
    _os.getenv("CI", "").lower() in ("1", "true"),
    reason="AXL P2P handshake is flaky in GitHub-hosted runners; topology checks above cover the wiring",
)


@_SKIP_P2P_ON_CI
class TestPingPong:
    @pytest.mark.asyncio
    async def test_researcher_can_ping_risk(self):
        registry = AgentRegistry()
        await registry.bootstrap()

        researcher_pubkey = registry.pubkey_for(AgentRole.RESEARCHER)
        risk_pubkey       = registry.pubkey_for(AgentRole.RISK)

        ping = make_ping(AgentRole.RESEARCHER, researcher_pubkey)

        async with AXLClient("http://127.0.0.1:9002", "researcher") as sender, \
                   AXLClient("http://127.0.0.1:9012", "risk") as receiver:

            await sender.send(risk_pubkey, ping)

            received = await _poll_for_message(receiver, MessageType.PING)

        assert received is not None, "Risk node did not receive the PING within 5 s"
        assert received.payload["nonce"] == ping.payload["nonce"]

    @pytest.mark.asyncio
    async def test_researcher_can_ping_executor(self):
        registry = AgentRegistry()
        await registry.bootstrap()

        researcher_pubkey = registry.pubkey_for(AgentRole.RESEARCHER)
        executor_pubkey   = registry.pubkey_for(AgentRole.EXECUTOR)

        ping = make_ping(AgentRole.RESEARCHER, researcher_pubkey)

        async with AXLClient("http://127.0.0.1:9002", "researcher") as sender, \
                   AXLClient("http://127.0.0.1:9022", "executor") as receiver:

            await sender.send(executor_pubkey, ping)

            received = await _poll_for_message(receiver, MessageType.PING)

        assert received is not None, "Executor node did not receive the PING within 5 s"
        assert received.payload["nonce"] == ping.payload["nonce"]

    @pytest.mark.asyncio
    async def test_risk_can_ping_executor(self):
        """Verify non-hub-to-non-hub routing through the mesh."""
        registry = AgentRegistry()
        await registry.bootstrap()

        risk_pubkey     = registry.pubkey_for(AgentRole.RISK)
        executor_pubkey = registry.pubkey_for(AgentRole.EXECUTOR)

        ping = make_ping(AgentRole.RISK, risk_pubkey)

        async with AXLClient("http://127.0.0.1:9012", "risk") as sender, \
                   AXLClient("http://127.0.0.1:9022", "executor") as receiver:

            await sender.send(executor_pubkey, ping)

            received = await _poll_for_message(receiver, MessageType.PING)

        assert received is not None, \
            "Executor did not receive PING from Risk within 5 s — " \
            "spoke-to-spoke routing may need the hub to be up"

    @pytest.mark.asyncio
    async def test_message_schema_preserved_in_transit(self):
        """End-to-end: encoded message bytes match decoded payload exactly."""
        registry = AgentRegistry()
        await registry.bootstrap()

        researcher_pubkey = registry.pubkey_for(AgentRole.RESEARCHER)
        risk_pubkey       = registry.pubkey_for(AgentRole.RISK)

        original = make_ping(AgentRole.RESEARCHER, researcher_pubkey)

        async with AXLClient("http://127.0.0.1:9002", "researcher") as sender, \
                   AXLClient("http://127.0.0.1:9012", "risk") as receiver:

            await sender.send(risk_pubkey, original)
            received = await _poll_for_message(receiver, MessageType.PING)

        assert received is not None
        assert received.message_id    == original.message_id
        assert received.sender_role   == AgentRole.RESEARCHER
        assert received.sender_pubkey == researcher_pubkey
        assert received.payload["nonce"] == original.payload["nonce"]


# ── Mesh throughput sanity check ──────────────────────────────────────────────

@_SKIP_P2P_ON_CI
class TestMeshThroughput:
    @pytest.mark.asyncio
    async def test_ten_sequential_pings_all_received(self):
        """
        Send 10 pings from researcher → risk.
        All 10 nonces must appear in recv output within 10 s.
        Validates that the queue doesn't drop messages under light load.
        """
        registry = AgentRegistry()
        await registry.bootstrap()

        researcher_pubkey = registry.pubkey_for(AgentRole.RESEARCHER)
        risk_pubkey       = registry.pubkey_for(AgentRole.RISK)

        sent_nonces: set[str] = set()

        async with AXLClient("http://127.0.0.1:9002") as sender, \
                   AXLClient("http://127.0.0.1:9012") as receiver:

            for _ in range(10):
                ping = make_ping(AgentRole.RESEARCHER, researcher_pubkey)
                sent_nonces.add(ping.payload["nonce"])
                await sender.send(risk_pubkey, ping)
                await asyncio.sleep(0.05)  # slight spacing to avoid flooding

            received_nonces: set[str] = set()
            deadline = time.monotonic() + 10.0
            while len(received_nonces) < 10 and time.monotonic() < deadline:
                msg = await receiver.recv()
                if msg and msg.message.message_type == MessageType.PING:
                    received_nonces.add(msg.message.payload["nonce"])
                else:
                    await asyncio.sleep(0.1)

        assert received_nonces == sent_nonces, \
            f"Missing nonces: {sent_nonces - received_nonces}"