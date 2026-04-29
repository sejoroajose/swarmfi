from __future__ import annotations

import structlog

from agents.base import BaseAgent
from core.axl_client import ReceivedMessage
from core.compute.client import ZeroGComputeClient
from core.compute.risk_scorer import RiskScorer
from core.registry import AgentRegistry
from core.schema import AgentRole, MessageType
from core.storage.client import ZeroGClient
from core.storage.models import AgentStatus, LogEventType

log = structlog.get_logger(__name__)


class RiskAgent(BaseAgent):
    def __init__(
        self,
        registry:       AgentRegistry,
        compute_client: ZeroGComputeClient,
        zg_client:      ZeroGClient | None = None,
    ) -> None:
        super().__init__(AgentRole.RISK, registry, zg_client=zg_client)
        self._scorer = RiskScorer(compute_client)

    async def on_message(self, received: ReceivedMessage) -> None:
        msg = received.message
        match msg.message_type:
            case MessageType.MARKET_SIGNAL:
                await self._handle_signal(msg.payload)
            case _:
                self._log.warning("unexpected message", type=msg.message_type)

    async def _handle_signal(self, signal: dict) -> None:
        if self.memory:
            await self.memory.update_status(AgentStatus.DECIDING, last_signal=signal)

        decision_msg = await self._scorer.score(
            signal=signal,
            sender_pubkey=self.pubkey,
            our_pubkey=self.pubkey,
        )

        if self.memory:
            await self.memory.log_event(
                LogEventType.RISK_DECISION,
                data=decision_msg.payload,
            )
            await self.memory.update_status(
                AgentStatus.IDLE,
                last_risk_score=decision_msg.payload.get("risk_score"),
            )

        await self.send(AgentRole.EXECUTOR, decision_msg)
        self._log.info(
            "decision sent",
            action=decision_msg.payload.get("action"),
            risk=decision_msg.payload.get("risk_score"),
        )