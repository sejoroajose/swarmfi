"""
core/storage
Persistent memory layer for the SwarmFi swarm, backed by 0G Storage.

Public API:
    from core.storage import ZeroGClient, SwarmKV, SwarmLog, KVKey
    from core.storage.models import AgentState, SwarmState, LogEntry, LogEventType
"""

from core.storage.client import UploadResult, ZeroGClient
from core.storage.kv import SwarmKV
from core.storage.log import SwarmLog
from core.storage.models import (
    AgentState,
    AgentStatus,
    KVKey,
    LogEntry,
    LogEventType,
    LogIndex,
    SwarmState,
)

__all__ = [
    "ZeroGClient",
    "UploadResult",
    "SwarmKV",
    "SwarmLog",
    "AgentState",
    "AgentStatus",
    "SwarmState",
    "LogEntry",
    "LogEventType",
    "LogIndex",
    "KVKey",
]