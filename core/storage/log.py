"""
core/storage/log.py
Append-only history log backed by 0G Storage.

Architecture:
  - Each LogEntry is uploaded as its own 0G object → gets a root hash
  - A LogIndex (ordered list of root hashes) is stored in the KV store
    under KVKey.LOG_INDEX
  - To read history: fetch LogIndex from KV, then fetch entries by hash
  - Entries are immutable once uploaded — perfect for audit trails

This gives the swarm:
  - Verifiable, tamper-evident decision history
  - Any agent can replay the full history from 0G at any time
  - Cross-agent visibility (risk agent can see researcher decisions, etc.)

Example history for a single trade cycle:
  [MARKET_SIGNAL]    researcher detected ETH/USDC opportunity
  [RISK_DECISION]    risk scored 3.2/10, approved BUY
  [TRADE_EXECUTED]   executor placed swap via Uniswap + KeeperHub, tx=0xabc...
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from core.storage.client import ZeroGClient
from core.storage.kv import SwarmKV
from core.storage.models import KVKey, LogEntry, LogEventType, LogIndex

log = structlog.get_logger(__name__)


class SwarmLog:
    """
    Append-only history log for the SwarmFi swarm.

    Usage:
        kv  = SwarmKV(client)
        slog = SwarmLog(client, kv)

        entry = await slog.append(
            event_type=LogEventType.MARKET_SIGNAL,
            agent_role="researcher",
            data={"token_in": "ETH", "price": 3200},
        )
        # entry.root_hash is now permanently on 0G

        history = await slog.recent(limit=20)
        for e in history:
            print(e.event_type, e.data)
    """

    def __init__(self, client: ZeroGClient, kv: SwarmKV) -> None:
        self._client = client
        self._kv     = kv
        self._log    = log.bind(store="log")

    # ── Write ─────────────────────────────────────────────────────────────────

    async def append(
        self,
        event_type: LogEventType,
        agent_role: str,
        data:       dict[str, Any] | None = None,
    ) -> LogEntry:
        """
        Create a new LogEntry, upload it to 0G, append its root hash
        to the LogIndex in KV.  Returns the completed LogEntry.
        """
        entry = LogEntry(
            entry_id=str(uuid.uuid4()),
            event_type=event_type,
            agent_role=agent_role,
            timestamp=datetime.now(tz=timezone.utc),
            data=data or {},
        )

        # Upload the entry itself
        result = await self._client.upload(entry.encode())
        entry = entry.model_copy(update={"root_hash": result.root_hash})

        # Update the log index in KV
        index = await self._load_index()
        updated_index = index.append(result.root_hash)
        await self._kv.set(KVKey.LOG_INDEX, updated_index.encode())

        self._log.info(
            "log.append",
            evt=event_type.value,
            agent=agent_role,
            root=result.root_hash[:16] + "…",
            total_entries=updated_index.entry_count,
        )
        return entry

    # ── Read ──────────────────────────────────────────────────────────────────

    async def recent(self, limit: int = 50) -> list[LogEntry]:
        """
        Return the most recent `limit` log entries in chronological order.
        Fetches each entry from 0G individually.
        """
        index = await self._load_index()
        root_hashes = index.entries[-limit:]

        entries: list[LogEntry] = []
        for root_hash in root_hashes:
            try:
                raw   = await self._client.download(root_hash)
                entry = LogEntry.decode(raw)
                entries.append(entry)
            except Exception as exc:
                self._log.warning(
                    "log.recent: failed to fetch entry",
                    root=root_hash[:16] + "…",
                    error=str(exc),
                )
        return entries

    async def get_entry(self, root_hash: str) -> LogEntry:
        """Fetch a specific log entry by its 0G root hash."""
        raw = await self._client.download(root_hash)
        return LogEntry.decode(raw)

    async def entry_count(self) -> int:
        """Return total number of log entries."""
        index = await self._load_index()
        return index.entry_count

    async def get_index(self) -> LogIndex:
        """Return the full log index."""
        return await self._load_index()

    # ── Filtering helpers ─────────────────────────────────────────────────────

    async def by_agent(self, role: str, limit: int = 20) -> list[LogEntry]:
        """Return recent entries from a specific agent."""
        all_recent = await self.recent(limit=limit * 3)
        return [e for e in all_recent if e.agent_role == role][:limit]

    async def by_event_type(
        self,
        event_type: LogEventType,
        limit: int = 20,
    ) -> list[LogEntry]:
        """Return recent entries of a specific event type."""
        all_recent = await self.recent(limit=limit * 3)
        return [e for e in all_recent if e.event_type == event_type][:limit]

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _load_index(self) -> LogIndex:
        """Load the LogIndex from KV, or return empty if not yet created."""
        raw = await self._kv.get(KVKey.LOG_INDEX)
        if raw is None:
            return LogIndex()
        try:
            return LogIndex.decode(raw)
        except Exception as exc:
            self._log.warning("log: failed to decode index, using empty", error=str(exc))
            return LogIndex()