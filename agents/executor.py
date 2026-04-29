from __future__ import annotations

import structlog

from agents.base import BaseAgent
from core.axl_client import ReceivedMessage
from core.keeperhub.client import KeeperHubClient
from core.keeperhub.executor import KeeperHubSwapExecutor
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
from core.uniswap.models import SwapStatus, SwapType

log = structlog.get_logger(__name__)


class ExecutorAgent(BaseAgent):
    """
    Executor agent — Stage 4.

    Receives TRADE_DECISION from Risk agent.
    Builds swap via Uniswap Trading API.
    Broadcasts via KeeperHub for guaranteed execution.
    Persists audit trail to 0G Storage.
    Reports result to researcher via AXL.
    """

    def __init__(
        self,
        registry:       AgentRegistry,
        uniswap_client: UniswapClient,
        kh_client:      KeeperHubClient,
        zg_client:      ZeroGClient | None = None,
        wallet_address: str = "",
    ) -> None:
        super().__init__(AgentRole.EXECUTOR, registry, zg_client=zg_client)
        self._uniswap   = uniswap_client
        self._kh        = kh_client
        self._wallet    = wallet_address
        self._executor  = KeeperHubSwapExecutor(
            uniswap=uniswap_client,
            keeperhub=kh_client,
            wallet_address=wallet_address,
            zg_client=zg_client,
        )

    async def on_message(self, received: ReceivedMessage) -> None:
        msg = received.message
        match msg.message_type:
            case MessageType.TRADE_DECISION:
                await self._handle_trade_decision(msg)
            case _:
                self._log.warning("unexpected message", msg_type=msg.message_type)

    async def _handle_trade_decision(self, msg: SwarmMessage) -> None:
        try:
            decision = TradeDecisionPayload.model_validate(msg.payload)
        except Exception as exc:
            self._log.error("invalid trade decision payload", error=str(exc))
            return

        self._log.info(
            "trade decision",
            action=decision.action.value,
            risk_score=decision.risk_score,
            token_in=decision.token_in[:10],
            token_out=decision.token_out[:10],
        )

        # ── Risk gate ─────────────────────────────────────────────────────────
        if decision.action == TradeAction.HOLD:
            self._log.info("risk gate: HOLD", reason=decision.rejection_reason)
            if self.memory:
                await self.memory.update_status(AgentStatus.IDLE)
                await self.memory.log_event(
                    LogEventType.TRADE_FAILED,
                    data={
                        "action":     "HOLD",
                        "reason":     decision.rejection_reason,
                        "risk_score": decision.risk_score,
                    },
                )
            await self._send_result(success=False, tx_hash=None, error=f"HOLD: {decision.rejection_reason}")
            return

        # ── Execute via KeeperHub ─────────────────────────────────────────────
        if self.memory:
            await self.memory.update_status(AgentStatus.EXECUTING)

        result = await self._executor.execute_swap(
            token_in=decision.token_in,
            token_out=decision.token_out,
            amount_in_wei=str(decision.amount_in_wei),
            chain_id=decision.chain_id,
            swap_type=SwapType.EXACT_INPUT,
            wallet_address=self._wallet,
        )

        # ── Update 0G state ───────────────────────────────────────────────────
        if self.memory:
            if result.succeeded:
                await self.memory.update_status(
                    AgentStatus.IDLE,
                    last_tx_hash=result.tx_hash,
                )
                await self.memory.log_event(
                    LogEventType.TRADE_EXECUTED,
                    data=result.to_log_data(),
                )
            else:
                await self.memory.update_status(
                    AgentStatus.ERROR,
                    error_message=result.error,
                )
                await self.memory.log_event(
                    LogEventType.TRADE_FAILED,
                    data=result.to_log_data(),
                )

        # ── Notify researcher ─────────────────────────────────────────────────
        success = result.status in (SwapStatus.CONFIRMED, SwapStatus.SUBMITTED)
        await self._send_result(
            success=success,
            tx_hash=result.tx_hash,
            error=result.error,
        )

    async def _send_result(
        self,
        success:  bool,
        tx_hash:  str | None,
        error:    str | None,
    ) -> None:
        payload = ExecutionResultPayload(
            success=success,
            tx_hash=tx_hash if success else None,
            error=error if not success else None,
        )
        msg = SwarmMessage(
            message_type=MessageType.EXECUTION_RESULT,
            sender_role=AgentRole.EXECUTOR,
            sender_pubkey=self.pubkey,
            payload=payload.model_dump(),
        )
        try:
            await self.send(AgentRole.RESEARCHER, msg)
        except Exception as exc:
            self._log.error("failed to send execution result", error=str(exc))