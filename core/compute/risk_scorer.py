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

_SYSTEM_PROMPT = """You are the risk-scoring AI for SwarmFi, an autonomous DeFi swarm.

Your sole job is to assess one market signal and return a single JSON object.
NEVER output prose, markdown fences, or commentary — only the JSON.

Schema:
{
  "risk_score":       <float 0-10>,
  "action":           "buy" | "sell" | "hold",
  "confidence":       <float 0-1>,
  "reasoning":        <string, ≤160 chars, plain text>,
  "rejection_reason": <string if hold, else null>
}

Scoring rubric (use the FULL range, do not anchor on 5):
  0–2  Bluechip pair on a healthy L2, strong directional signal,
       liquid venue, no flagged anomalies. → BUY or SELL.
  3–4  Solid pair, signal is medium-strong. → BUY or SELL.
  5–6  Mixed signal or moderate uncertainty. → BUY/SELL only with high
       confidence; otherwise HOLD.
  7–8  Weak signal, illiquid pair, or adverse conditions. → HOLD.
  9–10 Unknown token, missing critical data, suspected scam. → HOLD.

Important rules:
  - Treat ETH, WETH, USDC, USDT, DAI, cbBTC on Base (chain 8453) as
    bluechip and well-known. Do NOT flag these as "unknown tokens".
  - The signal already contains a live price quote — that is real,
    not a missing data point.
  - "Insufficient information" is NOT a valid reason for HOLD when the
    pair is in the bluechip list above. Score on the directional
    signal strength instead.
  - Be decisive. Defaulting to HOLD because you "cannot verify" is a
    failure mode."""


# Bluechip token registry: chain_id → {address_lower: symbol}
_KNOWN_TOKENS: dict[int, dict[str, str]] = {
    8453: {  # Base
        "0x0000000000000000000000000000000000000000": "ETH",
        "0x4200000000000000000000000000000000000006": "WETH",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
        "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": "USDT",
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": "DAI",
        "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": "cbBTC",
    },
    1: {  # Ethereum mainnet — included for completeness
        "0x0000000000000000000000000000000000000000": "ETH",
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
        "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
    },
}


def _resolve_symbol(addr: str | None, chain_id: int) -> str:
    """Translate a token address to its symbol if known, else short-hex."""
    if not addr:
        return "?"
    a = addr.lower()
    sym = _KNOWN_TOKENS.get(chain_id, {}).get(a)
    if sym:
        return sym
    # Short hex fallback so the LLM has something readable
    return f"{addr[:8]}…{addr[-4:]}"


def _build_user_prompt(signal: dict[str, Any]) -> str:
    chain_id = int(signal.get("chain_id", 8453))
    in_sym   = _resolve_symbol(signal.get("token_in"),  chain_id)
    out_sym  = _resolve_symbol(signal.get("token_out"), chain_id)
    chain_lbl = {8453: "Base", 1: "Ethereum"}.get(chain_id, f"chain {chain_id}")
    bluechip = in_sym in {"ETH", "WETH", "USDC", "USDT", "DAI", "cbBTC"} \
           and out_sym in {"ETH", "WETH", "USDC", "USDT", "DAI", "cbBTC"}
    bluechip_note = "Both tokens are bluechip on this venue." if bluechip else \
                    "At least one token is non-bluechip; weight that in your score."

    return f"""Market signal to assess:

  Pair:        {in_sym} → {out_sym}   ({chain_lbl})
  Live price:  ${float(signal.get('price_usd', 0)):,.2f}
  Strength:    {signal.get('signal', 'medium')}
  Rationale:   {signal.get('reason', '(no rationale)')}
  Size:        {signal.get('amount_in_wei', 0)} wei

  Venue note:  {bluechip_note}

Return the JSON risk assessment now."""


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