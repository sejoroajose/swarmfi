"""
tests/test_keeperhub_client.py
Unit tests for KeeperHubClient using the built-in mock backend.
No API key, no network.
"""

import pytest

from core.keeperhub.client import KeeperHubClient
from core.keeperhub.models import (
    KHCondition,
    KHConditionOperator,
    KHCheckAndExecuteRequest,
    KHContractCallRequest,
    KHCreateWorkflowRequest,
    KHExecuteWorkflowRequest,
    KHExecutionStatus,
    KHNetwork,
    KHTransferRequest,
)

WALLET   = "0x" + "a" * 40
CONTRACT = "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD"


@pytest.fixture()
def client():
    c = KeeperHubClient.from_env()  # no KEEPERHUB_API_KEY → mock
    assert not c.is_live
    yield c
    c.reset_mock()


@pytest.mark.unit
class TestKeeperHubClientModeDetection:
    def test_no_key_gives_mock(self, client):
        assert not client.is_live
        assert client.mock is not None


@pytest.mark.unit
class TestExecuteTransfer:
    @pytest.mark.asyncio
    async def test_returns_execution_id(self, client):
        req = KHTransferRequest(
            network=KHNetwork.BASE,
            recipientAddress=WALLET,
            amount="0.1",
        )
        async with client:
            result = await client.execute_transfer(req)
        # Mock now mirrors KH's real /api/execute/transfer response shape.
        assert result.execution_id and result.execution_id.startswith("direct_")

    @pytest.mark.asyncio
    async def test_records_call(self, client):
        req = KHTransferRequest(
            network=KHNetwork.BASE,
            recipientAddress=WALLET,
            amount="0.01",
        )
        async with client:
            await client.execute_transfer(req)
        assert len(client.mock.transfer_calls) == 1
        assert client.mock.transfer_calls[0]["network"] == "base"


@pytest.mark.unit
class TestExecuteContractCall:
    def _req(self, **overrides) -> KHContractCallRequest:
        base = {
            "contractAddress": CONTRACT,
            "network": KHNetwork.BASE,
            "functionName": "execute",
            "calldata": "0xdeadbeef",
            "value": "0x0",
        }
        return KHContractCallRequest(**(base | overrides))

    @pytest.mark.asyncio
    async def test_returns_execution_id(self, client):
        async with client:
            result = await client.execute_contract_call(self._req())
        # Mock mirrors KH's real /api/execute/contract-call response shape.
        assert result.execution_id and result.execution_id.startswith("direct_")

    @pytest.mark.asyncio
    async def test_records_call(self, client):
        async with client:
            await client.execute_contract_call(self._req())
        assert len(client.mock.contract_call_calls) == 1

    @pytest.mark.asyncio
    async def test_multiple_calls_all_recorded(self, client):
        async with client:
            await client.execute_contract_call(self._req())
            await client.execute_contract_call(self._req(calldata="0xabcd"))
        assert len(client.mock.contract_call_calls) == 2


@pytest.mark.unit
class TestGetExecutionStatus:
    @pytest.mark.asyncio
    async def test_status_after_contract_call(self, client):
        req = KHContractCallRequest(
            contractAddress=CONTRACT,
            network=KHNetwork.BASE,
            functionName="execute",
        )
        async with client:
            result = await client.execute_contract_call(req)
            status = await client.get_execution_status(result.execution_id)

        assert status.execution_id == result.execution_id
        assert status.status in KHExecutionStatus.__members__.values()

    @pytest.mark.asyncio
    async def test_unknown_execution_id_raises(self, client):
        async with client:
            with pytest.raises(KeyError):
                await client.get_execution_status("exec_nonexistent")

    @pytest.mark.asyncio
    async def test_mock_success_has_tx_hash(self, client):
        req = KHTransferRequest(
            network=KHNetwork.BASE,
            recipientAddress=WALLET,
            amount="1.0",
        )
        async with client:
            result = await client.execute_transfer(req)
            status = await client.get_execution_status(result.execution_id)

        # Mock always succeeds by default
        assert status.status == KHExecutionStatus.SUCCESS
        assert status.tx_hash is not None
        assert status.tx_hash.startswith("0x")
        assert status.block_number is not None
        assert status.gas_used is not None

    @pytest.mark.asyncio
    async def test_mock_failure_when_fail_next_set(self, client):
        client.mock.fail_next = True
        req = KHTransferRequest(
            network=KHNetwork.BASE,
            recipientAddress=WALLET,
            amount="1.0",
        )
        async with client:
            result = await client.execute_transfer(req)
            status = await client.get_execution_status(result.execution_id)

        assert status.status == KHExecutionStatus.FAILED
        assert status.error is not None
        assert status.tx_hash is None


