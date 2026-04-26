"""
core/registry.py
In-memory agent registry — maps AgentRole → AXL public key.

Stage 1: keys are bootstrapped by querying each node's /topology.
Stage 2+: this will be backed by 0G Storage KV so agents that restart
          can re-discover each other without out-of-band key exchange.
"""

from __future__ import annotations

import asyncio
from typing import cast

import structlog

from core.axl_client import AXLClient, NodeInfo
from core.schema import AgentRole

log = structlog.get_logger(__name__)


class AgentEndpoint:
    __slots__ = ("role", "api_url", "public_key")

    def __init__(self, role: AgentRole, api_url: str, public_key: str = "") -> None:
        self.role       = role
        self.api_url    = api_url
        self.public_key = public_key

    def __repr__(self) -> str:
        return (
            f"AgentEndpoint(role={self.role.value!r}, "
            f"pubkey={self.public_key[:8]}…)"
        )


class AgentRegistry:
    """
    Discovers and caches the public keys of all swarm agents.

    bootstrap() must be called before any lookup.
    After bootstrap, call pubkey_for(role) to resolve peers.
    """

    # Default API URLs matching the local AXL node configs
    _DEFAULT_ENDPOINTS: dict[AgentRole, str] = {
        AgentRole.RESEARCHER: "http://127.0.0.1:9002",
        AgentRole.RISK:       "http://127.0.0.1:9012",
        AgentRole.EXECUTOR:   "http://127.0.0.1:9022",
    }

    def __init__(
        self,
        endpoints: dict[AgentRole, str] | None = None,
    ) -> None:
        self._endpoints: dict[AgentRole, AgentEndpoint] = {
            role: AgentEndpoint(role, url)
            for role, url in (endpoints or self._DEFAULT_ENDPOINTS).items()
        }
        self._bootstrapped = False

    async def bootstrap(self) -> None:
        """
        Query /topology on every configured node and store their public keys.
        Runs queries concurrently. Raises if any node is unreachable.
        """
        log.info("registry: bootstrapping…")

        async def fetch(ep: AgentEndpoint) -> None:
            async with AXLClient(ep.api_url, agent_name=ep.role.value) as client:
                info: NodeInfo = await client.topology()
                ep.public_key = info.public_key
                log.info(
                    "registry: discovered agent",
                    role=ep.role.value,
                    pubkey=info.public_key[:16] + "…",
                )

        await asyncio.gather(*[fetch(ep) for ep in self._endpoints.values()])
        self._bootstrapped = True
        log.info("registry: bootstrap complete", count=len(self._endpoints))

    def pubkey_for(self, role: AgentRole) -> str:
        """Return the AXL public key for a given agent role."""
        self._require_bootstrap()
        ep = self._endpoints.get(role)
        if ep is None:
            raise KeyError(f"Unknown agent role: {role!r}")
        if not ep.public_key:
            raise RuntimeError(
                f"Agent {role.value!r} has no public key — bootstrap may have failed"
            )
        return ep.public_key

    def api_url_for(self, role: AgentRole) -> str:
        """Return the local HTTP API URL for a given agent role."""
        ep = self._endpoints.get(role)
        if ep is None:
            raise KeyError(f"Unknown agent role: {role!r}")
        return ep.api_url

    def all_roles(self) -> list[AgentRole]:
        return list(self._endpoints.keys())

    def _require_bootstrap(self) -> None:
        if not self._bootstrapped:
            raise RuntimeError(
                "AgentRegistry has not been bootstrapped. "
                "Call await registry.bootstrap() first."
            )

    def __repr__(self) -> str:
        entries = ", ".join(
            f"{r.value}={ep.public_key[:8]}…" if ep.public_key else f"{r.value}=?"
            for r, ep in self._endpoints.items()
        )
        return f"AgentRegistry({entries})"