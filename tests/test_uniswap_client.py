"""
tests/test_uniswap_client.py
Unit tests for UniswapClient using the built-in mock backend.
No API key required, no network.
"""

import pytest

from core.uniswap.client import UniswapClient
from core.uniswap.models import (
    ApprovalRequest,
    BaseAddresses,
    QuoteRequest,
    RoutingType,
    SwapRequest,
    SwapType,
)

WALLET = "0x" + "a" * 40
CHAIN  = BaseAddresses.BASE_CHAIN_ID


@pytest.fixture()
def client():
    """Fresh mock UniswapClient for each test."""
    c = UniswapClient.from_env()   # no UNISWAP_API_KEY → mock mode
    assert not c.is_live
    yield c
    c.reset_mock()


def _quote_req(**overrides) -> QuoteRequest:
    base = {
        "tokenIn":       BaseAddresses.NATIVE_ETH,
        "tokenOut":      BaseAddresses.USDC,
        "tokenInChainId":  CHAIN,
        "tokenOutChainId": CHAIN,
        "amount":        "1000000000000000000",
        "swapper":       WALLET,
    }
    return QuoteRequest(**(base | overrides))


@pytest.mark.unit
class TestUniswapClientModeDetection:
    def test_no_api_key_gives_mock_client(self, client):
        assert not client.is_live
        assert client.mock is not None

    def test_mock_property_available(self, client):
        mock = client.mock
        assert mock is not None
        assert hasattr(mock, "quote_calls")


@pytest.mark.unit
class TestCheckApproval:
    @pytest.mark.asyncio
    async def test_returns_no_approval_by_default(self, client):
        req = ApprovalRequest(
            token=BaseAddresses.USDC,
            amount="1000000",
            walletAddress=WALLET,
            chainId=CHAIN,
        )
        async with client:
            resp = await client.check_approval(req)
        assert not resp.needs_approval
        assert resp.approval_tx is None

    @pytest.mark.asyncio
    async def test_records_call(self, client):
        req = ApprovalRequest(
            token=BaseAddresses.USDC,
            amount="1000000",
            walletAddress=WALLET,
            chainId=CHAIN,
        )
        async with client:
            await client.check_approval(req)
        assert len(client.mock.check_approval_calls) == 1


@pytest.mark.unit
class TestQuote:
    @pytest.mark.asyncio
    async def test_returns_classic_routing(self, client):
        async with client:
            resp = await client.quote(_quote_req())
        assert resp.routing == RoutingType.CLASSIC

    @pytest.mark.asyncio
    async def test_returns_amount_out(self, client):
        async with client:
            resp = await client.quote(_quote_req())
        assert resp.amount_out is not None
        assert len(resp.amount_out) > 0

    @pytest.mark.asyncio
    async def test_no_permit_in_mock(self, client):
        async with client:
            resp = await client.quote(_quote_req())
        assert not resp.needs_permit
        assert not resp.use_order_endpoint

    @pytest.mark.asyncio
    async def test_records_call(self, client):
        async with client:
            await client.quote(_quote_req())
        assert len(client.mock.quote_calls) == 1

    @pytest.mark.asyncio
    async def test_quote_includes_token_addresses(self, client):
        async with client:
            resp = await client.quote(_quote_req())
        assert resp.quote["tokenIn"]  == BaseAddresses.NATIVE_ETH
        assert resp.quote["tokenOut"] == BaseAddresses.USDC

    @pytest.mark.asyncio
    async def test_multiple_quotes_recorded(self, client):
        async with client:
            await client.quote(_quote_req())
            await client.quote(_quote_req(amount="2000000000000000000"))
        assert len(client.mock.quote_calls) == 2


@pytest.mark.unit
class TestSwap:
    @pytest.mark.asyncio
    async def test_returns_transaction_with_data(self, client):
        async with client:
            quote_resp = await client.quote(_quote_req())
            swap_req = SwapRequest(quote=quote_resp.quote)
            swap_resp = await client.swap(swap_req)

        tx = swap_resp.swap
        assert tx.data.startswith("0x")
        assert len(tx.data) > 2
        assert tx.to.startswith("0x")

    @pytest.mark.asyncio
    async def test_records_swap_call(self, client):
        async with client:
            quote_resp = await client.quote(_quote_req())
            await client.swap(SwapRequest(quote=quote_resp.quote))
        assert len(client.mock.swap_calls) == 1

    @pytest.mark.asyncio
    async def test_swap_chain_id_matches_quote(self, client):
        async with client:
            quote_resp = await client.quote(_quote_req())
            swap_resp = await client.swap(SwapRequest(quote=quote_resp.quote))
        assert swap_resp.swap.chain_id == CHAIN


@pytest.mark.unit
class TestOrder:
    @pytest.mark.asyncio
    async def test_returns_order_id(self, client):
        async with client:
            quote_resp = await client.quote(_quote_req())
            result = await client.order(SwapRequest(quote=quote_resp.quote))
        assert "orderId" in result

    @pytest.mark.asyncio
    async def test_records_order_call(self, client):
        async with client:
            quote_resp = await client.quote(_quote_req())
            await client.order(SwapRequest(quote=quote_resp.quote))
        assert len(client.mock.order_calls) == 1


@pytest.mark.unit
class TestClientReset:
    @pytest.mark.asyncio
    async def test_reset_clears_call_history(self, client):
        async with client:
            await client.quote(_quote_req())
            await client.quote(_quote_req())
        assert len(client.mock.quote_calls) == 2
        client.reset_mock()
        assert len(client.mock.quote_calls) == 0