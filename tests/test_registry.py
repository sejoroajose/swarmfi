"""
tests/test_registry.py
Unit tests for core/registry.py.
"""

import pytest
import respx
import httpx

from core.registry import AgentRegistry
from core.schema import AgentRole

PUBKEYS = {
    AgentRole.RESEARCHER: "a" * 64,
    AgentRole.RISK:       "b" * 64,
    AgentRole.EXECUTOR:   "c" * 64,
}

PORTS = {
    AgentRole.RESEARCHER: 9002,
    AgentRole.RISK:       9012,
    AgentRole.EXECUTOR:   9022,
}


def _mock_all_topologies(respx_mock) -> None:
    for role, port in PORTS.items():
        respx_mock.get(f"http://127.0.0.1:{port}/topology").mock(
            return_value=httpx.Response(
                200,
                json={"our_public_key": PUBKEYS[role], "our_ipv6": "200::1"},
            )
        )


@pytest.mark.unit
class TestAgentRegistry:
    @pytest.mark.asyncio
    async def test_bootstrap_populates_all_pubkeys(self, respx_mock):
        _mock_all_topologies(respx_mock)
        registry = AgentRegistry()
        await registry.bootstrap()

        for role in AgentRole:
            assert registry.pubkey_for(role) == PUBKEYS[role]

    @pytest.mark.asyncio
    async def test_pubkey_for_raises_before_bootstrap(self):
        registry = AgentRegistry()
        with pytest.raises(RuntimeError, match="bootstrap"):
            registry.pubkey_for(AgentRole.RESEARCHER)

    @pytest.mark.asyncio
    async def test_api_url_for_returns_correct_url(self):
        registry = AgentRegistry()
        assert registry.api_url_for(AgentRole.RESEARCHER) == "http://127.0.0.1:9002"
        assert registry.api_url_for(AgentRole.RISK)       == "http://127.0.0.1:9012"
        assert registry.api_url_for(AgentRole.EXECUTOR)   == "http://127.0.0.1:9022"

    @pytest.mark.asyncio
    async def test_unknown_role_raises_key_error(self):
        registry = AgentRegistry()
        with pytest.raises(KeyError):
            registry.api_url_for("unknown_role")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_bootstrap_raises_if_node_unreachable(self, respx_mock):
        # Only researcher responds; risk is down
        respx_mock.get("http://127.0.0.1:9002/topology").mock(
            return_value=httpx.Response(
                200,
                json={"our_public_key": PUBKEYS[AgentRole.RESEARCHER], "our_ipv6": "200::1"},
            )
        )
        respx_mock.get("http://127.0.0.1:9012/topology").mock(
            side_effect=httpx.ConnectError("refused")
        )
        respx_mock.get("http://127.0.0.1:9022/topology").mock(
            return_value=httpx.Response(
                200,
                json={"our_public_key": PUBKEYS[AgentRole.EXECUTOR], "our_ipv6": "200::1"},
            )
        )
        registry = AgentRegistry()
        with pytest.raises(httpx.ConnectError):
            await registry.bootstrap()

    @pytest.mark.asyncio
    async def test_custom_endpoints_override_defaults(self, respx_mock):
        custom = {AgentRole.RESEARCHER: "http://10.0.0.1:9002"}
        respx_mock.get("http://10.0.0.1:9002/topology").mock(
            return_value=httpx.Response(
                200,
                json={"our_public_key": PUBKEYS[AgentRole.RESEARCHER], "our_ipv6": "200::1"},
            )
        )
        registry = AgentRegistry(endpoints=custom)
        await registry.bootstrap()
        assert registry.pubkey_for(AgentRole.RESEARCHER) == PUBKEYS[AgentRole.RESEARCHER]

    @pytest.mark.asyncio
    async def test_all_roles_returns_all_three(self):
        registry = AgentRegistry()
        roles = registry.all_roles()
        assert set(roles) == set(AgentRole)