"""
core/storage/kv.py
Key-value store for live swarm state, backed by 0G Storage.

Architecture:
  - Each value is serialised to bytes and uploaded to 0G → root hash returned
  - A manifest (dict[key → root_hash]) is maintained in memory and
    persisted to 0G on every write
  - On startup, agents call load_manifest() to restore the key→hash map
  - Reads hit 0G for every get() to always return fresh data

This gives us:
  - Persistent state that survives agent restarts
  - Shared state visible to all agents simultaneously
  - Full history (every value version lives on 0G forever)

Stage 2 KV keys (defined in models.KVKey):
  swarmfi:state:v1          → SwarmState  (whole-swarm view)
  swarmfi:agent:<role>:v1   → AgentState  (per-agent live state)
  swarmfi:log:index:v1      → LogIndex    (root-hash list for Log)
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from core.storage.client import UploadResult, ZeroGClient

log = structlog.get_logger(__name__)

# Key that holds the manifest itself on 0G
_MANIFEST_KEY = "__swarmfi_manifest__"


class SwarmKV:
    """
    Persistent key-value store backed by 0G Storage.

    Usage:
        async with ZeroGClient.from_env() as zg:
            kv = SwarmKV(zg)
            await kv.load_manifest()          # restore from 0G on startup

            await kv.set("mykey", b"value")   # write
            data = await kv.get("mykey")      # read
            await kv.delete("mykey")          # soft-delete (marks as None)
    """

    def __init__(self, client: ZeroGClient) -> None:
        self._client  = client
        # In-memory manifest: key → 0G root_hash
        self._manifest: dict[str, str] = {}
        # Root hash of the manifest itself (so we can reload it)
        self._manifest_root: str | None = None
        self._log = log.bind(store="kv")

    # ── Manifest management ───────────────────────────────────────────────────

    async def load_manifest(self, manifest_root: str | None = None) -> None:
        if manifest_root is None:
            self._log.info("manifest: starting fresh (no prior root)")
            return
        try:
            raw = await self._client.download(manifest_root)
            decoded = json.loads(raw.decode("utf-8"))
            self._manifest = decoded.get("_keys", decoded) if isinstance(decoded, dict) and "_keys" in decoded else decoded
            self._manifest_root = manifest_root
            self._log.info(
                "manifest: loaded",
                root=manifest_root[:16] + "…",
                keys=len(self._manifest),
            )
        except Exception as exc:
            self._log.warning("manifest: failed to load, starting fresh", error=str(exc))
            self._manifest = {}

    async def _persist_manifest(self) -> str:
        """Upload current manifest to 0G and return its root hash."""
        from datetime import datetime, timezone
        payload = {
            "_ts": datetime.now(tz=timezone.utc).isoformat(),  
            "_keys": self._manifest,
        }
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        result = await self._client.upload(raw)
        self._manifest_root = result.root_hash
        self._log.debug(
            "manifest persisted",
            root=result.root_hash[:16] + "…",
            keys=len(self._manifest),
        )
        return result.root_hash

    @property
    def manifest_root(self) -> str | None:
        """
        Current root hash of the manifest.
        Persist this externally (e.g. in .env or on-chain) so agents can
        call load_manifest(manifest_root) on restart.
        """
        return self._manifest_root

    # ── Core KV operations ────────────────────────────────────────────────────

    async def set(self, key: str, value: bytes) -> UploadResult:
        """
        Upload value bytes to 0G, record root_hash in manifest, persist manifest.
        Returns the UploadResult for the value (root_hash is the permanent ID).
        """
        result = await self._client.upload(value)
        self._manifest[key] = result.root_hash
        await self._persist_manifest()

        self._log.info(
            "kv.set",
            key=key,
            root=result.root_hash[:16] + "…",
            bytes=result.size_bytes,
        )
        return result

    async def get(self, key: str) -> bytes | None:
        """
        Retrieve value bytes from 0G by key.
        Returns None if key not found.
        Always fetches from 0G (not cached) to guarantee freshness.
        """
        root_hash = self._manifest.get(key)
        if root_hash is None:
            self._log.debug("kv.get: key not found", key=key)
            return None

        data = await self._client.download(root_hash)
        self._log.debug(
            "kv.get",
            key=key,
            root=root_hash[:16] + "…",
            bytes=len(data),
        )
        return data

    async def get_or_default(self, key: str, default: bytes) -> bytes:
        """Return value or default if key absent — convenience for agents."""
        value = await self.get(key)
        return value if value is not None else default

    async def delete(self, key: str) -> None:
        """
        Remove key from manifest and persist.
        Note: the data itself remains on 0G (immutable); only the manifest
        reference is removed.
        """
        if key not in self._manifest:
            self._log.debug("kv.delete: key not found", key=key)
            return
        del self._manifest[key]
        await self._persist_manifest()
        self._log.info("kv.delete", key=key)

    async def exists(self, key: str) -> bool:
        return key in self._manifest

    def keys(self) -> list[str]:
        """Return all keys currently in the manifest (in-memory view)."""
        return list(self._manifest.keys())

    async def set_json(self, key: str, obj: Any) -> UploadResult:
        """Serialise obj to JSON and store."""
        return await self.set(key, json.dumps(obj).encode("utf-8"))

    async def get_json(self, key: str) -> Any | None:
        """Retrieve and deserialise JSON. Returns None if key absent."""
        raw = await self.get(key)
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))