"""
tests/test_storage_agent_memory.py
Unit tests for AgentMemory — the per-agent interface to 0G Storage.
"""

import pytest

from core.storage.agent_memory import make_agent_memory, make_shared_memory_set
from core.storage.client import ZeroGClient
from core.storage.models import AgentStatus, LogEventType


@pytest.fixture()
def zg_client():
    client = ZeroGClient.from_env()
    yield client
    client.reset_memory_store()


@pytest.fixture()
def researcher_memory(zg_client):
    return make_agent_memory("researcher", zg_client)


@pytest.fixture()
def risk_memory(zg_client):
    return make_agent_memory("risk", zg_client)


@pytest.fixture()
def shared_memories(zg_client):
    """researcher + risk sharing one SwarmKV — mirrors production wiring."""
    return make_shared_memory_set(["researcher", "risk", "executor"], zg_client)


@pytest.mark.unit
class TestAgentMemoryStatus:
    @pytest.mark.asyncio
    async def test_update_status_writes_to_kv(self, researcher_memory):
        await researcher_memory.update_status(AgentStatus.SCANNING)
        state = await researcher_memory.read_agent_state()
        assert state is not None
        assert state.status == AgentStatus.SCANNING

    @pytest.mark.asyncio
    async def test_update_status_updates_swarm_state(self, researcher_memory):
        await researcher_memory.update_status(AgentStatus.DECIDING)
        swarm = await researcher_memory.read_swarm_state()
        assert "researcher" in swarm.agents
        assert swarm.agents["researcher"].status == AgentStatus.DECIDING

    @pytest.mark.asyncio
    async def test_update_status_with_signal(self, researcher_memory):
        signal = {"token_in": "ETH", "token_out": "USDC", "price": 3200.0}
        await researcher_memory.update_status(
            AgentStatus.SCANNING,
            last_signal=signal,
        )
        state = await researcher_memory.read_agent_state()
        assert state.last_signal["price"] == 3200.0

    @pytest.mark.asyncio
    async def test_update_status_with_risk_score(self, researcher_memory):
        await researcher_memory.update_status(
            AgentStatus.DECIDING,
            last_risk_score=6.8,
        )
        state = await researcher_memory.read_agent_state()
        assert state.last_risk_score == 6.8

    @pytest.mark.asyncio
    async def test_update_status_with_error(self, researcher_memory):
        await researcher_memory.update_status(
            AgentStatus.ERROR,
            error_message="RPC timeout",
        )
        state = await researcher_memory.read_agent_state()
        assert state.error_message == "RPC timeout"

    @pytest.mark.asyncio
    async def test_read_agent_state_returns_none_before_write(self, researcher_memory):
        state = await researcher_memory.read_agent_state()
        assert state is None


@pytest.mark.unit
class TestAgentMemorySwarmView:
    @pytest.mark.asyncio
    async def test_two_agents_see_each_others_state(self, shared_memories):
        researcher_memory = shared_memories["researcher"]
        risk_memory       = shared_memories["risk"]

        await researcher_memory.update_status(AgentStatus.SCANNING)
        await risk_memory.update_status(AgentStatus.DECIDING)

        swarm_from_researcher = await researcher_memory.read_swarm_state()
        assert "risk" in swarm_from_researcher.agents
        assert swarm_from_researcher.agents["risk"].status == AgentStatus.DECIDING

        swarm_from_risk = await risk_memory.read_swarm_state()
        assert "researcher" in swarm_from_risk.agents
        assert swarm_from_risk.agents["researcher"].status == AgentStatus.SCANNING

    @pytest.mark.asyncio
    async def test_read_swarm_state_empty_before_any_write(self, researcher_memory):
        swarm = await researcher_memory.read_swarm_state()
        assert swarm.agents == {}
        assert swarm.version == 0

    @pytest.mark.asyncio
    async def test_read_other_agents_state_by_role(self, shared_memories):
        researcher_memory = shared_memories["researcher"]
        risk_memory       = shared_memories["risk"]

        await risk_memory.update_status(
            AgentStatus.DECIDING,
            last_risk_score=4.2,
        )
        risk_state = await researcher_memory.read_agent_state("risk")
        assert risk_state is not None
        assert risk_state.last_risk_score == 4.2


@pytest.mark.unit
class TestAgentMemoryLog:
    @pytest.mark.asyncio
    async def test_log_event_returns_root_hash(self, researcher_memory):
        root = await researcher_memory.log_event(
            LogEventType.MARKET_SIGNAL,
            data={"token": "ETH"},
        )
        assert isinstance(root, str)
        assert len(root) > 0

    @pytest.mark.asyncio
    async def test_log_events_appear_in_recent_log(self, researcher_memory):
        await researcher_memory.log_event(LogEventType.AGENT_STARTED)
        await researcher_memory.log_event(
            LogEventType.MARKET_SIGNAL,
            data={"token": "ETH"},
        )
        history = await researcher_memory.read_recent_log(limit=10)
        assert len(history) == 2
        assert history[0]["event_type"] == "agent_started"
        assert history[1]["event_type"] == "market_signal"

    @pytest.mark.asyncio
    async def test_two_agents_share_same_log(self, shared_memories):
        researcher_memory = shared_memories["researcher"]
        risk_memory       = shared_memories["risk"]

        await researcher_memory.log_event(LogEventType.MARKET_SIGNAL)
        await risk_memory.log_event(LogEventType.RISK_DECISION)

        history = await researcher_memory.read_recent_log(limit=10)
        assert len(history) == 2
        event_types = {e["event_type"] for e in history}
        assert "market_signal" in event_types
        assert "risk_decision" in event_types

    @pytest.mark.asyncio
    async def test_read_recent_log_empty_before_events(self, researcher_memory):
        history = await researcher_memory.read_recent_log()
        assert history == []

    @pytest.mark.asyncio
    async def test_log_entry_has_required_fields(self, researcher_memory):
        await researcher_memory.log_event(
            LogEventType.TRADE_EXECUTED,
            data={"tx": "0xabc"},
        )
        history = await researcher_memory.read_recent_log()
        entry = history[0]
        assert "entry_id"   in entry
        assert "event_type" in entry
        assert "agent_role" in entry
        assert "timestamp"  in entry
        assert "data"       in entry
        assert "root_hash"  in entry
        assert entry["data"]["tx"] == "0xabc"


@pytest.mark.unit
class TestAgentMemoryManifest:
    @pytest.mark.asyncio
    async def test_manifest_root_none_before_any_write(self, researcher_memory):
        assert researcher_memory.manifest_root is None

    @pytest.mark.asyncio
    async def test_manifest_root_set_after_write(self, researcher_memory):
        await researcher_memory.update_status(AgentStatus.IDLE)
        assert researcher_memory.manifest_root is not None