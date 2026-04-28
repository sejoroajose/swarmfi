"""
core/uniswap/models.py
Pydantic models for the Uniswap Trading API.

Covers the full swap lifecycle:
  ApprovalCheck   → /check_approval request/response
  QuoteRequest    → /quote request
  QuoteResponse   → /quote response (routing-aware)
  SwapRequest     → /swap request
  SwapResult      → final result after broadcast

Base URL: https://trade-api.gateway.uniswap.org/v1/
Auth:     x-api-key header
Docs:     https://developers.uniswap.org/docs/trading/swapping-api/integration-guide
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ── Enums ─────────────────────────────────────────────────────────────────────

class RoutingType(str, Enum):
    CLASSIC  = "CLASSIC"
    DUTCH_V2 = "DUTCH_V2"
    DUTCH_V3 = "DUTCH_V3"
    PRIORITY = "PRIORITY"
    WRAP     = "WRAP"
    UNWRAP   = "UNWRAP"
    BRIDGE   = "BRIDGE"


class SwapType(str, Enum):
    EXACT_INPUT  = "EXACT_INPUT"
    EXACT_OUTPUT = "EXACT_OUTPUT"


class SwapStatus(str, Enum):
    PENDING   = "pending"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED    = "failed"
    SKIPPED   = "skipped"   # risk gate blocked the trade


# ── Well-known addresses (Base mainnet, chain 8453) ───────────────────────────

class BaseAddresses:
    """Token addresses on Base (chain 8453) used by SwarmFi."""
    NATIVE_ETH = "0x0000000000000000000000000000000000000000"
    WETH       = "0x4200000000000000000000000000000000000006"
    USDC       = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    USDT       = "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2"

    BASE_CHAIN_ID = 8453


# ── Approval ──────────────────────────────────────────────────────────────────

class ApprovalRequest(BaseModel):
    token:      str = Field(description="ERC-20 token address to approve")
    amount:     str = Field(description="Amount in token's smallest unit (wei)")
    wallet_address: str = Field(alias="walletAddress")
    chain_id:   int = Field(alias="chainId")

    model_config = {"populate_by_name": True}


class TransactionRequest(BaseModel):
    """On-chain transaction ready for signing and broadcast."""
    to:       str
    from_:    str = Field(alias="from")
    data:     str
    value:    str = "0x0"
    chain_id: int = Field(alias="chainId")
    gas_limit: str | None = Field(default=None, alias="gasLimit")
    max_fee_per_gas:          str | None = Field(default=None, alias="maxFeePerGas")
    max_priority_fee_per_gas: str | None = Field(default=None, alias="maxPriorityFeePerGas")

    model_config = {"populate_by_name": True}

    @field_validator("data")
    @classmethod
    def data_must_not_be_empty(cls, v: str) -> str:
        if not v or v in ("", "0x"):
            raise ValueError("Transaction data must not be empty — funds could be lost")
        return v


class ApprovalResponse(BaseModel):
    needs_approval: bool = Field(alias="needsApproval", default=False)
    approval_tx: TransactionRequest | None = Field(default=None, alias="approval")

    model_config = {"populate_by_name": True}


# ── Quote ─────────────────────────────────────────────────────────────────────

class QuoteRequest(BaseModel):
    token_in:       str   = Field(alias="tokenIn")
    token_out:      str   = Field(alias="tokenOut")
    token_in_chain_id:  int = Field(alias="tokenInChainId")
    token_out_chain_id: int = Field(alias="tokenOutChainId")
    amount:         str   = Field(description="In token's smallest unit")
    type:           SwapType = SwapType.EXACT_INPUT
    swapper:        str   = Field(description="Wallet address that will execute the swap")
    slippage_tolerance: float = Field(
        default=0.5,
        alias="slippageTolerance",
        ge=0,
        le=50,
        description="Slippage % (0.5 = 0.5%)",
    )
    protocols: list[str] | None = None

    model_config = {"populate_by_name": True}

    def to_api_dict(self) -> dict[str, Any]:
        """Serialise for the Uniswap API (camelCase keys, no None values)."""
        d = self.model_dump(by_alias=True, exclude_none=True)
        # type enum → string
        d["type"] = self.type.value
        return d


class QuoteResponse(BaseModel):
    """
    Parsed response from POST /quote.
    routing determines whether to call /swap (CLASSIC) or /order (DUTCH_V2/V3/PRIORITY).
    """
    routing:    RoutingType
    quote:      dict[str, Any]
    permit_data: dict[str, Any] | None = Field(default=None, alias="permitData")

    # Convenience fields extracted from the nested quote dict
    amount_out:   str | None = None
    amount_in:    str | None = None
    price_impact: float | None = None
    gas_use_estimate: str | None = None

    model_config = {"populate_by_name": True}

    @property
    def needs_permit(self) -> bool:
        return self.permit_data is not None

    @property
    def use_order_endpoint(self) -> bool:
        """True → call POST /order (UniswapX gasless). False → call POST /swap."""
        return self.routing in (
            RoutingType.DUTCH_V2,
            RoutingType.DUTCH_V3,
            RoutingType.PRIORITY,
        )


# ── Swap ──────────────────────────────────────────────────────────────────────

class SwapRequest(BaseModel):
    """Body for POST /swap (CLASSIC routing)."""
    quote:        dict[str, Any]
    signature:    str | None = None
    permit_data:  dict[str, Any] | None = Field(default=None, alias="permitData")

    model_config = {"populate_by_name": True}

    def to_api_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"quote": self.quote}
        if self.signature and self.permit_data:
            d["signature"]  = self.signature
            d["permitData"] = self.permit_data
        return d


class SwapResponse(BaseModel):
    """Response from POST /swap — contains unsigned transaction."""
    swap: TransactionRequest


# ── Final result ──────────────────────────────────────────────────────────────

class SwapResult(BaseModel):
    """
    Complete record of a swap attempt, written to 0G Storage log.
    Used by ExecutorAgent to report back to researcher via AXL.
    """
    status:       SwapStatus
    tx_hash:      str | None = None
    error:        str | None = None

    # Quote details
    token_in:     str
    token_out:    str
    chain_id:     int
    amount_in:    str
    amount_out:   str | None = None
    routing:      RoutingType | None = None
    price_impact: float | None = None

    # On-chain details (populated after broadcast)
    block_number: int | None = None
    gas_used:     int | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == SwapStatus.CONFIRMED

    def to_log_data(self) -> dict[str, Any]:
        """Serialise for 0G Storage log entry."""
        return self.model_dump(exclude_none=True)