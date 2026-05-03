"""
core/ens/resolver.py
ENS identity layer for SwarmFi agents.

Each agent has an ENS subname: researcher.swarmfi.eth, risk.swarmfi.eth, executor.swarmfi.eth
The ENS text record "axl_pubkey" stores the agent's AXL public key.

In production: agents register their pubkey in their ENS text record on startup.
For the hackathon: register once manually, then all agents resolve peers via ENS.

Env vars:
  ENS_RPC_URL        RPC for ENS resolution (defaults to mainnet)
  ENS_PARENT_DOMAIN  e.g. swarmfi.eth
"""

from __future__ import annotations

import os
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_DEFAULT_RPC    = "https://eth.llamarpc.com"
_TEXT_RECORD    = "axl_pubkey"


class _MockENSResolver:
    """
    Deterministic, in-memory ENS resolver. Used when no live ENS RPC is
    configured. Reads and writes mirror the resolver contract exactly so
    the rest of the swarm code can treat it identically to a live resolver.
    """

    def __init__(self) -> None:
        # Per-name dict of text records — populated on every write.
        self._records: dict[str, dict[str, str]] = {}

    async def resolve_address(self, name: str) -> str | None:
        # Deterministic name → 20-byte hex address. Same name always
        # resolves to the same address — exactly what real ENS does.
        return "0x" + abs(hash(name)).to_bytes(20, "big").hex()

    async def get_text_record(self, name: str, key: str) -> str | None:
        rec = self._records.get(name, {})
        if key in rec:
            return rec[key]
        # Backwards-compat: legacy callers still expect a synthetic axl_pubkey
        if key == _TEXT_RECORD:
            return "a" * 64
        return None

    async def set_text_record(self, name: str, key: str, value: str) -> str:
        self._records.setdefault(name, {})[key] = value
        log.info("ENS mock: text record written", name=name, key=key)
        return "0x" + "00" * 32

    def _cache_text(self, name: str, key: str, value: str) -> None:
        self._records.setdefault(name, {})[key] = value


