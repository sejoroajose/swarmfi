"""
core/schema.py
Canonical message schema for the SwarmFi swarm.

Every message sent over AXL is JSON-serialised to bytes.
All agents speak only these types — nothing ad-hoc.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Enum definitions ─────────────────────────────────────────────────────────


class AgentRole(str, Enum):
    RESEARCHER = "researcher"
    RISK       = "risk"
    EXECUTOR   = "executor"


class MessageType(str, Enum):
    # Lifecycle
    PING            = "ping"
    PONG            = "pong"

    # Research → Risk
    MARKET_SIGNAL   = "market_signal"

    # Risk → Executor
    TRADE_DECISION  = "trade_decision"

    # Executor → all (broadcast)
    EXECUTION_RESULT = "execution_result"

    # Generic error envelope
    ERROR           = "error"


class SignalStrength(str, Enum):
    WEAK   = "weak"
    MEDIUM = "medium"
    STRONG = "strong"


class TradeAction(str, Enum):
    BUY  = "buy"
    SELL = "sell"
    HOLD = "hold"


# ── Base envelope ─────────────────────────────────────────────────────────────


class SwarmMessage(BaseModel):
    """
    Every AXL payload starts with these fields.
    Agents MUST validate this envelope before reading the payload.
    """

    message_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique message ID (UUID4)",
    )
    message_type: MessageType
    sender_role: AgentRole
    sender_pubkey: str = Field(
        description="64-char hex AXL public key of the sending node",
        min_length=64,
        max_length=64,
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sender_pubkey")
    @classmethod
    def pubkey_must_be_hex(cls, v: str) -> str:
        try:
            int(v, 16)
        except ValueError as exc:
            raise ValueError("sender_pubkey must be a 64-char hex string") from exc
        return v.lower()

    def encode(self) -> bytes:
        """Serialise to UTF-8 JSON bytes for AXL /send."""
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def decode(cls, raw: bytes) -> "SwarmMessage":
        """Deserialise bytes received from AXL /recv."""
        return cls.model_validate_json(raw.decode("utf-8"))


# ── Typed payload models ──────────────────────────────────────────────────────


class PingPayload(BaseModel):
    nonce: str = Field(default_factory=lambda: str(uuid.uuid4()))


class PongPayload(BaseModel):
    nonce: str  # echoes the ping nonce
    latency_ms: float | None = None


class MarketSignalPayload(BaseModel):
    token_in:  str   = Field(description="e.g. ETH")
    token_out: str   = Field(description="e.g. USDC")
    chain_id:  int   = Field(description="EIP-155 chain ID")
    price_usd: float = Field(gt=0)
    signal:    SignalStrength
    reason:    str   = Field(max_length=500)
    source_url: str | None = None


class TradeDecisionPayload(BaseModel):
    action:          TradeAction
    token_in:        str
    token_out:       str
    chain_id:        int
    amount_in_wei:   int   = Field(gt=0)
    risk_score:      float = Field(ge=0, le=10)
    confidence:      float = Field(ge=0, le=1)
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def hold_needs_reason(self) -> "TradeDecisionPayload":
        if self.action == TradeAction.HOLD and not self.rejection_reason:
            raise ValueError("HOLD decisions must include a rejection_reason")
        return self


class ExecutionResultPayload(BaseModel):
    success:     bool
    tx_hash:     str | None = None
    error:       str | None = None
    gas_used:    int | None = None
    block_number: int | None = None

    @model_validator(mode="after")
    def result_must_have_detail(self) -> "ExecutionResultPayload":
        if self.success and not self.tx_hash:
            raise ValueError("Successful execution must include tx_hash")
        if not self.success and not self.error:
            raise ValueError("Failed execution must include error")
        return self


class ErrorPayload(BaseModel):
    code:    str
    message: str
    detail:  dict[str, Any] = Field(default_factory=dict)


# ── Convenience constructors ──────────────────────────────────────────────────


def make_ping(sender_role: AgentRole, sender_pubkey: str) -> SwarmMessage:
    nonce = str(uuid.uuid4())
    return SwarmMessage(
        message_type=MessageType.PING,
        sender_role=sender_role,
        sender_pubkey=sender_pubkey,
        payload=PingPayload(nonce=nonce).model_dump(),
    )


def make_pong(
    ping: SwarmMessage,
    sender_role: AgentRole,
    sender_pubkey: str,
    latency_ms: float | None = None,
) -> SwarmMessage:
    nonce = ping.payload.get("nonce", "")
    return SwarmMessage(
        message_type=MessageType.PONG,
        sender_role=sender_role,
        sender_pubkey=sender_pubkey,
        payload=PongPayload(nonce=nonce, latency_ms=latency_ms).model_dump(),
    )