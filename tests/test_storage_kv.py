"""
tests/test_storage_kv.py
Unit tests for SwarmKV using the in-memory ZeroGClient backend.
No network required.
"""

import pytest

from core.storage.client import ZeroGClient
from core.storage.kv import SwarmKV
from core.storage.models import AgentState, AgentStatus, KVKey, SwarmState


@pytest.fixture()
def zg_client():
    """Fresh in-memory ZeroGClient for each test."""
    client = ZeroGClient.from_env()   # no ZG_PRIVATE_KEY → in-memory mode
    assert not client.is_live
    yield client
    client.reset_memory_store()


@pytest.fixture()
def kv(zg_client):
    return SwarmKV(zg_client)


@pytest.mark.unit
class TestSwarmKVBasicOperations:
    @pytest.mark.asyncio
    async def test_set_and_get_bytes(self, kv):
        await kv.set("mykey", b"hello world")
        result = await kv.get("mykey")
        assert result == b"hello world"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, kv):
        result = await kv.get("does_not_exist")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_or_default_returns_default(self, kv):
        result = await kv.get_or_default("absent", b"default")
        assert result == b"default"

    @pytest.mark.asyncio
    async def test_get_or_default_returns_value_when_set(self, kv):
        await kv.set("present", b"value")
        result = await kv.get_or_default("present", b"default")
        assert result == b"value"

    @pytest.mark.asyncio
    async def test_overwrite_key(self, kv):
        await kv.set("key", b"first")
        await kv.set("key", b"second")
        result = await kv.get("key")
        assert result == b"second"

    @pytest.mark.asyncio
    async def test_delete_removes_key(self, kv):
        await kv.set("key", b"value")
        await kv.delete("key")
        result = await kv.get("key")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key_is_noop(self, kv):
        # Should not raise
        await kv.delete("never_existed")

    @pytest.mark.asyncio
    async def test_exists_true_after_set(self, kv):
        await kv.set("exists_key", b"x")
        assert await kv.exists("exists_key") is True

    @pytest.mark.asyncio
    async def test_exists_false_before_set(self, kv):
        assert await kv.exists("not_set") is False

    @pytest.mark.asyncio
    async def test_exists_false_after_delete(self, kv):
        await kv.set("del_key", b"v")
        await kv.delete("del_key")
        assert await kv.exists("del_key") is False

    @pytest.mark.asyncio
    async def test_keys_returns_all_set_keys(self, kv):
        await kv.set("a", b"1")
        await kv.set("b", b"2")
        await kv.set("c", b"3")
        assert set(kv.keys()) == {"a", "b", "c"}

    @pytest.mark.asyncio
    async def test_keys_excludes_deleted(self, kv):
        await kv.set("a", b"1")
        await kv.set("b", b"2")
        await kv.delete("a")
        assert kv.keys() == ["b"]


@pytest.mark.unit
class TestSwarmKVJsonHelpers:
    @pytest.mark.asyncio
    async def test_set_json_and_get_json(self, kv):
        obj = {"token": "ETH", "price": 3200.0, "signal": "strong"}
        await kv.set_json("signal", obj)
        result = await kv.get_json("signal")
        assert result == obj

    @pytest.mark.asyncio
    async def test_get_json_nonexistent_returns_none(self, kv):
        result = await kv.get_json("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_json_nested_object(self, kv):
        nested = {"agents": {"researcher": {"status": "scanning"}}}
        await kv.set_json("nested", nested)
        result = await kv.get_json("nested")
        assert result["agents"]["researcher"]["status"] == "scanning"


@pytest.mark.unit
class TestSwarmKVWithModels:
    @pytest.mark.asyncio
    async def test_store_and_retrieve_agent_state(self, kv):
        state = AgentState(
            agent_role="researcher",
            status=AgentStatus.SCANNING,
            last_risk_score=3.5,
        )
        await kv.set(KVKey.agent_state("researcher"), state.encode())
        raw     = await kv.get(KVKey.agent_state("researcher"))
        decoded = AgentState.decode(raw)
        assert decoded.agent_role     == "researcher"
        assert decoded.status         == AgentStatus.SCANNING
        assert decoded.last_risk_score == 3.5

    @pytest.mark.asyncio
    async def test_store_and_retrieve_swarm_state(self, kv):
        swarm = SwarmState()
        swarm = swarm.update_agent(
            "researcher",
            AgentState(agent_role="researcher", status=AgentStatus.IDLE),
        )
        await kv.set(KVKey.SWARM_STATE, swarm.encode())
        raw     = await kv.get(KVKey.SWARM_STATE)
        decoded = SwarmState.decode(raw)
        assert decoded.version == 1
        assert "researcher" in decoded.agents


@pytest.mark.unit
class TestSwarmKVManifest:
    @pytest.mark.asyncio
    async def test_manifest_root_none_before_any_write(self, kv):
        assert kv.manifest_root is None

    @pytest.mark.asyncio
    async def test_manifest_root_set_after_write(self, kv):
        await kv.set("key", b"value")
        assert kv.manifest_root is not None
        assert len(kv.manifest_root) > 0

    @pytest.mark.asyncio
    async def test_manifest_persists_across_fresh_kv_with_load(self, zg_client):
        """
        Simulate agent restart: write with kv1, then reload manifest
        with a fresh kv2 using the same root hash.
        """
        kv1 = SwarmKV(zg_client)
        await kv1.set("persist_key", b"persist_value")
        manifest_root = kv1.manifest_root
        assert manifest_root is not None

        # Simulate restart — fresh KV instance, same underlying storage
        kv2 = SwarmKV(zg_client)
        await kv2.load_manifest(manifest_root)
        result = await kv2.get("persist_key")
        assert result == b"persist_value"

    @pytest.mark.asyncio
    async def test_load_manifest_with_none_starts_fresh(self, kv):
        # Should not raise
        await kv.load_manifest(None)
        assert kv.manifest_root is None