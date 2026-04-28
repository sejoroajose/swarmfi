"""
core/uniswap/executor.py
High-level swap execution pipeline used by the Executor agent.

Full flow per the Uniswap docs:
  1. check_approval()   → approve Permit2 if needed (on-chain tx)
  2. quote()            → get best route + unsigned tx
  3. sign_permit()      → sign Permit2 typed data if quote requires it
  4. swap() or order()  → get final unsigned transaction
  5. validate_tx()      → guard against empty data field
  6. broadcast()        → sign + send via web3
  7. wait_receipt()     → poll for confirmation
  8. return SwapResult  → written to 0G Storage by ExecutorAgent

In offline mode (no WALLET_PRIVATE_KEY) the executor simulates
broadcast and returns a mock SwapResult — safe for tests and demos.
"""

from __future__ import annotations

import os
import time
from typing import Any

import structlog

from core.uniswap.client import UniswapClient
from core.uniswap.models import (
    ApprovalRequest,
    BaseAddresses,
    QuoteRequest,
    RoutingType,
    SwapRequest,
    SwapResult,
    SwapStatus,
    SwapType,
    TransactionRequest,
)

log = structlog.get_logger(__name__)

_GAS_LIMIT_BUFFER = 1.2   # 20% buffer on estimated gas
_QUOTE_MAX_AGE_S  = 30    # refresh quote if older than 30 s
_POLL_INTERVAL_S  = 2.0   # seconds between receipt polls
_MAX_POLL_TRIES   = 30    # ~60 s total wait time