@pytest.mark.unit
class TestWaitForCompletion:
    @pytest.mark.asyncio
    async def test_returns_terminal_status_immediately(self, client):
        """Mock returns terminal state immediately — no polling needed."""
        req = KHContractCallRequest(
            contractAddress=CONTRACT,
            network=KHNetwork.BASE,
            functionName="execute",
            calldata="0xdeadbeef",
        )
        async with client:
            result = await client.execute_contract_call(req)
            status = await client.wait_for_completion(
                result.execution_id,
                poll_interval=0.01,
                timeout=5.0,
            )

        assert status.is_terminal
        assert status.succeeded

    @pytest.mark.asyncio
    async def test_timeout_returns_last_status(self, client):
        """
        Simulate an execution that never completes within timeout.
        Override the status to RUNNING to prevent early termination.
        """
        req = KHContractCallRequest(
            contractAddress=CONTRACT,
            network=KHNetwork.BASE,
            functionName="execute",
        )
        async with client:
            result = await client.execute_contract_call(req)
            # Manually set status to RUNNING to prevent wait_for_completion from exiting early
            client.mock._executions[result.execution_id].status = KHExecutionStatus.RUNNING

            status = await client.wait_for_completion(
                result.execution_id,
                poll_interval=0.01,
                timeout=0.05,   # Very short timeout — will expire
            )

        # Should return whatever status was last seen (RUNNING)
        assert status is not None


@pytest.mark.unit
class TestGetExecutionLogs:
    @pytest.mark.asyncio
    async def test_returns_log_list(self, client):
        req = KHTransferRequest(
            network=KHNetwork.BASE,
            recipientAddress=WALLET,
            amount="0.5",
        )
        async with client:
            result = await client.execute_transfer(req)
            logs = await client.get_execution_logs(result.execution_id)

        assert isinstance(logs, list)
        assert len(logs) > 0
        assert "message" in logs[0]


@pytest.mark.unit
class TestCreateWorkflow:
    @pytest.mark.asyncio
    async def test_returns_workflow_with_id(self, client):
        req = KHCreateWorkflowRequest(name="Test Workflow")
        async with client:
            workflow = await client.create_workflow(req)
        # KH returns raw IDs (no `wf_` prefix) — just assert non-empty.
        assert workflow.workflow_id
        assert workflow.name == "Test Workflow"

    @pytest.mark.asyncio
    async def test_records_workflow_creation(self, client):
        req = KHCreateWorkflowRequest(name="Swarm Workflow")
        async with client:
            await client.create_workflow(req)
        assert len(client.mock.workflow_calls) == 1


@pytest.mark.unit
class TestClientReset:
    @pytest.mark.asyncio
    async def test_reset_clears_all_history(self, client):
        req = KHContractCallRequest(
            contractAddress=CONTRACT,
            network=KHNetwork.BASE,
            functionName="execute",
        )
        async with client:
            await client.execute_contract_call(req)
            await client.execute_contract_call(req)

        assert len(client.mock.contract_call_calls) == 2
        client.reset_mock()
        assert len(client.mock.contract_call_calls) == 0
        assert len(client.mock.transfer_calls) == 0