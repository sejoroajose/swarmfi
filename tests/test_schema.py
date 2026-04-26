"""
tests/test_schema.py
Unit tests for core/schema.py — no I/O, no AXL nodes required.
"""

import pytest
from pydantic import ValidationError

from core.schema import (
    AgentRole,
    ErrorPayload,
    ExecutionResultPayload,
    MarketSignalPayload,
    MessageType,
    PingPayload,
    PongPayload,
    SignalStrength,
    SwarmMessage,
    TradeAction,
    TradeDecisionPayload,
    make_ping,
    make_pong,
)

VALID_PUBKEY = "a" * 64
OTHER_PUBKEY = "b" * 64


# ── SwarmMessage envelope ─────────────────────────────────────────────────────

@pytest.mark.unit
class TestSwarmMessageEnvelope:
    def test_minimal_valid_message(self):
        msg = SwarmMessage(
            message_type=MessageType.PING,
            sender_role=AgentRole.RESEARCHER,
            sender_pubkey=VALID_PUBKEY,
        )
        assert msg.message_id is not None
        assert msg.timestamp is not None
        assert msg.payload == {}

    def test_pubkey_normalised_to_lowercase(self):
        msg = SwarmMessage(
            message_type=MessageType.PING,
            sender_role=AgentRole.RESEARCHER,
            sender_pubkey="A" * 64,
        )
        assert msg.sender_pubkey == "a" * 64

    def test_pubkey_too_short_rejected(self):
        with pytest.raises(ValidationError, match="64"):
            SwarmMessage(
                message_type=MessageType.PING,
                sender_role=AgentRole.RESEARCHER,
                sender_pubkey="abc",
            )

    def test_pubkey_non_hex_rejected(self):
        with pytest.raises(ValidationError, match="hex"):
            SwarmMessage(
                message_type=MessageType.PING,
                sender_role=AgentRole.RESEARCHER,
                sender_pubkey="g" * 64,  # 'g' is not hex
            )

    def test_invalid_message_type_rejected(self):
        with pytest.raises(ValidationError):
            SwarmMessage(
                message_type="not_a_type",  # type: ignore[arg-type]
                sender_role=AgentRole.RESEARCHER,
                sender_pubkey=VALID_PUBKEY,
            )

    def test_encode_decode_roundtrip(self):
        original = SwarmMessage(
            message_type=MessageType.MARKET_SIGNAL,
            sender_role=AgentRole.RESEARCHER,
            sender_pubkey=VALID_PUBKEY,
            payload={"token_in": "ETH", "token_out": "USDC"},
        )
        encoded = original.encode()
        assert isinstance(encoded, bytes)

        decoded = SwarmMessage.decode(encoded)
        assert decoded.message_id    == original.message_id
        assert decoded.message_type  == original.message_type
        assert decoded.sender_pubkey == original.sender_pubkey
        assert decoded.payload       == original.payload

    def test_decode_garbage_raises_value_error(self):
        with pytest.raises(Exception):
            SwarmMessage.decode(b"this is not json")

    def test_unique_message_ids(self):
        ids = {
            SwarmMessage(
                message_type=MessageType.PING,
                sender_role=AgentRole.RESEARCHER,
                sender_pubkey=VALID_PUBKEY,
            ).message_id
            for _ in range(50)
        }
        assert len(ids) == 50


# ── Typed payloads ────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestPingPongPayloads:
    def test_ping_has_nonce(self):
        p = PingPayload()
        assert len(p.nonce) > 0

    def test_pong_echoes_nonce(self):
        ping = PingPayload()
        pong = PongPayload(nonce=ping.nonce, latency_ms=1.5)
        assert pong.nonce == ping.nonce

    def test_pong_latency_optional(self):
        pong = PongPayload(nonce="abc")
        assert pong.latency_ms is None


@pytest.mark.unit
class TestMarketSignalPayload:
    def test_valid_signal(self):
        sig = MarketSignalPayload(
            token_in="ETH",
            token_out="USDC",
            chain_id=8453,
            price_usd=3200.0,
            signal=SignalStrength.STRONG,
            reason="RSI divergence detected",
        )
        assert sig.price_usd == 3200.0

    def test_zero_price_rejected(self):
        with pytest.raises(ValidationError):
            MarketSignalPayload(
                token_in="ETH",
                token_out="USDC",
                chain_id=8453,
                price_usd=0,
                signal=SignalStrength.WEAK,
                reason="test",
            )

    def test_negative_price_rejected(self):
        with pytest.raises(ValidationError):
            MarketSignalPayload(
                token_in="ETH",
                token_out="USDC",
                chain_id=8453,
                price_usd=-1.0,
                signal=SignalStrength.WEAK,
                reason="test",
            )


@pytest.mark.unit
class TestTradeDecisionPayload:
    def _valid_buy(self, **overrides):
        base = dict(
            action=TradeAction.BUY,
            token_in="USDC",
            token_out="ETH",
            chain_id=8453,
            amount_in_wei=1_000_000,
            risk_score=3.5,
            confidence=0.8,
        )
        return TradeDecisionPayload(**(base | overrides))

    def test_valid_buy(self):
        d = self._valid_buy()
        assert d.action == TradeAction.BUY

    def test_hold_without_reason_rejected(self):
        with pytest.raises(ValidationError, match="HOLD"):
            self._valid_buy(action=TradeAction.HOLD)

    def test_hold_with_reason_accepted(self):
        d = self._valid_buy(action=TradeAction.HOLD, rejection_reason="risk too high")
        assert d.rejection_reason == "risk too high"

    def test_risk_score_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            self._valid_buy(risk_score=11.0)

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            self._valid_buy(confidence=1.1)

    def test_zero_amount_rejected(self):
        with pytest.raises(ValidationError):
            self._valid_buy(amount_in_wei=0)


@pytest.mark.unit
class TestExecutionResultPayload:
    def test_success_requires_tx_hash(self):
        with pytest.raises(ValidationError, match="tx_hash"):
            ExecutionResultPayload(success=True)

    def test_failure_requires_error(self):
        with pytest.raises(ValidationError, match="error"):
            ExecutionResultPayload(success=False)

    def test_valid_success(self):
        r = ExecutionResultPayload(
            success=True,
            tx_hash="0x" + "a" * 64,
            gas_used=120_000,
        )
        assert r.tx_hash is not None

    def test_valid_failure(self):
        r = ExecutionResultPayload(
            success=False,
            error="insufficient funds",
        )
        assert r.error is not None


# ── Convenience constructors ──────────────────────────────────────────────────

@pytest.mark.unit
class TestConvenienceConstructors:
    def test_make_ping(self):
        msg = make_ping(AgentRole.RESEARCHER, VALID_PUBKEY)
        assert msg.message_type == MessageType.PING
        assert "nonce" in msg.payload

    def test_make_pong_echoes_nonce(self):
        ping = make_ping(AgentRole.RESEARCHER, VALID_PUBKEY)
        pong = make_pong(
            ping=ping,
            sender_role=AgentRole.RISK,
            sender_pubkey=OTHER_PUBKEY,
            latency_ms=2.5,
        )
        assert pong.message_type == MessageType.PONG
        assert pong.payload["nonce"] == ping.payload["nonce"]
        assert pong.payload["latency_ms"] == 2.5

    def test_make_pong_different_ids(self):
        ping = make_ping(AgentRole.RESEARCHER, VALID_PUBKEY)
        pong = make_pong(ping, AgentRole.RISK, OTHER_PUBKEY)
        assert pong.message_id != ping.message_id