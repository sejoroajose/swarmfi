"""
core/uniswap/client.py
Async Uniswap Trading API client.

Base URL: https://trade-api.gateway.uniswap.org/v1/
Auth:     x-api-key header
Docs:     https://developers.uniswap.org/docs/trading/swapping-api/integration-guide

In offline / test mode (no UNISWAP_API_KEY) the client returns
deterministic mock responses so unit tests never touch the network.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.uniswap.models import (
    ApprovalRequest,
    ApprovalResponse,
    QuoteRequest,
    QuoteResponse,
    RoutingType,
    SwapRequest,
    SwapResponse,
    TransactionRequest,
)

log = structlog.get_logger(__name__)

UNISWAP_API_BASE = "https://trade-api.gateway.uniswap.org/v1"
_REQUEST_TIMEOUT = 15.0


# ── Mock backend (offline / CI) ────────────────────────────────────────────────

class _MockUniswapBackend:
    """
    Returns realistic mock responses without network access.
    Used when UNISWAP_API_KEY is not set.
    Every call is logged so tests can assert on call counts.
    """

    def __init__(self) -> None:
        self.check_approval_calls: list[dict] = []
        self.quote_calls:          list[dict] = []
        self.swap_calls:           list[dict] = []
        self.order_calls:          list[dict] = []

    async def check_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        self.check_approval_calls.append(req.model_dump())
        return ApprovalResponse(needsApproval=False, approval=None)

    async def quote(self, req: QuoteRequest) -> QuoteResponse:
        self.quote_calls.append(req.to_api_dict())
        mock_quote = {
            "tokenIn":        req.token_in,
            "tokenOut":       req.token_out,
            "tokenInChainId": req.token_in_chain_id,
            "tokenOutChainId": req.token_out_chain_id,
            "amount":         req.amount,
            "swapper":        req.swapper,
            "amountOut":      "1950000000",  # ~1950 USDC for 1 ETH mock
            "gasUseEstimate": "150000",
        }
        return QuoteResponse(
            routing=RoutingType.CLASSIC,
            quote=mock_quote,
            permitData=None,
            amount_out="1950000000",
            amount_in=req.amount,
            gas_use_estimate="150000",
        )

    async def swap(self, req: SwapRequest) -> SwapResponse:
        self.swap_calls.append(req.to_api_dict())
        mock_tx = TransactionRequest(**{
            "to":      "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD",
            "from":    req.quote.get("swapper", "0x" + "0" * 40),
            "data":    "0x" + "ab" * 100,
            "value":   req.quote.get("amount", "0"),
            "chainId": req.quote.get("tokenInChainId", 8453),
            "gasLimit": "300000",
        })
        return SwapResponse(swap=mock_tx)

    async def order(self, req: SwapRequest) -> dict[str, Any]:
        self.order_calls.append(req.to_api_dict())
        return {"orderId": "mock-order-" + req.quote.get("amount", "0")[:8]}

    def reset(self) -> None:
        self.check_approval_calls.clear()
        self.quote_calls.clear()
        self.swap_calls.clear()
        self.order_calls.clear()


# ── Real HTTP client ───────────────────────────────────────────────────────────

class _LiveUniswapClient:
    """Hits the real Uniswap Trading API."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "_LiveUniswapClient":
        self._http = httpx.AsyncClient(
            base_url=UNISWAP_API_BASE,
            headers={
                "x-api-key":    self._api_key,
                "Content-Type": "application/json",
                "Accept":       "application/json",
            },
            timeout=httpx.Timeout(_REQUEST_TIMEOUT),
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http:
            await self._http.aclose()

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("Client must be used as async context manager")
        return self._http

    def _retrying(self) -> AsyncRetrying:
        return AsyncRetrying(
            retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            reraise=True,
        )

    async def check_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        async for attempt in self._retrying():
            with attempt:
                resp = await self._client.post(
                    "/check_approval",
                    json=req.model_dump(by_alias=True, exclude_none=True),
                )
                resp.raise_for_status()
        return ApprovalResponse.model_validate(resp.json())

    async def quote(self, req: QuoteRequest) -> QuoteResponse:
        async for attempt in self._retrying():
            with attempt:
                resp = await self._client.post("/quote", json=req.to_api_dict())
                resp.raise_for_status()
        data = resp.json()
        # Extract convenience fields from nested quote object
        quote_obj = data.get("quote", {})
        return QuoteResponse(
            routing=RoutingType(data.get("routing", "CLASSIC")),
            quote=quote_obj,
            permitData=data.get("permitData"),
            amount_out=str(quote_obj.get("outputAmount", {}).get("amount", "")),
            amount_in=str(quote_obj.get("inputAmount", {}).get("amount", "")),
            gas_use_estimate=str(quote_obj.get("gasUseEstimate", "")),
        )

    async def swap(self, req: SwapRequest) -> SwapResponse:
        async for attempt in self._retrying():
            with attempt:
                resp = await self._client.post("/swap", json=req.to_api_dict())
                resp.raise_for_status()
        return SwapResponse.model_validate(resp.json())

    async def order(self, req: SwapRequest) -> dict[str, Any]:
        async for attempt in self._retrying():
            with attempt:
                resp = await self._client.post("/order", json=req.to_api_dict())
                resp.raise_for_status()
        return resp.json()


# ── Public facade ──────────────────────────────────────────────────────────────

class UniswapClient:
    """
    Public Uniswap Trading API client.

    Auto-selects backend:
      - UNISWAP_API_KEY set → real API
      - not set             → mock (for dev/CI)

    Usage:
        async with UniswapClient.from_env() as client:
            quote = await client.quote(QuoteRequest(...))
    """

    def __init__(self, backend: _LiveUniswapClient | _MockUniswapBackend) -> None:
        self._backend = backend
        self._is_live = isinstance(backend, _LiveUniswapClient)

    @classmethod
    def from_env(cls) -> "UniswapClient":
        api_key = os.getenv("UNISWAP_API_KEY", "").strip()
        if api_key:
            log.info("Uniswap client: live Trading API mode")
            return cls(_LiveUniswapClient(api_key))
        log.info("Uniswap client: mock mode (set UNISWAP_API_KEY for live)")
        return cls(_MockUniswapBackend())

    @property
    def is_live(self) -> bool:
        return self._is_live

    async def __aenter__(self) -> "UniswapClient":
        if isinstance(self._backend, _LiveUniswapClient):
            await self._backend.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        if isinstance(self._backend, _LiveUniswapClient):
            await self._backend.__aexit__(*args)

    async def check_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        """Check if Permit2 approval is needed for the token."""
        result = await self._backend.check_approval(req)
        log.info(
            "approval checked",
            token=req.token[:10] + "…",
            needs=result.needs_approval,
        )
        return result

    async def quote(self, req: QuoteRequest) -> QuoteResponse:
        """Get a swap quote. Returns routing type + quote object for /swap or /order."""
        result = await self._backend.quote(req)
        log.info(
            "quote received",
            routing=result.routing.value,
            amount_out=result.amount_out,
            needs_permit=result.needs_permit,
        )
        return result

    async def swap(self, req: SwapRequest) -> SwapResponse:
        """Build unsigned transaction for CLASSIC routing."""
        result = await self._backend.swap(req)
        log.info("swap tx built", to=result.swap.to[:10] + "…")
        return result

    async def order(self, req: SwapRequest) -> dict[str, Any]:
        """Submit UniswapX order for DUTCH/PRIORITY routing (gasless)."""
        result = await self._backend.order(req)
        log.info("UniswapX order submitted", order_id=result.get("orderId", "?"))
        return result

    def reset_mock(self) -> None:
        """Clear mock call history between tests."""
        if isinstance(self._backend, _MockUniswapBackend):
            self._backend.reset()

    @property
    def mock(self) -> _MockUniswapBackend | None:
        """Direct access to mock backend for test assertions."""
        if isinstance(self._backend, _MockUniswapBackend):
            return self._backend
        return None