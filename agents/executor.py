"""
agents/executor.py  (Stage 3 update)
Replace the Stage 1/2 stub with this file.

Changes from Stage 2:
  - Accepts UniswapClient + SwapExecutor in __init__
  - Handles TRADE_DECISION messages — executes the actual swap
  - Applies the risk gate: rejects trades with action=HOLD
  - Writes SwapResult to 0G Storage log
  - Sends EXECUTION_RESULT back to researcher via AXL
  - Full error handling: never crashes the dispatch loop
"""

from __future__ import annotations

import structlog

from agents.base import BaseAgent
from core.axl_client import ReceivedMessage
from core.registry import AgentRegistry
from core.schema import (
    AgentRole,
    ExecutionResultPayload,
    MessageType,
    SwarmMessage,
    TradeAction,
    TradeDecisionPayload,
)
from core.storage.client import ZeroGClient
from core.storage.models import AgentStatus, LogEventType
from core.uniswap.client import UniswapClient
from core.uniswap.executor import SwapExecutor
from core.uniswap.models import SwapStatus, SwapType

log = structlog.get_logger(__name__)


class ExecutorAgent(BaseAgent):
    """
    Executor agent — receives TRADE_DECISION from Risk, executes swap.

    Wired with:
      - UniswapClient: Trading API for quotes and swap tx building
      - SwapExecutor:  Full pipeline (approval → quote → sign → broadcast)
      - AgentMemory:   0G Storage for persisting results (via BaseAgent)
    """

    def __init__(
        self,
        registry:       AgentRegistry,
        uniswap_client: UniswapClient,
        zg_client:      ZeroGClient | None = None,
    ) -> None:
        super().__init__(AgentRole.EXECUTOR, registry, zg_client=zg_client)
        self._uniswap = uniswap_client
        self._swap_executor = SwapExecutor.from_env(uniswap_client)

    async def on_message(self, received: ReceivedMessage) -> None:
        msg = received.message
        match msg.message_type:
            case MessageType.TRADE_DECISION:
                await self._handle_trade_decision(msg)
            case _:
                self._log.warning(
                    "unexpected message type",
                    msg_type=msg.message_type,
                )

    # ── Trade decision handler ────────────────────────────────────────────────

    async def _handle_trade_decision(self, msg: SwarmMessage) -> None:
        """
        Process a TRADE_DECISION from the Risk agent.
        Steps:
          1. Parse and validate the decision payload
          2. Apply risk gate (reject HOLD)
          3. Execute the swap via SwapExecutor
          4. Persist result to 0G Storage
          5. Broadcast EXECUTION_RESULT to researcher
        """
        try:
            decision = TradeDecisionPayload.model_validate(msg.payload)
        except Exception as exc:
            self._log.error("trade decision: invalid payload", error=str(exc))
            return

        self._log.info(
            "trade decision received",
            action=decision.action.value,
            token_in=decision.token_in,
            token_out=decision.token_out,
            risk_score=decision.risk_score,
            amount_wei=decision.amount_in_wei,
        )

        # ── Risk gate ─────────────────────────────────────────────────────────
        if decision.action == TradeAction.HOLD:
            self._log.info(
                "trade blocked by risk gate",
                reason=decision.rejection_reason,
            )
            if self.memory:
                await self.memory.update_status(
                    AgentStatus.IDLE,
                    metadata={"last_skip_reason": decision.rejection_reason},
                )
                await self.memory.log_event(
                    LogEventType.TRADE_FAILED,
                    data={
                        "action": "HOLD",
                        "reason": decision.rejection_reason,
                        "risk_score": decision.risk_score,
                    },
                )
            await self._broadcast_result(
                success=False,
                tx_hash=None,
                error=f"Risk gate: {decision.rejection_reason}",
            )
            return

        # ── Execute swap ──────────────────────────────────────────────────────
        if self.memory:
            await self.memory.update_status(AgentStatus.EXECUTING)

        swap_result = await self._swap_executor.execute_swap(
            token_in=decision.token_in,
            token_out=decision.token_out,
            amount_in_wei=str(decision.amount_in_wei),
            chain_id=decision.chain_id,
            swap_type=SwapType.EXACT_INPUT,
        )

        # ── Persist result ────────────────────────────────────────────────────
        if self.memory:
            if swap_result.succeeded:
                await self.memory.update_status(
                    AgentStatus.IDLE,
                    last_tx_hash=swap_result.tx_hash,
                )
                await self.memory.log_event(
                    LogEventType.TRADE_EXECUTED,
                    data=swap_result.to_log_data(),
                )
            else:
                await self.memory.update_status(
                    AgentStatus.ERROR,
                    error_message=swap_result.error,
                )
                await self.memory.log_event(
                    LogEventType.TRADE_FAILED,
                    data=swap_result.to_log_data(),
                )

        # ── Broadcast result to researcher ────────────────────────────────────
        await self._broadcast_result(
            success=swap_result.status in (SwapStatus.CONFIRMED, SwapStatus.SUBMITTED),
            tx_hash=swap_result.tx_hash,
            error=swap_result.error,
        )

    async def _broadcast_result(
        self,
        success:  bool,
        tx_hash:  str | None,
        error:    str | None,
    ) -> None:
        """Send EXECUTION_RESULT to the researcher agent via AXL."""
        payload = ExecutionResultPayload(
            success=success,
            tx_hash=tx_hash if success else None,
            error=error if not success else None,
        )
        result_msg = SwarmMessage(
            message_type=MessageType.EXECUTION_RESULT,
            sender_role=AgentRole.EXECUTOR,
            sender_pubkey=self.pubkey,
            payload=payload.model_dump(),
        )
        try:
            await self.send(AgentRole.RESEARCHER, result_msg)
            self._log.info(
                "execution result sent",
                success=success,
                tx_hash=(tx_hash or "")[:18],
            )
        except Exception as exc:
            self._log.error(
                "failed to send execution result",
                error=str(exc),
            )