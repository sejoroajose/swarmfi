"""
tests/test_storage_models.py
Unit tests for core/storage/models.py — no I/O, no network.
"""

import pytest
from datetime import timezone
from pydantic import ValidationError

from core.storage.models import (
    AgentState,
    AgentStatus,
    KVKey,
    LogEntry,
    LogEventType,
    LogIndex,
    SwarmState,
)


@pytest.mark.unit
class TestAgentState:
    def test_default_status_is_idle(self):
        s = AgentState(agent_role="researcher")
        assert s.status == AgentStatus.IDLE

    def test_encode_decode_roundtrip(self):
        original = AgentState(
            agent_role="researcher",
            status=AgentStatus.SCANNING,
            last_risk_score=4.2,
            last_tx_hash="0xabc",
        )
        decoded = AgentState.decode(original.encode())
        assert decoded.agent_role    == original.agent_role
        assert decoded.status        == original.status
        assert decoded.last_risk_score == original.last_risk_score
        assert decoded.last_tx_hash  == original.last_tx_hash

    def test_timestamp_is_utc(self):
        s = AgentState(agent_role="risk")
        assert s.last_seen.tzinfo is not None

    def test_all_status_values_valid(self):
        for status in AgentStatus:
            s = AgentState(agent_role="executor", status=status)
            assert s.status == status

    def test_garbage_decode_raises(self):
        with pytest.raises(Exception):
            AgentState.decode(b"not json")


@pytest.mark.unit
class TestSwarmState:
    def test_empty_swarm_state(self):
        s = SwarmState()
        assert s.version == 0
        assert s.agents  == {}

    def test_update_agent_increments_version(self):
        swarm   = SwarmState()
        agent   = AgentState(agent_role="researcher", status=AgentStatus.SCANNING)
        updated = swarm.update_agent("researcher", agent)
        assert updated.version == 1
        assert updated.agents["researcher"].status == AgentStatus.SCANNING
        # Original unchanged
        assert swarm.version == 0

    def test_update_agent_twice(self):
        swarm  = SwarmState()
        agent1 = AgentState(agent_role="researcher", status=AgentStatus.IDLE)
        agent2 = AgentState(agent_role="risk",       status=AgentStatus.DECIDING)
        s1 = swarm.update_agent("researcher", agent1)
        s2 = s1.update_agent("risk", agent2)
        assert s2.version == 2
        assert len(s2.agents) == 2

    def test_encode_decode_roundtrip(self):
        swarm = SwarmState()
        swarm = swarm.update_agent(
            "researcher",
            AgentState(agent_role="researcher", status=AgentStatus.SCANNING),
        )
        decoded = SwarmState.decode(swarm.encode())
        assert decoded.version == 1
        assert "researcher" in decoded.agents


@pytest.mark.unit
class TestLogEntry:
    def test_encode_decode_roundtrip(self):
        entry = LogEntry(
            entry_id="test-id-1",
            event_type=LogEventType.MARKET_SIGNAL,
            agent_role="researcher",
            data={"token_in": "ETH", "price": 3200.0},
        )
        decoded = LogEntry.decode(entry.encode())
        assert decoded.entry_id   == entry.entry_id
        assert decoded.event_type == LogEventType.MARKET_SIGNAL
        assert decoded.data["price"] == 3200.0

    def test_root_hash_none_by_default(self):
        entry = LogEntry(
            entry_id="x",
            event_type=LogEventType.RISK_DECISION,
            agent_role="risk",
        )
        assert entry.root_hash is None

    def test_all_event_types_valid(self):
        for et in LogEventType:
            entry = LogEntry(
                entry_id="x",
                event_type=et,
                agent_role="researcher",
            )
            assert entry.event_type == et


@pytest.mark.unit
class TestLogIndex:
    def test_empty_index(self):
        idx = LogIndex()
        assert idx.entries     == []
        assert idx.entry_count == 0

    def test_append_increments_count(self):
        idx = LogIndex()
        idx2 = idx.append("hash1")
        assert idx2.entry_count == 1
        assert "hash1" in idx2.entries
        # Original unchanged
        assert idx.entry_count == 0

    def test_append_multiple(self):
        idx = LogIndex()
        for i in range(5):
            idx = idx.append(f"hash{i}")
        assert idx.entry_count == 5
        assert idx.entries[-1] == "hash4"

    def test_encode_decode_roundtrip(self):
        idx = LogIndex()
        idx = idx.append("aabbcc")
        idx = idx.append("ddeeff")
        decoded = LogIndex.decode(idx.encode())
        assert decoded.entry_count == 2
        assert decoded.entries == ["aabbcc", "ddeeff"]


@pytest.mark.unit
class TestKVKey:
    def test_swarm_state_key(self):
        assert KVKey.SWARM_STATE == "swarmfi:state:v1"

    def test_log_index_key(self):
        assert KVKey.LOG_INDEX == "swarmfi:log:index:v1"

    def test_agent_state_key(self):
        assert KVKey.agent_state("researcher") == "swarmfi:agent:researcher:v1"
        assert KVKey.agent_state("risk")       == "swarmfi:agent:risk:v1"
        assert KVKey.agent_state("executor")   == "swarmfi:agent:executor:v1"