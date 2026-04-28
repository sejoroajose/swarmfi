"""
core/keeperhub/executor.py
KeeperHub-backed swap executor for SwarmFi.

KeeperHub adds:
  ✓ Automatic retry on gas spikes or RPC failures
  ✓ Gas price optimization (~30% savings vs direct broadcast)
  ✓ Nonce management (safe for concurrent agent txs)
  ✓ MEV protection via private routing
  ✓ Full audit trail (execution_id, tx_hash, gas_used, block_number)
  ✓ 24/7 infrastructure — no self-hosted RPC needed

The KeeperHubSwapExecutor composes with Stage 3's UniswapClient:
  UniswapClient  → builds the swap transaction (route, calldata, permit)
  KeeperHubClient → guarantees the transaction lands onchain
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from core.keeperhub.client import KeeperHubClient
from core.keeperhub.models import (
    KHAuditEntry,
    KHContractCallRequest,
    KHExecutionStatus,
    KHNetwork,
)
from core.storage.client import ZeroGClient
from core.storage.models import LogEventType
from core.uniswap.client import UniswapClient
from core.uniswap.executor import SwapExecutor
from core.uniswap.models import (
    BaseAddresses,
    QuoteRequest,
    SwapRequest,
    SwapResult,
    SwapStatus,
    SwapType,
    TransactionRequest,
)

log = structlog.get_logger(__name__)

# Map EIP-155 chain IDs → KeeperHub network names
_CHAIN_ID_TO_KH_NETWORK: dict[int, KHNetwork] = {
    1:     KHNetwork.ETHEREUM,
    8453:  KHNetwork.BASE,
    42161: KHNetwork.ARBITRUM,
    137:   KHNetwork.POLYGON,
    11155111: KHNetwork.SEPOLIA,
    84532:   KHNetwork.BASE_SEPOLIA,
}


class KeeperHubSwapExecutor:
    """
    Executes Uniswap swaps with guaranteed delivery via KeeperHub.

    Composes UniswapClient (build tx) + KeeperHubClient (broadcast).

    Usage:
        executor = KeeperHubSwapExecutor(uniswap_client, keeperhub_client)
        result   = await executor.execute_swap(
            token_in="0x0000...0000",
            token_out=BaseAddresses.USDC,
            amount_in_wei="1000000000000000000",
            chain_id=8453,
            wallet_address="0xYourWallet",
        )
    """

    def __init__(
        self,
        uniswap:    UniswapClient,
        keeperhub:  KeeperHubClient,
        wallet_address: str = "",
        zg_client:  ZeroGClient | None = None,
    ) -> None:
        self._uniswap   = uniswap
        self._kh        = keeperhub
        self._wallet    = wallet_address
        self._zg        = zg_client
        self._log       = log.bind(wallet=wallet_address[:10] + "…" if wallet_address else "?")

    # ── Main entry point ──────────────────────────────────────────────────────

    async def execute_swap(
        self,
        token_in:       str,
        token_out:      str,
        amount_in_wei:  str,
        chain_id:       int = BaseAddresses.BASE_CHAIN_ID,
        slippage:       float = 0.5,
        swap_type:      SwapType = SwapType.EXACT_INPUT,
        wallet_address: str | None = None,
    ) -> SwapResult:
        """
        Full swap pipeline: quote → build tx → KeeperHub guaranteed broadcast.
        Never raises — all errors captured in SwapResult.error.
        """
        wallet  = wallet_address or self._wallet
        started = time.monotonic()

        self._log.info(
            "KH swap: starting",
            token_in=token_in[:10] + "…",
            token_out=token_out[:10] + "…",
            amount=amount_in_wei,
            chain=chain_id,
        )

        try:
            # Step 1 — Get Uniswap quote
            quote_resp = await self._get_quote(
                token_in, token_out, amount_in_wei, chain_id, slippage, wallet
            )

            # Step 2 — Build unsigned transaction via Uniswap /swap
            if quote_resp.use_order_endpoint:
                # UniswapX gasless — submit via Uniswap /order, no KH needed
                return await self._submit_uniswap_order(
                    quote_resp, token_in, token_out, chain_id, amount_in_wei
                )

            swap_resp = await self._uniswap.swap(
                SwapRequest(quote=quote_resp.quote)
            )
            tx = swap_resp.swap
            self._validate_tx(tx)

            # Step 3 — Submit via KeeperHub for guaranteed execution
            kh_result = await self._submit_via_keeperhub(tx, chain_id)

            # Step 4 — Build audit entry and persist to 0G
            elapsed_ms = round((time.monotonic() - started) * 1000)
            audit = KHAuditEntry(
                execution_id=kh_result.execution_id,
                status=kh_result.status,
                tx_hash=kh_result.tx_hash,
                block_number=kh_result.block_number,
                gas_used=kh_result.gas_used,
                error=kh_result.error,
                explorer_url=kh_result.explorer_url,
                network=_CHAIN_ID_TO_KH_NETWORK.get(chain_id, KHNetwork.BASE).value,
                contract=tx.to,
                function_name="execute",
                elapsed_ms=elapsed_ms,
            )
            await self._write_audit(audit)

            # Step 5 — Return SwapResult
            final_status = (
                SwapStatus.CONFIRMED if kh_result.succeeded
                else SwapStatus.SUBMITTED if kh_result.status == KHExecutionStatus.RUNNING
                else SwapStatus.FAILED
            )

            return SwapResult(
                status=final_status,
                tx_hash=kh_result.tx_hash,
                error=kh_result.error if not kh_result.succeeded else None,
                token_in=token_in,
                token_out=token_out,
                chain_id=chain_id,
                amount_in=amount_in_wei,
                amount_out=quote_resp.amount_out,
                routing=quote_resp.routing,
                block_number=kh_result.block_number,
                gas_used=kh_result.gas_used,
            )

        except Exception as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            self._log.error("KH swap: failed", error=str(exc), elapsed_ms=elapsed_ms)
            return SwapResult(
                status=SwapStatus.FAILED,
                error=str(exc),
                token_in=token_in,
                token_out=token_out,
                chain_id=chain_id,
                amount_in=amount_in_wei,
            )

    # ── Internal steps ────────────────────────────────────────────────────────

    async def _get_quote(
        self,
        token_in:  str,
        token_out: str,
        amount:    str,
        chain_id:  int,
        slippage:  float,
        wallet:    str,
    ) -> Any:
        req = QuoteRequest(
            tokenIn=token_in,
            tokenOut=token_out,
            tokenInChainId=chain_id,
            tokenOutChainId=chain_id,
            amount=amount,
            swapper=wallet or "0x" + "0" * 40,
            slippageTolerance=slippage,
        )
        return await self._uniswap.quote(req)

    async def _submit_via_keeperhub(
        self,
        tx:       TransactionRequest,
        chain_id: int,
    ) -> Any:
        """
        Send the Uniswap-built transaction through KeeperHub.
        KeeperHub handles: gas pricing, nonce, retry, MEV protection.
        """
        network = _CHAIN_ID_TO_KH_NETWORK.get(chain_id, KHNetwork.BASE)

        req = KHContractCallRequest(
            contractAddress=tx.to,
            network=network,
            functionName="execute",    # Universal Router entrypoint
            calldata=tx.data,          # raw ABI-encoded calldata from Uniswap API
            value=tx.value,
        )

        # Submit to KeeperHub
        result = await self._kh.execute_contract_call(req)
        self._log.info(
            "KH: execution submitted",
            execution_id=result.execution_id,
            network=network.value,
        )

        # Poll until terminal
        final = await self._kh.wait_for_completion(
            result.execution_id,
            poll_interval=2.0,
            timeout=120.0,
        )
        return final

    async def _submit_uniswap_order(
        self,
        quote_resp: Any,
        token_in:   str,
        token_out:  str,
        chain_id:   int,
        amount_in:  str,
    ) -> SwapResult:
        """Handle UniswapX gasless orders — no KeeperHub needed."""
        result = await self._uniswap.order(SwapRequest(quote=quote_resp.quote))
        order_id = result.get("orderId", "")
        self._log.info("UniswapX order submitted (gasless)", order_id=order_id[:12])
        return SwapResult(
            status=SwapStatus.SUBMITTED,
            tx_hash=order_id,
            token_in=token_in,
            token_out=token_out,
            chain_id=chain_id,
            amount_in=amount_in,
            amount_out=quote_resp.amount_out,
            routing=quote_resp.routing,
        )

    def _validate_tx(self, tx: TransactionRequest) -> None:
        if not tx.data or tx.data in ("", "0x"):
            raise ValueError(
                "Transaction data is empty — refusing to submit to KeeperHub. "
                "This would result in loss of funds."
            )

    async def _write_audit(self, audit: KHAuditEntry) -> None:
        """Persist KeeperHub execution audit to 0G Storage log."""
        if self._zg is None:
            return
        try:
            from core.storage.kv import SwarmKV
            from core.storage.log import SwarmLog
            kv   = SwarmKV(self._zg)
            slog = SwarmLog(self._zg, kv)
            await slog.append(
                event_type=LogEventType.TRADE_EXECUTED if audit.succeeded else LogEventType.TRADE_FAILED,
                agent_role="executor",
                data=audit.to_log_data(),
            )
        except Exception as exc:
            self._log.warning("KH: failed to write audit to 0G", error=str(exc))