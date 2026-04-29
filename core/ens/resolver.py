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
    """Returns fake but valid ENS data for offline dev."""

    async def resolve_address(self, name: str) -> str | None:
        return "0x" + abs(hash(name)).to_bytes(20, "big").hex()

    async def get_text_record(self, name: str, key: str) -> str | None:
        if key == _TEXT_RECORD:
            return "a" * 64  # fake pubkey
        return None

    async def set_text_record(self, name: str, key: str, value: str) -> str:
        log.info("ENS mock: set text record", name=name, key=key, value=value[:16])
        return "0x" + "00" * 32  # fake tx hash


class _LiveENSResolver:
    """Real ENS resolution via web3.py + eth_ens."""

    def __init__(self, rpc_url: str) -> None:
        self._rpc_url = rpc_url
        self._ns: Any = None

    def _get_ns(self) -> Any:
        if self._ns is None:
            from ens import ENS
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            self._ns = ENS.from_web3(w3)
        return self._ns

    async def resolve_address(self, name: str) -> str | None:
        try:
            ns = self._get_ns()
            addr = ns.address(name)
            return str(addr) if addr else None
        except Exception as exc:
            log.warning("ENS resolve failed", name=name, error=str(exc))
            return None

    async def get_text_record(self, name: str, key: str) -> str | None:
        try:
            ns = self._get_ns()
            return ns.get_text(name, key)
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

    @classmethod
    def from_env(cls) -> "AgentIdentity":
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

        return cls(domain, resolver)

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