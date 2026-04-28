"""
tests/test_swap_executor.py
Unit tests for SwapExecutor in dry-run mode.
No wallet, no network, no real transactions.
"""

import pytest

from core.uniswap.client import UniswapClient
from core.uniswap.executor import SwapExecutor
from core.uniswap.models import BaseAddresses, SwapStatus, SwapType

WALLET = "0x" + "a" * 40
CHAIN  = BaseAddresses.BASE_CHAIN_ID


@pytest.fixture()
def client():
    c = UniswapClient.from_env()
    yield c
    c.reset_mock()


@pytest.fixture()
def executor(client):
    return SwapExecutor(
        client=client,
        wallet_address=WALLET,
        private_key=None,  # dry-run
        dry_run=True,
    )


@pytest.mark.unit
class TestSwapExecutorDryRun:
    def test_is_dry_run_without_private_key(self, executor):
        assert executor.is_dry_run

    @pytest.mark.asyncio
    async def test_execute_swap_returns_result(self, executor, client):
        async with client:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert result is not None

    @pytest.mark.asyncio
    async def test_dry_run_returns_submitted_status(self, executor, client):
        async with client:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        # dry-run returns SUBMITTED (not CONFIRMED — no real broadcast)
        assert result.status == SwapStatus.SUBMITTED

    @pytest.mark.asyncio
    async def test_result_has_correct_token_addresses(self, executor, client):
        async with client:
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
    async def test_result_has_tx_hash(self, executor, client):
        async with client:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert result.tx_hash is not None
        assert result.tx_hash.startswith("0x")

    @pytest.mark.asyncio
    async def test_native_eth_skips_approval_check(self, executor, client):
        """ETH doesn't need Permit2 approval — approval call should not be made."""
        async with client:
            await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        # No approval calls for native ETH
        assert len(client.mock.check_approval_calls) == 0

    @pytest.mark.asyncio
    async def test_erc20_triggers_approval_check(self, executor, client):
        """USDC → ETH swap should trigger approval check."""
        async with client:
            await executor.execute_swap(
                token_in=BaseAddresses.USDC,
                token_out=BaseAddresses.NATIVE_ETH,
                amount_in_wei="1000000000",
                chain_id=CHAIN,
            )
        assert len(client.mock.check_approval_calls) == 1

    @pytest.mark.asyncio
    async def test_quote_is_called_once(self, executor, client):
        async with client:
            await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert len(client.mock.quote_calls) == 1

    @pytest.mark.asyncio
    async def test_swap_is_called_for_classic_routing(self, executor, client):
        """Mock always returns CLASSIC routing → /swap endpoint used."""
        async with client:
            await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        assert len(client.mock.swap_calls) == 1
        assert len(client.mock.order_calls) == 0

    @pytest.mark.asyncio
    async def test_failed_swap_returns_failed_status(self, client):
        """Simulate a failure by passing zero amount."""
        # We need to test that errors are captured, not raised
        executor = SwapExecutor(
            client=client,
            wallet_address="0x" + "0" * 40,
            dry_run=True,
        )

        # patch quote to raise to simulate API error
        original_quote = client._backend.quote

        async def _failing_quote(req):
            raise RuntimeError("No quotes available for this pair")
        client._backend.quote = _failing_quote

        async with client:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1",
                chain_id=CHAIN,
            )

        # Restore
        client._backend.quote = original_quote

        assert result.status == SwapStatus.FAILED
        assert result.error is not None
        assert "No quotes" in result.error

    @pytest.mark.asyncio
    async def test_to_log_data_is_serialisable(self, executor, client):
        """Result must be JSON-serialisable for 0G Storage log."""
        import json
        async with client:
            result = await executor.execute_swap(
                token_in=BaseAddresses.NATIVE_ETH,
                token_out=BaseAddresses.USDC,
                amount_in_wei="1000000000000000000",
                chain_id=CHAIN,
            )
        # Should not raise
        data = result.to_log_data()
        json.dumps(data)


@pytest.mark.unit
class TestSwapExecutorFromEnv:
    def test_from_env_no_wallet_is_dry_run(self, client):
        import os
        old = os.environ.pop("WALLET_ADDRESS", None)
        old_pk = os.environ.pop("WALLET_PRIVATE_KEY", None)
        try:
            executor = SwapExecutor.from_env(client)
            assert executor.is_dry_run
        finally:
            if old:    os.environ["WALLET_ADDRESS"]     = old
            if old_pk: os.environ["WALLET_PRIVATE_KEY"] = old_pk