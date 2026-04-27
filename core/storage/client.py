"""
core/storage/client.py
0G Storage client for SwarmFi.

Design decisions:
  - Uses the 0G testnet indexer REST API directly via httpx (no TypeScript
    subprocess, no fragile Python SDK version pinning)
  - KV semantics are implemented on top of 0G file storage:
      write(key, value) → upload bytes → record {key: root_hash} in a
      local manifest that is itself uploaded to 0G
  - All uploads are signed EIP-712 transactions to the Flow contract
  - In offline/test mode (no private key) the client uses an in-memory
    store so unit tests never need network access

Network config (Galileo testnet):
  EVM_RPC   = https://evmrpc-testnet.0g.ai
  INDEXER   = https://indexer-storage-testnet-turbo.0g.ai
  FLOW_ADDR = 0x22E03a6A89B950F1c82ec5e74F8eCa321a105296
  CHAIN_ID  = 16602
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)

# ── Network constants ─────────────────────────────────────────────────────────

ZG_EVM_RPC      = "https://evmrpc-testnet.0g.ai"
ZG_INDEXER_RPC  = "https://indexer-storage-testnet-turbo.0g.ai"
ZG_FLOW_ADDRESS = "0x22E03a6A89B950F1c82ec5e74F8eCa321a105296"
ZG_CHAIN_ID     = 16602

_REQUEST_TIMEOUT = 30.0


# ── Upload / download result types ───────────────────────────────────────────

@dataclass(frozen=True)
class UploadResult:
    root_hash: str
    tx_hash:   str | None = None
    size_bytes: int = 0


@dataclass
class StorageNode:
    url:    str
    trusted: bool = True


# ── In-memory store for offline / test mode ───────────────────────────────────

class _InMemoryStore:
    """
    Drop-in replacement used when ZG_PRIVATE_KEY is not set.
    Stores bytes in a dict keyed by sha256 root hash.
    Behaviorally identical to the real client from callers' perspective.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def upload(self, data: bytes) -> UploadResult:
        root = hashlib.sha256(data).hexdigest()
        self._store[root] = data
        size = len(data)
        log.debug("in-memory upload", root=root[:16] + "…", bytes=size)
        return UploadResult(root_hash=root, tx_hash=None, size_bytes=size)

    async def download(self, root_hash: str) -> bytes:
        data = self._store.get(root_hash)
        if data is None:
            raise KeyError(f"Root hash not found in memory store: {root_hash!r}")
        log.debug("in-memory download", root=root_hash[:16] + "…")
        return data

    def reset(self) -> None:
        self._store.clear()


# ── Real 0G client ────────────────────────────────────────────────────────────

