"""
core/storage/client.py
0G Storage client for SwarmFi.

Architecture
============
Live mode  (ZG_PRIVATE_KEY set):
    Delegates to a Node.js sidecar script (zg-sidecar/sidecar.mjs) that
    wraps @0gfoundation/0g-ts-sdk — the official TypeScript SDK maintained
    by 0G Labs.  This sidesteps the broken Python SDK entirely: the Python
    SDK (0g-storage-sdk) installs as the top-level package name 'core',
    which directly collides with SwarmFi's own 'core' package.

    The sidecar is a plain Node.js ESM script — no build step, no binary,
    just `node zg-sidecar/sidecar.mjs upload|download`.

Offline mode (no ZG_PRIVATE_KEY):
    Falls back to an in-memory SHA-256-keyed store so all unit tests run
    without network access or Node.js.

Sidecar protocol
================
    upload:   echo <raw bytes> | node sidecar.mjs upload --key <hex> [--evm <url>] [--indexer <url>]
              stdout → {"rootHash":"0x...","txHash":"0x..."}

    download: node sidecar.mjs download --root <0xhash> [--indexer <url>]
              stdout → raw bytes

Setup
=====
    cd zg-sidecar && npm install
    (scripts/setup.sh does this automatically)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ── Network defaults ──────────────────────────────────────────────────────────

ZG_EVM_RPC     = "https://evmrpc-testnet.0g.ai"
ZG_INDEXER_RPC = "https://indexer-storage-testnet-turbo.0g.ai"

# 0G storage nodes won't replicate sub-segment payloads. Uploads below this
# size submit the flow tx successfully but storage nodes never pick up the
# segments, leaving the file permanently at 0 locations and undownloadable.
_ZG_MIN_UPLOAD_BYTES = 256

# Path to the Node.js sidecar script:
#   repo/core/storage/client.py  →  repo/zg-sidecar/sidecar.mjs
_REPO_ROOT    = Path(__file__).parent.parent.parent
_SIDECAR_DIR  = _REPO_ROOT / "zg-sidecar"
_SIDECAR_MJS  = _SIDECAR_DIR / "sidecar.mjs"


def _frame(data: bytes) -> bytes:
    """Prepend 4-byte length, then pad total to _ZG_MIN_UPLOAD_BYTES."""
    framed = len(data).to_bytes(4, "big") + data
    if len(framed) < _ZG_MIN_UPLOAD_BYTES:
        framed = framed.ljust(_ZG_MIN_UPLOAD_BYTES, b"\x00")
    return framed

def _unframe(data: bytes) -> bytes:
    """Extract original payload using the 4-byte length prefix."""
    if len(data) < 4:
        return data  # unframed legacy data — return as-is
    length = int.from_bytes(data[:4], "big")
    payload = data[4:4 + length]
    if len(payload) != length:
        # Corrupted or legacy unframed data — return raw
        return data
    return payload


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UploadResult:
    root_hash:  str
    tx_hash:    str | None = None
    size_bytes: int = 0


# ── In-memory store (offline / unit-test mode) ────────────────────────────────

class _InMemoryStore:
    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def upload(self, data: bytes) -> UploadResult:
        root = hashlib.sha256(data).hexdigest()
        self._store[root] = data
        log.debug("in-memory upload", root=root[:16] + "…", bytes=len(data))
        return UploadResult(root_hash=root, size_bytes=len(data))

    async def download(self, root_hash: str) -> bytes:
        key  = root_hash.removeprefix("0x")
        data = self._store.get(key) or self._store.get(root_hash)
        if data is None:
            raise KeyError(f"Root hash not found in memory store: {root_hash!r}")
        log.debug("in-memory download", root=root_hash[:16] + "…")
        return data

    def reset(self) -> None:
        self._store.clear()


# ── Live 0G client (delegates to Node.js sidecar) ────────────────────────────

class _ZeroGStorageClient:
    """
    Calls `node zg-sidecar/sidecar.mjs` for all storage operations.

    Why Node.js instead of the Python SDK?
    The Python SDK (0g-storage-sdk==0.3.0) installs its packages under the
    top-level name 'core', which directly shadows SwarmFi's own 'core'
    package.  There is no sys.path trick that fixes a same-name collision.
    The TypeScript SDK has no Python namespace at all.
    """

    def __init__(
        self,
        private_key: str,
        evm_rpc:     str = ZG_EVM_RPC,
        indexer_rpc: str = ZG_INDEXER_RPC,
        sidecar_mjs: Path = _SIDECAR_MJS,
    ) -> None:
        from eth_account import Account
        pk = private_key if private_key.startswith("0x") else f"0x{private_key}"
        self._account     = Account.from_key(pk)
        self._private_key = private_key.removeprefix("0x")
        self._evm_rpc     = evm_rpc
        self._indexer_rpc = indexer_rpc
        self._sidecar     = sidecar_mjs
        self._validate()
        log.info("0G client initialised", address=self._account.address)

    def _validate(self) -> None:
        if not self._sidecar.exists():
            raise FileNotFoundError(
                f"0G sidecar not found at {self._sidecar}.\n"
                "Run:  cd zg-sidecar && npm install\n"
                "Or:   ./scripts/setup.sh"
            )
        node_modules = self._sidecar.parent / "node_modules" / "@0gfoundation"
        if not node_modules.exists():
            raise FileNotFoundError(
                f"Node modules missing at {node_modules.parent}.\n"
                "Run:  cd zg-sidecar && npm install"
            )

    def _run(self, args: list[str], stdin: bytes | None = None) -> bytes:
        """Run the sidecar synchronously (via asyncio.to_thread). Returns stdout as bytes."""
        cmd = ["node", str(self._sidecar), *args]
        result = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            timeout=600,
            cwd=str(self._sidecar.parent),
            text=False,                    # Keep as bytes (safer for binary download)
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            stdout = result.stdout.decode(errors="replace").strip()
            raise RuntimeError(
                f"zg-sidecar failed (code {result.returncode}):\n"
                f"CMD: {' '.join(cmd)}\n"
                f"STDOUT:\n{stdout}\n"
                f"STDERR:\n{stderr}"
            )
        return result.stdout   # already bytes

    async def upload(self, data: bytes) -> UploadResult:
        args = [
            "upload",
            "--key",     self._private_key,
            "--evm",     self._evm_rpc,
            "--indexer", self._indexer_rpc,
        ]
        raw    = await asyncio.to_thread(self._run, args, data)
        # Strip any console.log noise the SDK emits — take only the last JSON line
        lines  = [l for l in raw.decode(errors="replace").splitlines() if l.strip()]
        result = json.loads(lines[-1])
        root   = result.get("rootHash", "")
        if not root:
            raise RuntimeError(f"Upload returned no rootHash: {result}")
        log.info("0G upload complete", root=root[:18] + "…")
        return UploadResult(
            root_hash=root,
            tx_hash=result.get("txHash") or None,
            size_bytes=len(data),
        )

    async def download(self, root_hash: str) -> bytes:
        root = root_hash if root_hash.startswith("0x") else f"0x{root_hash}"
        args = [
            "download",
            "--root",    root,
            "--indexer", self._indexer_rpc,
        ]
        data = await asyncio.to_thread(self._run, args, None)
        log.info("0G download complete", root=root[:18] + "…", bytes=len(data))
        return data


# ── Unified public interface ──────────────────────────────────────────────────

class ZeroGClient:
    """
    Auto-selects backend:
      ZG_PRIVATE_KEY set  →  real 0G testnet via Node.js sidecar
      not set             →  in-memory store (unit tests / CI)

    Padding contract
    ----------------
    All uploads are padded to _ZG_MIN_UPLOAD_BYTES with null bytes.
    All downloads strip trailing null bytes before returning.
    This is safe because every stored value is JSON/UTF-8 text, which
    never legitimately ends in \\x00.
    """

    def __init__(self, backend: _ZeroGStorageClient | _InMemoryStore) -> None:
        self._backend = backend
        self._is_live = isinstance(backend, _ZeroGStorageClient)

    @classmethod
    def from_env(cls) -> "ZeroGClient":
        pk = os.getenv("ZG_PRIVATE_KEY", "").strip()
        if pk:
            log.info("0G client: live testnet mode (Node.js sidecar)")
            return cls(_ZeroGStorageClient(
                private_key=pk,
                evm_rpc=os.getenv("ZG_EVM_RPC", ZG_EVM_RPC),
                indexer_rpc=os.getenv("ZG_INDEXER_RPC", ZG_INDEXER_RPC),
            ))
        log.info("0G client: offline/in-memory mode (set ZG_PRIVATE_KEY for testnet)")
        return cls(_InMemoryStore())

    @property
    def is_live(self) -> bool:
        return self._is_live

    async def __aenter__(self) -> "ZeroGClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def upload(self, data: bytes) -> UploadResult:
        return await self._backend.upload(_frame(data))

    async def download(self, root_hash: str) -> bytes:
        raw = await self._backend.download(root_hash)
        return _unframe(raw)

    async def upload_json(self, obj: Any) -> UploadResult:
        return await self.upload(json.dumps(obj).encode("utf-8"))

    async def download_json(self, root_hash: str) -> Any:
        return json.loads((await self.download(root_hash)).decode("utf-8"))

    def reset_memory_store(self) -> None:
        if isinstance(self._backend, _InMemoryStore):
            self._backend.reset()