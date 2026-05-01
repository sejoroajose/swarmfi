"""
tests/test_keeperhub_executor.py
Unit tests for KeeperHubSwapExecutor.
Uses mock Uniswap client + mock KeeperHub client — no network.
"""

import pytest

from core.keeperhub.client import KeeperHubClient
from core.keeperhub.executor import KeeperHubSwapExecutor
from core.keeperhub.models import KHExecutionStatus
from core.uniswap.client import UniswapClient
from core.uniswap.models import BaseAddresses, SwapStatus

WALLET = "0x" + "a" * 40
CHAIN  = BaseAddresses.BASE_CHAIN_ID


@pytest.fixture()
def uniswap():
    c = UniswapClient.from_env()
    yield c
    c.reset_mock()


@pytest.fixture()
def keeperhub():
    c = KeeperHubClient.from_env()
    yield c
    c.reset_mock()


@pytest.fixture()
def executor(uniswap, keeperhub):
    return KeeperHubSwapExecutor(
        uniswap=uniswap,
        keeperhub=keeperhub,
        wallet_address=WALLET,
    )


@pytest.mark.unit
class TestKeeperHubSwapExecutorSuccess:
    @pytest.mark.asyncio
    async def test_execute_swap_returns_result(self, executor, uniswap, keeperhub):
        async with uniswap, keeperhub:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert result is not None

    @pytest.mark.asyncio
    async def test_confirmed_status_on_success(self, executor, uniswap, keeperhub):
        async with uniswap, keeperhub:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert result.status == SwapStatus.CONFIRMED

    @pytest.mark.asyncio
    async def test_result_has_tx_hash(self, executor, uniswap, keeperhub):
        async with uniswap, keeperhub:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert result.tx_hash is not None
        assert result.tx_hash.startswith("0x")

    @pytest.mark.asyncio
    async def test_result_has_correct_token_addresses(self, executor, uniswap, keeperhub):
        async with uniswap, keeperhub:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="500000000000000000",
                chain_id=CHAIN,
            )
        assert result.token_in  == BaseAddresses.NATIVE_ETH
        assert result.token_out == BaseAddresses.USDC
        assert result.chain_id  == CHAIN
        assert result.amount_in == "500000000000000000"

    @pytest.mark.asyncio
    async def test_uniswap_quote_called_once(self, executor, uniswap, keeperhub):
        async with uniswap, keeperhub:
            await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert len(uniswap.mock.quote_calls) == 1

    @pytest.mark.asyncio
    async def test_uniswap_swap_not_called_in_path_b(self, executor, uniswap, keeperhub):
        """Path B uses Uniswap as price oracle only — /swap is no longer called."""
        async with uniswap, keeperhub:
            await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert len(uniswap.mock.swap_calls)  == 0
        assert len(uniswap.mock.order_calls) == 0

    @pytest.mark.asyncio
    async def test_keeperhub_transfer_submitted(self, executor, uniswap, keeperhub):
        """Path B routes the on-chain commitment through /api/execute/transfer."""
        async with uniswap, keeperhub:
            await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert len(keeperhub.mock.transfer_calls)      == 1
        assert len(keeperhub.mock.contract_call_calls) == 0

    @pytest.mark.asyncio
    async def test_keeperhub_transfer_uses_commitment_network(self, executor, uniswap, keeperhub):
        """Commitment defaults to Sepolia (Turnkey-supported) regardless of quote chain."""
        async with uniswap, keeperhub:
            await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,  # Quote chain = Base
            )
        call = keeperhub.mock.transfer_calls[0]
        assert call["network"] == "sepolia"
        assert call["recipientAddress"].lower() == WALLET.lower()

    @pytest.mark.asyncio
    async def test_result_succeeded_true(self, executor, uniswap, keeperhub):
        async with uniswap, keeperhub:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert result.succeeded

    @pytest.mark.asyncio
    async def test_result_log_data_is_json_serialisable(self, executor, uniswap, keeperhub):
        import json
        async with uniswap, keeperhub:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        json.dumps(result.to_log_data())


@pytest.mark.unit
class TestKeeperHubSwapExecutorFailure:
    @pytest.mark.asyncio
    async def test_kh_failure_returns_failed_result(self, executor, uniswap, keeperhub):
        """When KeeperHub reports failure, result should be FAILED."""
        keeperhub.mock.fail_next = True
        async with uniswap, keeperhub:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert result.status == SwapStatus.FAILED
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_uniswap_error_returns_failed_result(self, executor, uniswap, keeperhub):
        """Uniswap API error should be captured, not raised."""
        original = uniswap._backend.quote

        async def _raise(req):
            raise RuntimeError("Uniswap API unavailable")
        uniswap._backend.quote = _raise

        async with uniswap, keeperhub:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )

        uniswap._backend.quote = original

        assert result.status == SwapStatus.FAILED
        assert "unavailable" in result.error.lower()

    @pytest.mark.asyncio
    async def test_failed_result_has_no_tx_hash(self, executor, uniswap, keeperhub):
        keeperhub.mock.fail_next = True
        async with uniswap, keeperhub:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert not result.succeeded


@pytest.mark.unit
class TestKeeperHubSwapExecutorNativeETH:
    @pytest.mark.asyncio
    async def test_native_eth_does_not_trigger_approval(self, executor, uniswap, keeperhub):
        """Native ETH never needs Permit2 approval."""
        async with uniswap, keeperhub:
            await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert len(uniswap.mock.check_approval_calls) == 0