"""
core/uniswap
Uniswap Trading API integration for SwarmFi.

Public API:
    from core.uniswap import UniswapClient, SwapExecutor
    from core.uniswap.models import QuoteRequest, SwapResult, BaseAddresses
"""

from core.uniswap.client import UniswapClient
from core.uniswap.executor import SwapExecutor
from core.uniswap.models import (
    ApprovalRequest,
    ApprovalResponse,
    BaseAddresses,
    QuoteRequest,
    QuoteResponse,
    RoutingType,
    SwapRequest,
    SwapResult,
    SwapStatus,
    SwapType,
    TransactionRequest,
)

__all__ = [
    "UniswapClient",
    "SwapExecutor",
    "ApprovalRequest",
    "ApprovalResponse",
    "BaseAddresses",
    "QuoteRequest",
    "QuoteResponse",
    "RoutingType",
    "SwapRequest",
    "SwapResult",
    "SwapStatus",
    "SwapType",
    "TransactionRequest",
]