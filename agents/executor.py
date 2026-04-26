"""
agents/executor.py
Executor agent — Stage 1 stub.

Responsibilities (fully implemented in Stage 3–4):
  - Receive TradeDecision from Risk
  - Execute swap via Uniswap API + KeeperHub
  - Broadcast ExecutionResult to all agents

Stage 1 scope:
  - Respond to PING (handled by BaseAgent)
  - Log any received messages
"""

from __future__ import annotations

import structlog

from agents.base import BaseAgent
from core.axl_client import ReceivedMessage
from core.registry import AgentRegistry
from core.schema import AgentRole, MessageType

log = structlog.get_logger(__name__)


class ExecutorAgent(BaseAgent):
    def __init__(self, registry: AgentRegistry) -> None:
        super().__init__(AgentRole.EXECUTOR, registry)

    async def on_message(self, received: ReceivedMessage) -> None:
        msg = received.message
        match msg.message_type:
            case MessageType.TRADE_DECISION:
                self._log.info(
                    "trade decision received (stub — execution in Stage 3)",
                    action=msg.payload.get("action"),
                    risk_score=msg.payload.get("risk_score"),
                )
            case _:
                self._log.warning(
                    "unexpected message type",
                    msg_type=msg.message_type,
                )