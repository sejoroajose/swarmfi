"""
tests/test_uniswap_models.py
Unit tests for core/uniswap/models.py — no network, no API key.
"""

import pytest
from pydantic import ValidationError

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

WALLET = "0x" + "a" * 40
CHAIN  = 8453


@pytest.mark.unit
class TestBaseAddresses:
    def test_native_eth_is_zero_address(self):
        assert BaseAddresses.NATIVE_ETH == "0x" + "0" * 40

    def test_base_chain_id(self):
        assert BaseAddresses.BASE_CHAIN_ID == 8453

    def test_usdc_is_valid_address(self):
        assert BaseAddresses.USDC.startswith("0x")
        assert len(BaseAddresses.USDC) == 42


@pytest.mark.unit
class TestTransactionRequest:
    def _valid(self, **overrides) -> dict:
        base = {
            "to":      "0x" + "b" * 40,
            "from":    WALLET,
            "data":    "0xabcdef1234",
            "chainId": CHAIN,
        }
        return base | overrides

    def test_valid_transaction(self):
        tx = TransactionRequest(**self._valid())
        assert tx.to.startswith("0x")
        assert tx.data == "0xabcdef1234"

    def test_empty_data_rejected(self):
        with pytest.raises(ValidationError, match="empty"):
            TransactionRequest(**self._valid(data=""))

    def test_0x_data_rejected(self):
        with pytest.raises(ValidationError, match="empty"):
            TransactionRequest(**self._valid(data="0x"))

    def test_alias_from_field(self):
        tx = TransactionRequest(**self._valid())
        assert tx.from_ == WALLET


@pytest.mark.unit
class TestApprovalRequest:
    def test_valid_approval_request(self):
        req = ApprovalRequest(
            token=BaseAddresses.USDC,
            amount="1000000",
            walletAddress=WALLET,
            chainId=CHAIN,
        )
        assert req.token == BaseAddresses.USDC
        assert req.chain_id == CHAIN

    def test_native_token_approval(self):
        # Native ETH can also be passed (though usually not needed)
        req = ApprovalRequest(
            token=BaseAddresses.NATIVE_ETH,
            amount="1000000000000000000",
            walletAddress=WALLET,
            chainId=CHAIN,
        )
        assert req.token == BaseAddresses.NATIVE_ETH


@pytest.mark.unit
class TestApprovalResponse:
    def test_no_approval_needed(self):
        resp = ApprovalResponse(needsApproval=False)
        assert not resp.needs_approval
        assert resp.approval_tx is None

    def test_approval_needed_with_tx(self):
        tx = TransactionRequest(**{
            "to":      "0x" + "b" * 40,
            "from":    WALLET,
            "data":    "0xdeadbeef",
            "chainId": CHAIN,
        })
        resp = ApprovalResponse(needsApproval=True, approval=tx)
        assert resp.needs_approval
        assert resp.approval_tx is not None


@pytest.mark.unit
class TestQuoteRequest:
    def _valid(self, **overrides) -> dict:
        base = {
            "tokenIn":       BaseAddresses.NATIVE_ETH,
            "tokenOut":      BaseAddresses.USDC,
            "tokenInChainId":  CHAIN,
            "tokenOutChainId": CHAIN,
            "amount":        "1000000000000000000",
            "swapper":       WALLET,
        }
        return base | overrides

    def test_valid_quote_request(self):
        req = QuoteRequest(**self._valid())
        assert req.token_in == BaseAddresses.NATIVE_ETH
        assert req.token_out == BaseAddresses.USDC
        assert req.slippage_tolerance == 0.5

    def test_custom_slippage(self):
        req = QuoteRequest(**self._valid(slippageTolerance=1.0))
        assert req.slippage_tolerance == 1.0

    def test_slippage_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            QuoteRequest(**self._valid(slippageTolerance=51.0))

    def test_to_api_dict_has_camel_case_keys(self):
        req = QuoteRequest(**self._valid())
        d = req.to_api_dict()
        assert "tokenIn"       in d
        assert "tokenOut"      in d
        assert "tokenInChainId" in d
        assert "type"          in d

    def test_to_api_dict_no_none_values(self):
        req = QuoteRequest(**self._valid())
        d = req.to_api_dict()
        assert all(v is not None for v in d.values())

    def test_default_type_is_exact_input(self):
        req = QuoteRequest(**self._valid())
        assert req.type == SwapType.EXACT_INPUT


