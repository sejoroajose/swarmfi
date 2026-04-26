"""
agents/researcher.py
Researcher agent — Stage 1 stub.

Responsibilities (fully implemented in Stage 3+):
  - Monitor onchain price signals and news feeds
  - Produce MarketSignal messages and broadcast to the Risk agent

Stage 1 scope:
  - Receive PONG responses and log them
  - Send PING to all peers to verify connectivity
  - Handle EXECUTION_RESULT from executor (log only)
"""

from __future__ import annotations

import structlog

from agents.base import BaseAgent
from core.axl_client import ReceivedMessage
from core.registry import AgentRegistry
from core.schema import AgentRole, MessageType, make_ping

log = structlog.get_logger(__name__)


class ResearcherAgent(BaseAgent):
    def __init__(self, registry: AgentRegistry) -> None:
        super().__init__(AgentRole.RESEARCHER, registry)

    async def ping_swarm(self) -> None:
        """Send a PING to every other agent in the swarm."""
        for role in self.registry.all_roles():
            if role == self.role:
                continue
            msg = make_ping(
                sender_role=self.role,
                sender_pubkey=self.pubkey,
            )
            await self.send(role, msg)
            self._log.info("ping sent", to=role.value, nonce=msg.payload.get("nonce", "")[:8])

    async def on_message(self, received: ReceivedMessage) -> None:
        msg = received.message
        match msg.message_type:
            case MessageType.PONG:
                self._log.info(
                    "pong received",
                    from_role=msg.sender_role.value,
                    nonce=msg.payload.get("nonce", "")[:8],
                    latency_ms=msg.payload.get("latency_ms"),
                )
            case MessageType.EXECUTION_RESULT:
                self._log.info(
                    "execution result received",
                    success=msg.payload.get("success"),
                    tx_hash=msg.payload.get("tx_hash"),
                )
            case _:
                self._log.warning(
                    "unexpected message type",
                    msg_type=msg.message_type,
                )