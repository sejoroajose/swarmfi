"""
core/storage/models.py
Pydantic models for all data stored on 0G Storage.

Two storage abstractions:
  KV  — live swarm state (agent status, last signal, risk scores)
  Log — append-only history (decisions, executions, errors)

Every model can serialize to/from bytes so it moves cleanly through
the 0G upload/download pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────


class AgentStatus(str, Enum):
    IDLE      = "idle"
    SCANNING  = "scanning"
    DECIDING  = "deciding"
    EXECUTING = "executing"
    ERROR     = "error"


class LogEventType(str, Enum):
    AGENT_STARTED    = "agent_started"
    AGENT_STOPPED    = "agent_stopped"
    MARKET_SIGNAL    = "market_signal"
    RISK_DECISION    = "risk_decision"
    TRADE_EXECUTED   = "trade_executed"
    TRADE_FAILED     = "trade_failed"
    STATE_UPDATED    = "state_updated"
    ERROR            = "error"


# ── KV models (live state) ────────────────────────────────────────────────────


class AgentState(BaseModel):
    """
    Live state of a single agent — stored in 0G KV.
    Updated on every significant agent action.
    """
    agent_role:     str
    status:         AgentStatus = AgentStatus.IDLE
    last_seen:      datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    last_signal:    dict[str, Any] = Field(default_factory=dict)
    last_risk_score: float | None = None
    last_tx_hash:   str | None = None
    error_message:  str | None = None
    metadata:       dict[str, Any] = Field(default_factory=dict)

    def encode(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def decode(cls, data: bytes) -> "AgentState":
        return cls.model_validate_json(data.decode("utf-8"))


class SwarmState(BaseModel):
    """
    Aggregate live state of the whole swarm — the single KV entry
    that all agents read to understand the swarm's current situation.
    """
    version:    int = 0
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    agents:     dict[str, AgentState] = Field(default_factory=dict)
    active_opportunity: dict[str, Any] | None = None
    swarm_metadata:     dict[str, Any] = Field(default_factory=dict)

    def encode(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def decode(cls, data: bytes) -> "SwarmState":
        return cls.model_validate_json(data.decode("utf-8"))

    def update_agent(self, role: str, state: AgentState) -> "SwarmState":
        """Return a new SwarmState with the given agent updated."""
        updated = self.model_copy(deep=True)
        updated.agents[role] = state
        updated.version += 1
        updated.updated_at = datetime.now(tz=timezone.utc)
        return updated


# ── Log models (history) ──────────────────────────────────────────────────────


class LogEntry(BaseModel):
    """
    Single immutable entry in the swarm history log.
    Uploaded to 0G Storage; root hash appended to the log index.
    """
    entry_id:   str = Field(description="UUID4 of this log entry")
    event_type: LogEventType
    agent_role: str
    timestamp:  datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    data:       dict[str, Any] = Field(default_factory=dict)
    root_hash:  str | None = Field(
        default=None,
        description="0G Storage root hash — populated after upload"
    )

    def encode(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def decode(cls, data: bytes) -> "LogEntry":
        return cls.model_validate_json(data.decode("utf-8"))


class LogIndex(BaseModel):
    """
    Ordered list of 0G root hashes for all log entries.
    This index itself is stored in 0G KV so agents can
    discover all history without knowing individual root hashes.
    """
    entries:    list[str] = Field(
        default_factory=list,
        description="0G root hashes in chronological order"
    )
    entry_count: int = 0
    last_updated: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    def append(self, root_hash: str) -> "LogIndex":
        updated = self.model_copy(deep=True)
        updated.entries.append(root_hash)
        updated.entry_count += 1
        updated.last_updated = datetime.now(tz=timezone.utc)
        return updated

    def encode(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def decode(cls, data: bytes) -> "LogIndex":
        return cls.model_validate_json(data.decode("utf-8"))


# ── KV key constants ──────────────────────────────────────────────────────────
# Agents use these constants so key names never diverge.

class KVKey:
    SWARM_STATE = "swarmfi:state:v1"
    LOG_INDEX   = "swarmfi:log:index:v1"

    @staticmethod
    def agent_state(role: str) -> str:
        return f"swarmfi:agent:{role}:v1"