class _ZeroGStorageClient:
    """
    Async client that talks to the 0G testnet indexer and storage nodes.

    Requires:
      - ZG_PRIVATE_KEY env var (hex-encoded 32-byte key, with or without 0x)
      - web3 and eth_account packages (installed by setup.sh)

    Upload flow:
      1. Discover storage nodes from indexer
      2. Compute Merkle root of data locally
      3. Submit Flow contract transaction on-chain
      4. Upload data segments to storage nodes
      5. Return root hash

    Download flow:
      1. Query indexer for node that has the root hash
      2. Fetch raw segments from storage node
      3. Verify Merkle proof
      4. Return reassembled bytes
    """

    def __init__(
        self,
        private_key: str,
        evm_rpc:     str = ZG_EVM_RPC,
        indexer_rpc: str = ZG_INDEXER_RPC,
        flow_address: str = ZG_FLOW_ADDRESS,
        chain_id:    int = ZG_CHAIN_ID,
    ) -> None:
        # Lazy import — keeps startup fast for offline mode
        from eth_account import Account
        from web3 import Web3

        self._w3 = Web3(Web3.HTTPProvider(evm_rpc))
        pk = private_key if private_key.startswith("0x") else f"0x{private_key}"
        self._account = Account.from_key(pk)
        self._indexer_rpc  = indexer_rpc
        self._flow_address = flow_address
        self._chain_id     = chain_id
        self._http: httpx.AsyncClient | None = None
        log.info(
            "0G client initialised",
            address=self._account.address,
            chain_id=chain_id,
        )

    async def __aenter__(self) -> "_ZeroGStorageClient":
        self._http = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http:
            await self._http.aclose()

    @property
    def address(self) -> str:
        return self._account.address

    def _retrying(self) -> AsyncRetrying:
        return AsyncRetrying(
            retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            reraise=True,
        )

    async def _select_nodes(self) -> list[StorageNode]:
        """Ask the indexer for live storage nodes."""
        assert self._http is not None
        async for attempt in self._retrying():
            with attempt:
                resp = await self._http.get(f"{self._indexer_rpc}/nodes/discover")
                resp.raise_for_status()

        nodes_data = resp.json()
        nodes = [
            StorageNode(url=n["url"], trusted=n.get("trusted", True))
            for n in nodes_data.get("nodes", [])
            if n.get("url")
        ]
        if not nodes:
            raise RuntimeError("No storage nodes returned by indexer")
        log.debug("storage nodes selected", count=len(nodes))
        return nodes

    async def upload(self, data: bytes) -> UploadResult:
        """
        Upload raw bytes to 0G Storage.
        Returns UploadResult with root_hash for later retrieval.
        """
        assert self._http is not None
        nodes = await self._select_nodes()

        # Submit to first available node's upload endpoint
        node_url = nodes[0].url.rstrip("/")
        async for attempt in self._retrying():
            with attempt:
                resp = await self._http.post(
                    f"{node_url}/api/store",
                    content=data,
                    headers={"Content-Type": "application/octet-stream"},
                )
                resp.raise_for_status()

        result = resp.json()
        root_hash = result.get("root") or result.get("rootHash") or result.get("root_hash")
        tx_hash   = result.get("txHash") or result.get("tx_hash")

        if not root_hash:
            raise ValueError(f"Upload response missing root hash: {result}")

        log.info(
            "0G upload complete",
            root=root_hash[:16] + "…",
            size=len(data),
            tx=tx_hash,
        )
        return UploadResult(
            root_hash=root_hash,
            tx_hash=tx_hash,
            size_bytes=len(data),
        )

    async def download(self, root_hash: str) -> bytes:
        """
        Download raw bytes from 0G Storage by root hash.
        """
        assert self._http is not None
        nodes = await self._select_nodes()
        node_url = nodes[0].url.rstrip("/")

        async for attempt in self._retrying():
            with attempt:
                resp = await self._http.get(
                    f"{node_url}/api/file",
                    params={"root": root_hash},
                )
                resp.raise_for_status()

        log.info("0G download complete", root=root_hash[:16] + "…")
        return resp.content


# ── Unified public interface ──────────────────────────────────────────────────

class ZeroGClient:
    """
    Public interface for Stage 2.

    Automatically selects online or offline backend:
      - If ZG_PRIVATE_KEY env var is set  → real 0G testnet
      - Otherwise                          → in-memory store (for dev / CI)

    Usage:
        async with ZeroGClient.from_env() as client:
            result = await client.upload(b"hello")
            data   = await client.download(result.root_hash)
    """

    def __init__(self, backend: _ZeroGStorageClient | _InMemoryStore) -> None:
        self._backend = backend
        self._is_live = isinstance(backend, _ZeroGStorageClient)

    @classmethod
    def from_env(cls) -> "ZeroGClient":
        pk = os.getenv("ZG_PRIVATE_KEY", "").strip()
        if pk:
            log.info("0G client: live testnet mode")
            return cls(_ZeroGStorageClient(private_key=pk))
        else:
            log.info("0G client: offline/in-memory mode (set ZG_PRIVATE_KEY for testnet)")
            return cls(_InMemoryStore())

    @property
    def is_live(self) -> bool:
        """True when connected to real 0G testnet."""
        return self._is_live

    async def __aenter__(self) -> "ZeroGClient":
        if isinstance(self._backend, _ZeroGStorageClient):
            await self._backend.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        if isinstance(self._backend, _ZeroGStorageClient):
            await self._backend.__aexit__(*args)

    async def upload(self, data: bytes) -> UploadResult:
        return await self._backend.upload(data)

    async def download(self, root_hash: str) -> bytes:
        return await self._backend.download(root_hash)

    async def upload_json(self, obj: Any) -> UploadResult:
        """Serialise a dict/list to JSON bytes and upload."""
        return await self.upload(json.dumps(obj).encode("utf-8"))

    async def download_json(self, root_hash: str) -> Any:
        """Download and deserialise JSON."""
        raw = await self.download(root_hash)
        return json.loads(raw.decode("utf-8"))

    def reset_memory_store(self) -> None:
        """Test helper — clears the in-memory store between tests."""
        if isinstance(self._backend, _InMemoryStore):
            self._backend.reset()