"""
agents/risk.py
Risk agent — Stage 1 stub.

Responsibilities (fully implemented in Stage 5+):
  - Receive MarketSignal from Researcher
  - Score trade risk using 0G Compute inference
  - Emit TradeDecision to Executor

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


class RiskAgent(BaseAgent):
    def __init__(self, registry: AgentRegistry) -> None:
        super().__init__(AgentRole.RISK, registry)

    async def on_message(self, received: ReceivedMessage) -> None:
        msg = received.message
        match msg.message_type:
            case MessageType.MARKET_SIGNAL:
                self._log.info(
                    "market signal received (stub — scoring in Stage 5)",
                    token_in=msg.payload.get("token_in"),
                    token_out=msg.payload.get("token_out"),
                    signal=msg.payload.get("signal"),
                )
            case _:
                self._log.warning(
                    "unexpected message type",
                    msg_type=msg.message_type,
                )