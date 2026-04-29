"""
core/compute/risk_scorer.py
AI-powered risk scoring for SwarmFi using 0G Compute sealed inference.

Takes a MarketSignalPayload, sends it to GLM-5-FP8 on 0G Compute,
parses the structured JSON response into a TradeDecisionPayload.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from core.compute.client import ZeroGComputeClient
from core.schema import AgentRole, MessageType, SwarmMessage, TradeAction, TradeDecisionPayload

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are a DeFi risk assessment AI for an autonomous trading swarm.

You will receive a market signal and must output a JSON risk assessment.
Your output must be ONLY valid JSON — no markdown, no explanation, no preamble.

Required JSON schema:
{
  "risk_score": <float 0-10, where 0=no risk, 10=extreme risk>,
  "action": <"buy" | "sell" | "hold">,
  "confidence": <float 0-1>,
  "reasoning": <string, max 200 chars>,
  "rejection_reason": <string if action=hold, else null>
}

Risk scoring guide:
- 0-3:  Low risk. Strong signal, healthy market conditions → BUY/SELL
- 4-6:  Medium risk. Mixed signals, proceed with caution
- 7-10: High risk. Weak signal or adverse conditions → HOLD

Always output valid JSON. Never output anything else."""


def _build_user_prompt(signal: dict[str, Any]) -> str:
    return f"""Market Signal:
- Token In:  {signal.get('token_in', 'ETH')}
- Token Out: {signal.get('token_out', 'USDC')}
- Chain:     {signal.get('chain_id', 8453)}
- Price USD: ${signal.get('price_usd', 0):.2f}
- Signal Strength: {signal.get('signal', 'medium')}
- Reason: {signal.get('reason', 'No reason provided')}

Assess the risk and decide: buy, sell, or hold."""


def _parse_response(raw: str) -> dict[str, Any]:
    """Extract JSON from model response, handling markdown fences."""
    # Strip markdown fences if present
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    # Find JSON object
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group())
    return json.loads(cleaned)


class RiskScorer:
    """
    Uses 0G Compute (GLM-5-FP8) to score trade risk.

    Usage:
        async with ZeroGComputeClient.from_env() as client:
            scorer = RiskScorer(client)
            decision = await scorer.score(signal_payload, sender_pubkey)
    """

    # If risk_score >= this threshold, action is forced to HOLD
    RISK_THRESHOLD = 7.0

    def __init__(self, client: ZeroGComputeClient) -> None:
        self._client = client

    async def score(
        self,
        signal: dict[str, Any],
        sender_pubkey: str,
        our_pubkey:   str,
    ) -> SwarmMessage:
        """
        Score a market signal. Returns a TRADE_DECISION SwarmMessage
        ready to send to the executor agent.
        """
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": _build_user_prompt(signal)},
        ]

        try:
            raw = await self._client.chat(messages, max_tokens=512)
            result = _parse_response(raw)
        except Exception as exc:
            log.error("risk scorer: inference failed", error=str(exc))
            # On inference failure, HOLD is the safe default
            result = {
                "risk_score": 10.0,
                "action": "hold",
                "confidence": 0.0,
                "reasoning": f"Inference failed: {exc}",
                "rejection_reason": f"Inference error: {exc}",
            }

        risk_score = float(result.get("risk_score", 10.0))
        action_str = result.get("action", "hold").lower()
        confidence = float(result.get("confidence", 0.5))

        # Override to HOLD if risk too high
        if risk_score >= self.RISK_THRESHOLD and action_str != "hold":
            action_str = "hold"
            result["rejection_reason"] = (
                f"Risk score {risk_score:.1f} exceeds threshold {self.RISK_THRESHOLD}"
            )

        action = TradeAction(action_str) if action_str in ("buy", "sell") else TradeAction.HOLD

        # Build TradeDecisionPayload
        payload_kwargs: dict[str, Any] = {
            "action":         action,
            "token_in":       signal.get("token_in", "0x0000000000000000000000000000000000000000"),
            "token_out":      signal.get("token_out", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
            "chain_id":       int(signal.get("chain_id", 8453)),
            "amount_in_wei":  int(signal.get("amount_in_wei", 100_000_000_000_000_000)),
            "risk_score":     risk_score,
            "confidence":     confidence,
        }
        if action == TradeAction.HOLD:
            payload_kwargs["rejection_reason"] = (
                result.get("rejection_reason") or "Risk gate: hold signal"
            )

        decision = TradeDecisionPayload(**payload_kwargs)

        log.info(
            "risk scored",
            action=action.value,
            risk_score=risk_score,
            confidence=confidence,
        )

        return SwarmMessage(
            message_type=MessageType.TRADE_DECISION,
            sender_role=AgentRole.RISK,
            sender_pubkey=our_pubkey,
            payload=decision.model_dump(),
        )