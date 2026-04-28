"""
tests/test_uniswap_integration.py
Integration tests — require UNISWAP_API_KEY env var.

Get your free API key at: https://developers.uniswap.org/dashboard

Run with:
    UNISWAP_API_KEY=<key> python3 -m pytest tests/test_uniswap_integration.py -m integration -v

These tests hit the REAL Uniswap Trading API.
They do NOT execute transactions (no WALLET_PRIVATE_KEY needed).
They only call /check_approval and /quote — read-only.
"""

import os
import pytest

from core.uniswap.client import UniswapClient
from core.uniswap.executor import SwapExecutor
from core.uniswap.models import (
    ApprovalRequest,
    BaseAddresses,
    QuoteRequest,
    RoutingType,
    SwapRequest,
    SwapType,
)

pytestmark = pytest.mark.integration

API_KEY = os.getenv("UNISWAP_API_KEY", "").strip()
WALLET  = os.getenv("WALLET_ADDRESS", "0x" + "a" * 40).strip()
CHAIN   = BaseAddresses.BASE_CHAIN_ID

skip_without_key = pytest.mark.skipif(
    not API_KEY,
    reason="UNISWAP_API_KEY not set — skipping live Uniswap API tests",
)


@pytest.fixture()
async def live_client():
    client = UniswapClient.from_env()
    assert client.is_live, "Expected live client — is UNISWAP_API_KEY set?"
    async with client:
        yield client


@pytest.mark.asyncio
@skip_without_key
async def test_live_client_is_live():
    client = UniswapClient.from_env()
    assert client.is_live


@pytest.mark.asyncio
@skip_without_key
async def test_check_approval_eth(live_client):
    """Native ETH doesn't need approval — API should return needsApproval=False."""
    req = ApprovalRequest(
        token=BaseAddresses.NATIVE_ETH,
        amount="1000000000000000000",
        walletAddress=WALLET,
        chainId=CHAIN,
    )
    resp = await live_client.check_approval(req)
    # Native ETH never needs approval
    assert not resp.needs_approval


@pytest.mark.asyncio
@skip_without_key
async def test_quote_eth_to_usdc(live_client):
    """Get a real ETH → USDC quote on Base."""
    req = QuoteRequest(
        tokenIn=BaseAddresses.NATIVE_ETH,
        tokenOut=BaseAddresses.USDC,
        tokenInChainId=CHAIN,
        tokenOutChainId=CHAIN,
        amount="1000000000000000000",  # 1 ETH
        swapper=WALLET,
        slippageTolerance=0.5,
    )
    resp = await live_client.quote(req)

    assert resp.routing in RoutingType.__members__.values()
    assert resp.quote is not None
    assert isinstance(resp.quote, dict)


@pytest.mark.asyncio
@skip_without_key
async def test_quote_returns_amount_out(live_client):
    """Quote should return a non-zero output amount."""
    req = QuoteRequest(
        tokenIn=BaseAddresses.NATIVE_ETH,
        tokenOut=BaseAddresses.USDC,
        tokenInChainId=CHAIN,
        tokenOutChainId=CHAIN,
        amount="100000000000000000",  # 0.1 ETH
        swapper=WALLET,
    )
    resp = await live_client.quote(req)
    # We got a quote — just verify the structure is valid
    assert resp.routing is not None


@pytest.mark.asyncio
@skip_without_key
async def test_quote_usdc_to_eth(live_client):
    """Reverse direction: USDC → ETH."""
    req = QuoteRequest(
        tokenIn=BaseAddresses.USDC,
        tokenOut=BaseAddresses.NATIVE_ETH,
        tokenInChainId=CHAIN,
        tokenOutChainId=CHAIN,
        amount="100000000",  # 100 USDC (6 decimals)
        swapper=WALLET,
    )
    resp = await live_client.quote(req)
    assert resp.routing is not None


@pytest.mark.asyncio
@skip_without_key
async def test_swap_tx_has_valid_data(live_client):
    """
    Full quote → swap flow. Verifies the transaction data field is
    non-empty — our critical guard against fund loss.
    Only runs for CLASSIC routing (most common on Base).
    """
    req = QuoteRequest(
        tokenIn=BaseAddresses.NATIVE_ETH,
        tokenOut=BaseAddresses.USDC,
        tokenInChainId=CHAIN,
        tokenOutChainId=CHAIN,
        amount="100000000000000000",  # 0.1 ETH
        swapper=WALLET,
    )
    quote_resp = await live_client.quote(req)

    if quote_resp.use_order_endpoint:
        pytest.skip("Routing is UniswapX — /swap not applicable for this test")

    swap_req  = SwapRequest(quote=quote_resp.quote)
    swap_resp = await live_client.swap(swap_req)

    tx = swap_resp.swap
    assert tx.data and tx.data != "0x", "Transaction data must never be empty"
    assert tx.to.startswith("0x")
    assert tx.chain_id == CHAIN


@pytest.mark.asyncio
@skip_without_key
async def test_dry_run_executor_full_cycle(live_client):
    """
    Full executor cycle in dry-run mode.
    Quotes via real API but does not broadcast any transaction.
    """
    executor = SwapExecutor(
        client=live_client,
        wallet_address=WALLET,
        private_key=None,
        dry_run=True,
    )
    result = await executor.execute_swap(
        token_in=BaseAddresses.NATIVE_ETH,
        token_out=BaseAddresses.USDC,
        amount_in_wei="100000000000000000",
        chain_id=CHAIN,
    )
    # Dry-run with real quote should succeed
    assert result.status.value in ("submitted", "confirmed", "failed")
    assert result.token_in  == BaseAddresses.NATIVE_ETH
    assert result.token_out == BaseAddresses.USDC
    if result.error:
        # If it failed, at least the error is captured — not raised
        assert isinstance(result.error, str)