@pytest.mark.unit
class TestQuoteResponse:
    def _valid_quote(self) -> dict:
        return {
            "tokenIn":  BaseAddresses.NATIVE_ETH,
            "tokenOut": BaseAddresses.USDC,
            "amount":   "1000000000000000000",
            "swapper":  WALLET,
            "amountOut": "1950000000",
        }

    def test_classic_routing(self):
        resp = QuoteResponse(
            routing=RoutingType.CLASSIC,
            quote=self._valid_quote(),
        )
        assert not resp.use_order_endpoint
        assert not resp.needs_permit

    def test_dutch_v2_uses_order_endpoint(self):
        resp = QuoteResponse(
            routing=RoutingType.DUTCH_V2,
            quote=self._valid_quote(),
        )
        assert resp.use_order_endpoint

    def test_dutch_v3_uses_order_endpoint(self):
        resp = QuoteResponse(
            routing=RoutingType.DUTCH_V3,
            quote=self._valid_quote(),
        )
        assert resp.use_order_endpoint

    def test_priority_uses_order_endpoint(self):
        resp = QuoteResponse(
            routing=RoutingType.PRIORITY,
            quote=self._valid_quote(),
        )
        assert resp.use_order_endpoint

    def test_needs_permit_when_permit_data_set(self):
        resp = QuoteResponse(
            routing=RoutingType.CLASSIC,
            quote=self._valid_quote(),
            permitData={"domain": {}, "types": {}, "values": {}},
        )
        assert resp.needs_permit

    def test_no_permit_when_permit_data_none(self):
        resp = QuoteResponse(
            routing=RoutingType.CLASSIC,
            quote=self._valid_quote(),
            permitData=None,
        )
        assert not resp.needs_permit


@pytest.mark.unit
class TestSwapRequest:
    def test_to_api_dict_without_permit(self):
        req = SwapRequest(quote={"amount": "1000"})
        d = req.to_api_dict()
        assert "quote" in d
        assert "signature"  not in d
        assert "permitData" not in d

    def test_to_api_dict_with_permit(self):
        req = SwapRequest(
            quote={"amount": "1000"},
            signature="0x" + "aa" * 65,
            permitData={"domain": {}, "types": {}, "values": {}},
        )
        d = req.to_api_dict()
        assert "signature"  in d
        assert "permitData" in d


@pytest.mark.unit
class TestSwapResult:
    def test_confirmed_is_succeeded(self):
        r = SwapResult(
            status=SwapStatus.CONFIRMED,
            tx_hash="0x" + "aa" * 32,
            token_in=BaseAddresses.NATIVE_ETH,
            token_out=BaseAddresses.USDC,
            chain_id=CHAIN,
            amount_in="1000000000000000000",
        )
        assert r.succeeded

    def test_failed_is_not_succeeded(self):
        r = SwapResult(
            status=SwapStatus.FAILED,
            error="insufficient balance",
            token_in=BaseAddresses.NATIVE_ETH,
            token_out=BaseAddresses.USDC,
            chain_id=CHAIN,
            amount_in="1000000000000000000",
        )
        assert not r.succeeded

    def test_to_log_data_excludes_none(self):
        r = SwapResult(
            status=SwapStatus.CONFIRMED,
            tx_hash="0xabc",
            token_in=BaseAddresses.NATIVE_ETH,
            token_out=BaseAddresses.USDC,
            chain_id=CHAIN,
            amount_in="1000",
        )
        d = r.to_log_data()
        assert None not in d.values()
        assert "tx_hash" in d
        assert "status"  in d

    def test_all_swap_statuses_valid(self):
        for status in SwapStatus:
            kwargs: dict = {
                "status":    status,
                "token_in":  BaseAddresses.NATIVE_ETH,
                "token_out": BaseAddresses.USDC,
                "chain_id":  CHAIN,
                "amount_in": "1000",
            }
            if status == SwapStatus.CONFIRMED:
                kwargs["tx_hash"] = "0xabc"
            elif status == SwapStatus.FAILED:
                kwargs["error"] = "test error"
            SwapResult(**kwargs)