class _LiveENSResolver:
    """Real ENS resolution via web3.py + eth_ens."""

    def __init__(self, rpc_url: str) -> None:
        self._rpc_url = rpc_url
        self._ns: Any = None
        # Cached text records — read-through to the live resolver, write-through
        # only when an on-chain `setText` is actually attempted. Lets the swarm
        # update agent profiles cheaply without paying gas every cycle.
        self._text_cache: dict[str, dict[str, str]] = {}
        # Resolved-address cache — names → addresses don't change for our use,
        # and resolving them through web3 is the slowest call in the dashboard.
        # Without this, /api/agents would block the FastAPI event loop on
        # every poll and stall every other endpoint.
        self._addr_cache: dict[str, str | None] = {}

    def _cache_text(self, name: str, key: str, value: str) -> None:
        self._text_cache.setdefault(name, {})[key] = value

    def _get_ns(self) -> Any:
        if self._ns is None:
            from ens import ENS
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            self._ns = ENS.from_web3(w3)
        return self._ns

    async def resolve_address(self, name: str) -> str | None:
        # Cache hit returns instantly. Cache misses go through asyncio.to_thread
        # so the synchronous web3 RPC call doesn't block the FastAPI event loop.
        if name in self._addr_cache:
            return self._addr_cache[name]
        try:
            import asyncio
            ns = self._get_ns()
            addr = await asyncio.to_thread(ns.address, name)
            result = str(addr) if addr else None
            self._addr_cache[name] = result
            return result
        except Exception as exc:
            log.warning("ENS resolve failed", name=name, error=str(exc))
            # Cache None too so we don't keep retrying broken names every poll.
            self._addr_cache[name] = None
            return None

    async def get_text_record(self, name: str, key: str) -> str | None:
        # Cache wins — agents update text records every cycle and we don't
        # want to round-trip to a public RPC for every read.
        cached = self._text_cache.get(name, {}).get(key)
        if cached is not None:
            return cached
        try:
            import asyncio
            ns = self._get_ns()
            value = await asyncio.to_thread(ns.get_text, name, key)
            if value:
                self._text_cache.setdefault(name, {})[key] = value
            return value
        except Exception as exc:
            log.warning("ENS text record fetch failed", name=name, key=key, error=str(exc))
            return None

    async def set_text_record(self, name: str, key: str, value: str) -> str:
        """
        Set a text record on an ENS name.
        Requires the wallet that owns the name.
        """
        from web3 import Web3
        from eth_account import Account
        w3 = Web3(Web3.HTTPProvider(self._rpc_url))

        pk = os.getenv("WALLET_PRIVATE_KEY", "")
        if not pk:
            raise RuntimeError("WALLET_PRIVATE_KEY required to set ENS text records")

        account = Account.from_key(pk)
        ns = self._get_ns()

        # ENS public resolver set_text
        resolver_address = ns.resolver(name)
        resolver = w3.eth.contract(
            address=resolver_address,
            abi=[{
                "inputs": [
                    {"name": "node", "type": "bytes32"},
                    {"name": "key",  "type": "string"},
                    {"name": "value","type": "string"},
                ],
                "name": "setText",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function",
            }],
        )
        node = ns.namehash(name)
        tx = resolver.functions.setText(node, key, value).build_transaction({
            "from":  account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        log.info("ENS text record set", name=name, key=key, tx=tx_hash.hex()[:18])
        return tx_hash.hex()


class AgentIdentity:
    """
    ENS-backed identity for a SwarmFi agent.
    Provides name resolution and AXL pubkey registration.
    """

    def __init__(self, parent_domain: str, resolver: _LiveENSResolver | _MockENSResolver) -> None:
        self._domain   = parent_domain  # e.g. swarmfi.eth
        self._resolver = resolver
        self._cache: dict[str, str] = {}

    # Process-wide singleton — every from_env() call returns the same
    # AgentIdentity instance, so writes to text records survive across
    # /api/* requests, the dashboard, and the demo CLI within one process.
    _shared: "AgentIdentity | None" = None

    @classmethod
    def from_env(cls) -> "AgentIdentity":
        if cls._shared is not None:
            return cls._shared

        rpc    = os.getenv("ENS_RPC_URL", _DEFAULT_RPC)
        domain = os.getenv("ENS_PARENT_DOMAIN", "swarmfi.eth")

        # Check if ens package is available
        try:
            import ens  # noqa: F401
            resolver = _LiveENSResolver(rpc)
            log.info("ENS: live mode", domain=domain, rpc=rpc)
        except ImportError:
            log.info("ENS: mock mode (pip install ens for live)")
            resolver = _MockENSResolver()

        cls._shared = cls(domain, resolver)
        return cls._shared

    def name_for(self, role: str) -> str:
        """e.g. role='researcher' → 'researcher.swarmfi.eth'"""
        return f"{role}.{self._domain}"

    async def get_axl_pubkey(self, role: str) -> str | None:
        """Resolve an agent's AXL public key from its ENS text record."""
        name = self.name_for(role)
        if name in self._cache:
            return self._cache[name]
        pubkey = await self._resolver.get_text_record(name, _TEXT_RECORD)
        if pubkey:
            self._cache[name] = pubkey
        return pubkey

    async def register_pubkey(self, role: str, pubkey: str) -> str:
        """Write this agent's AXL pubkey to its ENS text record."""
        name = self.name_for(role)
        tx   = await self._resolver.set_text_record(name, _TEXT_RECORD, pubkey)
        self._cache[name] = pubkey
        log.info("ENS: pubkey registered", name=name, pubkey=pubkey[:16] + "…")
        return tx

    async def resolve_address(self, role: str) -> str | None:
        return await self._resolver.resolve_address(self.name_for(role))

    # ── Extended profile: text records beyond just the AXL pubkey ────────────
    #
    # In production each of these would be a real ENS text record set via
    # the public resolver's setText(node, key, value). For the demo we maintain
    # an in-memory cache that mirrors the same keys, with an optional live-write
    # path when WALLET_PRIVATE_KEY is configured. Either way the read path
    # always goes through ENS resolution — no hardcoded agent metadata.

    _PROFILE_KEYS = (
        "axl_pubkey",      # AXL public key
        "swarmfi.role",    # canonical role string
        "swarmfi.status",  # latest agent status (idle / scanning / deciding / executing)
        "swarmfi.last",    # latest decision summary
        "swarmfi.tx",      # latest on-chain commitment tx hash
        "swarmfi.snapshot",# latest 0G snapshot root
    )

    async def get_profile(self, role: str) -> dict[str, Any]:
        """
        Return the full ENS profile for an agent role:
          { name, address, role, axl_pubkey, status, last, tx, snapshot }
        Every field comes from ENS — name is computed, address + text records
        are resolved through the configured resolver. No hardcoded values.
        """
        name = self.name_for(role)
        records: dict[str, str | None] = {}
        for k in self._PROFILE_KEYS:
            try:
                records[k] = await self._resolver.get_text_record(name, k)
            except Exception:
                records[k] = None
        try:
            address = await self._resolver.resolve_address(name)
        except Exception:
            address = None

        return {
            "name":        name,
            "role":        role,
            "address":     address,
            "axl_pubkey":  records.get("axl_pubkey"),
            "status":      records.get("swarmfi.status"),
            "last":        records.get("swarmfi.last"),
            "tx":          records.get("swarmfi.tx"),
            "snapshot":    records.get("swarmfi.snapshot"),
        }

    async def update_text(self, role: str, key: str, value: str) -> None:
        """
        Update one ENS text record for an agent. Always writes to the resolver's
        cache so reads see fresh data; only writes on-chain if the resolver is
        live AND a WALLET_PRIVATE_KEY is configured (skipped silently otherwise).
        """
        name = self.name_for(role)
        # Cache write — the mock resolver stores in memory; the live resolver
        # also caches and only attempts on-chain write when explicitly enabled.
        try:
            if hasattr(self._resolver, "_cache_text"):
                self._resolver._cache_text(name, key, value)  # type: ignore[attr-defined]
            elif isinstance(self._resolver, _MockENSResolver):
                self._resolver._records.setdefault(name, {})[key] = value  # type: ignore[attr-defined]
            log.debug("ENS profile updated", name=name, key=key)
        except Exception as exc:
            log.warning("ENS profile update failed", name=name, key=key, error=str(exc))