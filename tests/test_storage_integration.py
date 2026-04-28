"""
tests/test_storage_integration.py
Integration tests — require ZG_PRIVATE_KEY env var pointing to a funded
Galileo testnet wallet.

Run with:
    ZG_PRIVATE_KEY=<your_key> python3 -m pytest tests/test_storage_integration.py -m integration -v

These tests are SKIPPED automatically in CI unless ZG_PRIVATE_KEY is set.
They hit the real 0G testnet and cost small amounts of test tokens.
"""

import os
import pytest

from core.storage.agent_memory import make_agent_memory
from core.storage.client import ZeroGClient
from core.storage.kv import SwarmKV
from core.storage.log import SwarmLog
from core.storage.models import AgentStatus, LogEventType, KVKey, SwarmState

pytestmark = pytest.mark.integration

# Skip all tests in this file if ZG_PRIVATE_KEY not set
PRIVATE_KEY = os.getenv("ZG_PRIVATE_KEY", "").strip()

skip_without_key = pytest.mark.skipif(
    not PRIVATE_KEY,
    reason="ZG_PRIVATE_KEY not set — skipping live 0G testnet tests",
)


@pytest.fixture
async def live_client():
    client = ZeroGClient.from_env()
    assert client.is_live, "Expected live client but got in-memory mode"
    async with client:
        yield client


@pytest.mark.asyncio
@skip_without_key
async def test_live_client_is_live():
    client = ZeroGClient.from_env()
    assert client.is_live


@pytest.mark.asyncio
@skip_without_key
async def test_upload_and_download_roundtrip(live_client):
    """Upload bytes, download by root hash, verify identical."""
    data = b"SwarmFi integration test payload"
    result = await live_client.upload(data)

    assert result.root_hash is not None
    assert len(result.root_hash) > 0

    downloaded = await live_client.download(result.root_hash)
    assert downloaded == data


@pytest.mark.asyncio
@skip_without_key
async def test_upload_json_and_download_json(live_client):
    obj = {"swarm": "swarmfi", "stage": 2, "network": "0g-galileo"}
    result = await live_client.upload_json(obj)

    downloaded = await live_client.download_json(result.root_hash)
    assert downloaded == obj


@pytest.mark.asyncio
@skip_without_key
async def test_kv_set_get_on_testnet(live_client):
    kv = SwarmKV(live_client)

    await kv.set("integration:test:key", b"integration test value")
    result = await kv.get("integration:test:key")

    assert result == b"integration test value"


@pytest.mark.asyncio
@skip_without_key
async def test_swarm_state_roundtrip_on_testnet(live_client):
    kv = SwarmKV(live_client)

    swarm = SwarmState(swarm_metadata={"test": "integration"})
    await kv.set(KVKey.SWARM_STATE, swarm.encode())

    raw     = await kv.get(KVKey.SWARM_STATE)
    decoded = SwarmState.decode(raw)
    assert decoded.swarm_metadata["test"] == "integration"


@pytest.mark.asyncio
@skip_without_key
async def test_log_append_and_read_on_testnet(live_client):
    kv   = SwarmKV(live_client)
    slog = SwarmLog(live_client, kv)

    entry = await slog.append(
        LogEventType.MARKET_SIGNAL,
        "researcher",
        data={"token": "ETH", "source": "integration_test"},
    )

    assert entry.root_hash is not None

    # Fetch entry back by root hash
    fetched = await slog.get_entry(entry.root_hash)
    assert fetched.data["source"] == "integration_test"


@pytest.mark.asyncio
@skip_without_key
async def test_manifest_persists_across_kv_instances(live_client):
    """
    Simulate agent restart: write with kv1, reload with kv2.
    """
    kv1 = SwarmKV(live_client)
    await kv1.set("restart:test", b"survived restart")
    manifest_root = kv1.manifest_root
    assert manifest_root is not None

    kv2 = SwarmKV(live_client)
    await kv2.load_manifest(manifest_root)
    result = await kv2.get("restart:test")
    assert result == b"survived restart"


@pytest.mark.asyncio
@skip_without_key
async def test_agent_memory_full_cycle_on_testnet(live_client):
    """
    Full agent memory cycle: start → update status → log events → read back.
    """
    memory = make_agent_memory("researcher", live_client)

    await memory.update_status(
        AgentStatus.SCANNING,
        last_signal={"token_in": "ETH", "price": 3200.0},
    )
    root = await memory.log_event(
        LogEventType.MARKET_SIGNAL,
        data={"token": "ETH", "price": 3200.0},
    )
    assert len(root) > 0

    state   = await memory.read_agent_state()
    history = await memory.read_recent_log(limit=5)

    assert state.status == AgentStatus.SCANNING
    assert len(history) >= 1
    assert history[-1]["event_type"] == "market_signal"