class SwapExecutor:
    """
    Orchestrates the full swap lifecycle.

    Usage:
        executor = SwapExecutor.from_env(uniswap_client)
        result = await executor.execute_swap(
            token_in=BaseAddresses.NATIVE_ETH,
            token_out=BaseAddresses.USDC,
            amount_in_wei="1000000000000000000",  # 1 ETH
            chain_id=8453,
        )
    """

    def __init__(
        self,
        client:       UniswapClient,
        wallet_address: str,
        private_key:  str | None = None,
        dry_run:      bool = False,
    ) -> None:
        self._client         = client
        self._wallet_address = wallet_address
        self._private_key    = private_key
        self._dry_run        = dry_run or (private_key is None)
        self._w3: Any        = None   # web3.Web3 — lazily initialised
        self._log = log.bind(wallet=wallet_address[:10] + "…")

    @classmethod
    def from_env(cls, client: UniswapClient) -> "SwapExecutor":
        """
        Build from environment variables.
        WALLET_ADDRESS   required
        WALLET_PRIVATE_KEY optional (dry-run if absent)
        EVM_RPC          optional (defaults to Base public RPC)
        """
        wallet  = os.getenv("WALLET_ADDRESS", "").strip()
        pk      = os.getenv("WALLET_PRIVATE_KEY", "").strip()
        dry_run = not pk

        if not wallet:
            wallet  = "0x" + "0" * 40
            dry_run = True
            log.warning("WALLET_ADDRESS not set — using zero address, dry-run mode")

        return cls(
            client=client,
            wallet_address=wallet,
            private_key=pk or None,
            dry_run=dry_run,
        )

    @property
    def is_dry_run(self) -> bool:
        return self._dry_run

    # ── Main entry point ──────────────────────────────────────────────────────

    async def execute_swap(
        self,
        token_in:       str,
        token_out:      str,
        amount_in_wei:  str,
        chain_id:       int = BaseAddresses.BASE_CHAIN_ID,
        slippage:       float = 0.5,
        swap_type:      SwapType = SwapType.EXACT_INPUT,
    ) -> SwapResult:
        """
        Execute a swap end-to-end.
        Returns SwapResult regardless of success/failure.
        Never raises — all errors are captured in SwapResult.error.
        """
        started_at = time.monotonic()
        self._log.info(
            "swap: starting",
            token_in=token_in[:10] + "…",
            token_out=token_out[:10] + "…",
            amount=amount_in_wei,
            dry_run=self._dry_run,
        )

        try:
            # Step 1 — check approval
            if token_in != BaseAddresses.NATIVE_ETH:
                await self._ensure_approval(token_in, amount_in_wei, chain_id)

            # Step 2 — get quote
            quote_req = QuoteRequest(
                tokenIn=token_in,
                tokenOut=token_out,
                tokenInChainId=chain_id,
                tokenOutChainId=chain_id,
                amount=amount_in_wei,
                type=swap_type,
                swapper=self._wallet_address,
                slippageTolerance=slippage,
            )
            quote_resp = await self._client.quote(quote_req)

            # Step 3 — sign permit if needed
            signature = None
            if quote_resp.needs_permit and not self._dry_run:
                signature = await self._sign_permit(quote_resp.permit_data)

            # Step 4 — build transaction
            swap_req = SwapRequest(
                quote=quote_resp.quote,
                signature=signature,
                permitData=quote_resp.permit_data,
            )

            if quote_resp.use_order_endpoint:
                return await self._submit_order(swap_req, quote_resp, token_in, token_out, chain_id)
            else:
                return await self._submit_swap(swap_req, quote_resp, token_in, token_out, chain_id, amount_in_wei)

        except Exception as exc:
            elapsed = round(time.monotonic() - started_at, 2)
            self._log.error("swap: failed", error=str(exc), elapsed_s=elapsed)
            return SwapResult(
                status=SwapStatus.FAILED,
                error=str(exc),
                token_in=token_in,
                token_out=token_out,
                chain_id=chain_id,
                amount_in=amount_in_wei,
            )

    # ── Steps ─────────────────────────────────────────────────────────────────

    async def _ensure_approval(
        self, token: str, amount: str, chain_id: int
    ) -> None:
        approval_req = ApprovalRequest(
            token=token,
            amount=amount,
            walletAddress=self._wallet_address,
            chainId=chain_id,
        )
        approval_resp = await self._client.check_approval(approval_req)
        if approval_resp.needs_approval and approval_resp.approval_tx:
            self._log.info("approval: submitting Permit2 approval tx")
            if not self._dry_run:
                await self._broadcast(approval_resp.approval_tx)
            else:
                self._log.info("approval: dry-run — skipping broadcast")

    async def _sign_permit(self, permit_data: dict[str, Any]) -> str:
        """Sign Permit2 typed data EIP-712."""
        if self._dry_run or not self._private_key:
            return "0x" + "ff" * 65  # mock signature

        from eth_account import Account
        from eth_account.messages import encode_typed_data

        account = Account.from_key(self._private_key)
        typed   = encode_typed_data(
            domain_data=permit_data["domain"],
            message_types=permit_data["types"],
            message_data=permit_data["values"],
        )
        signed  = account.sign_message(typed)
        return signed.signature.hex()

    async def _submit_swap(
        self,
        swap_req:   SwapRequest,
        quote_resp: Any,
        token_in:   str,
        token_out:  str,
        chain_id:   int,
        amount_in:  str,
    ) -> SwapResult:
        swap_resp = await self._client.swap(swap_req)
        tx = swap_resp.swap
        self._validate_tx(tx)

        if self._dry_run:
            self._log.info("swap: dry-run — skipping broadcast")
            return SwapResult(
                status=SwapStatus.SUBMITTED,
                tx_hash="0x" + "00" * 32,
                token_in=token_in,
                token_out=token_out,
                chain_id=chain_id,
                amount_in=amount_in,
                amount_out=quote_resp.amount_out,
                routing=quote_resp.routing,
            )

        tx_hash = await self._broadcast(tx)
        receipt = await self._wait_for_receipt(tx_hash)

        return SwapResult(
            status=SwapStatus.CONFIRMED if receipt else SwapStatus.SUBMITTED,
            tx_hash=tx_hash,
            token_in=token_in,
            token_out=token_out,
            chain_id=chain_id,
            amount_in=amount_in,
            amount_out=quote_resp.amount_out,
            routing=quote_resp.routing,
            block_number=receipt.get("blockNumber") if receipt else None,
            gas_used=receipt.get("gasUsed") if receipt else None,
        )

    async def _submit_order(
        self,
        swap_req:   SwapRequest,
        quote_resp: Any,
        token_in:   str,
        token_out:  str,
        chain_id:   int,
    ) -> SwapResult:
        """UniswapX gasless order — submitted to fillers, no local broadcast."""
        result = await self._client.order(swap_req)
        order_id = result.get("orderId", "")
        self._log.info("order: UniswapX order submitted", order_id=order_id[:12])
        return SwapResult(
            status=SwapStatus.SUBMITTED,
            tx_hash=order_id,  # order ID serves as the identifier
            token_in=token_in,
            token_out=token_out,
            chain_id=chain_id,
            amount_in=swap_req.quote.get("amount", "0"),
            amount_out=quote_resp.amount_out,
            routing=quote_resp.routing,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _validate_tx(self, tx: TransactionRequest) -> None:
        """Guard: never broadcast a transaction with empty data."""
        if not tx.data or tx.data in ("", "0x"):
            raise ValueError(
                "Refusing to broadcast: transaction data is empty. "
                "This would result in loss of funds."
            )

    async def _broadcast(self, tx: TransactionRequest) -> str:
        """Sign and broadcast a transaction. Returns tx_hash."""
        w3 = self._get_web3()
        from eth_account import Account

        account = Account.from_key(self._private_key)
        nonce   = w3.eth.get_transaction_count(account.address)

        tx_dict: dict[str, Any] = {
            "to":    tx.to,
            "from":  account.address,
            "data":  tx.data,
            "value": int(tx.value, 16) if tx.value.startswith("0x") else int(tx.value),
            "chainId": tx.chain_id,
            "nonce": nonce,
        }

        if tx.max_fee_per_gas:
            tx_dict["maxFeePerGas"]         = int(tx.max_fee_per_gas, 16)
            tx_dict["maxPriorityFeePerGas"] = int(
                tx.max_priority_fee_per_gas or "0x0", 16
            )
        elif tx.gas_limit:
            tx_dict["gas"] = int(tx.gas_limit, 16)

        signed  = account.sign_transaction(tx_dict)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        self._log.info("broadcast: tx sent", tx_hash=tx_hash.hex()[:18] + "…")
        return tx_hash.hex()

    async def _wait_for_receipt(
        self, tx_hash: str, max_tries: int = _MAX_POLL_TRIES
    ) -> dict[str, Any] | None:
        """Poll for transaction receipt. Returns None on timeout."""
        import asyncio
        w3 = self._get_web3()
        for i in range(max_tries):
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                if receipt:
                    self._log.info(
                        "receipt: confirmed",
                        block=receipt["blockNumber"],
                        gas=receipt["gasUsed"],
                    )
                    return dict(receipt)
            except Exception:
                pass
            await asyncio.sleep(_POLL_INTERVAL_S)
        self._log.warning("receipt: timeout waiting for confirmation")
        return None

    def _get_web3(self) -> Any:
        """Lazy-init web3 connection."""
        if self._w3 is None:
            from web3 import Web3
            rpc = os.getenv("EVM_RPC", "https://mainnet.base.org")
            self._w3 = Web3(Web3.HTTPProvider(rpc))
        return self._w3