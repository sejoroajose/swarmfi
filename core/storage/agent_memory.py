"""
core/storage/agent_memory.py
Convenience wrapper that each agent uses to read/write its own state
and append to the shared history log.

This keeps agent code clean — agents call self.memory.update_status()
rather than manually constructing SwarmState objects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from core.storage.client import ZeroGClient
from core.storage.kv import SwarmKV
from core.storage.log import SwarmLog
from core.storage.models import (
    AgentState,
    AgentStatus,
    KVKey,
    LogEventType,
    SwarmState,
)

log = structlog.get_logger(__name__)


class AgentMemory:
    """
    Per-agent interface to 0G Storage.
    Instantiated once per agent in BaseAgent.start().

    Provides:
      - update_status()     write this agent's live state to 0G KV
      - read_swarm_state()  read the whole swarm's current state from 0G KV
      - log_event()         append an entry to the shared history log
      - read_recent_log()   read recent history entries
    """

    def __init__(
        self,
        role:   str,
        client: ZeroGClient,
        kv:     SwarmKV,
        slog:   SwarmLog,
    ) -> None:
        self.role   = role
        self._client = client
        self._kv    = kv
        self._slog  = slog
        self._log   = log.bind(agent=role)

    # ── Agent state ───────────────────────────────────────────────────────────

    async def update_status(
        self,
        status:         AgentStatus,
        last_signal:    dict[str, Any] | None = None,
        last_risk_score: float | None = None,
        last_tx_hash:   str | None = None,
        error_message:  str | None = None,
        metadata:       dict[str, Any] | None = None,
    ) -> None:
        """
        Write this agent's current state to 0G KV.
        Also updates the aggregate SwarmState so other agents see the change.
        """
        agent_state = AgentState(
            agent_role=self.role,
            status=status,
            last_seen=datetime.now(tz=timezone.utc),
            last_signal=last_signal or {},
            last_risk_score=last_risk_score,
            last_tx_hash=last_tx_hash,
            error_message=error_message,
            metadata=metadata or {},
        )

        # Write individual agent state
        await self._kv.set(
            KVKey.agent_state(self.role),
            agent_state.encode(),
        )

        # Update aggregate swarm state
        swarm = await self.read_swarm_state()
        updated = swarm.update_agent(self.role, agent_state)
        await self._kv.set(KVKey.SWARM_STATE, updated.encode())

        self._log.info(
            "memory: status updated",
            status=status.value,
            kv_keys=2,
        )

    async def read_swarm_state(self) -> SwarmState:
        """
        Read the aggregate swarm state from 0G KV.
        Returns empty SwarmState if not yet written.
        """
        raw = await self._kv.get(KVKey.SWARM_STATE)
        if raw is None:
            return SwarmState()
        try:
            return SwarmState.decode(raw)
        except Exception as exc:
            self._log.warning("memory: failed to decode SwarmState", error=str(exc))
            return SwarmState()

    async def read_agent_state(self, role: str | None = None) -> AgentState | None:
        """
        Read a specific agent's state from 0G KV.
        Defaults to this agent's own state.
        """
        target = role or self.role
        raw = await self._kv.get(KVKey.agent_state(target))
        if raw is None:
            return None
        return AgentState.decode(raw)

    # ── History log ───────────────────────────────────────────────────────────

    async def log_event(
        self,
        event_type: LogEventType,
        data:       dict[str, Any] | None = None,
    ) -> str:
        """
        Append an event to the shared history log on 0G Storage.
        Returns the 0G root hash of the log entry (permanent ID).
        """
        entry = await self._slog.append(
            event_type=event_type,
            agent_role=self.role,
            data=data or {},
        )
        self._log.info(
            "memory: log event written",
            evt=event_type.value,
            root=entry.root_hash[:16] + "…" if entry.root_hash else "none",
        )
        return entry.root_hash or ""

    async def read_recent_log(self, limit: int = 20) -> list[dict[str, Any]]:
        """
        Return recent log entries as plain dicts (easy for agents to inspect).
        """
        entries = await self._slog.recent(limit=limit)
        return [
            {
                "entry_id":   e.entry_id,
                "event_type": e.event_type.value,
                "agent_role": e.agent_role,
                "timestamp":  e.timestamp.isoformat(),
                "data":       e.data,
                "root_hash":  e.root_hash,
            }
            for e in entries
        ]

    @property
    def manifest_root(self) -> str | None:
        """
        The current KV manifest root hash.
        Agents should persist this so they can reload state on restart.
        In Stage 6 this will be stored in the agent's ENS text record.
        """
        return self._kv.manifest_root


# ── Factory ───────────────────────────────────────────────────────────────────

def make_agent_memory(role: str, client: ZeroGClient) -> AgentMemory:
    """
    Create a fully wired AgentMemory for a given agent role.
    All three layers (client, kv, log) share the same ZeroGClient.
    """
    kv   = SwarmKV(client)
    slog = SwarmLog(client, kv)
    return AgentMemory(role=role, client=client, kv=kv, slog=slog)


def make_shared_memory_set(
    roles: list[str],
    client: ZeroGClient,
) -> dict[str, "AgentMemory"]:
    """
    Create a set of AgentMemory instances that ALL share one SwarmKV.
    This is how production agents are wired — they share state via 0G.

    Usage:
        memories = make_shared_memory_set(["researcher","risk","executor"], zg)
        researcher = memories["researcher"]
        risk       = memories["risk"]
    """
    shared_kv = SwarmKV(client)
    return {
        role: AgentMemory(
            role=role,
            client=client,
            kv=shared_kv,
            slog=SwarmLog(client, shared_kv),
        )
        for role in roles
    }