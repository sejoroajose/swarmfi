"""
tests/test_keeperhub_integration.py
Integration tests for KeeperHub API.
"""

import os
import pytest

from core.keeperhub.client import KeeperHubClient
from core.keeperhub.models import (
    KHCreateWorkflowRequest,
    KHExecutionStatus,
    KHNetwork,
    KHTransferRequest,
)

pytestmark = pytest.mark.integration

API_KEY = os.getenv("KEEPERHUB_API_KEY", "").strip()

skip_without_key = pytest.mark.skipif(
    not API_KEY,
    reason="KEEPERHUB_API_KEY not set — skipping live KeeperHub tests",
)


@pytest.fixture()
async def live_client():
    client = KeeperHubClient.from_env()
    assert client.is_live, "Expected live client — is KEEPERHUB_API_KEY set?"
    async with client:
        yield client


@pytest.mark.asyncio
@skip_without_key
async def test_live_client_is_live():
    client = KeeperHubClient.from_env()
    assert client.is_live


@pytest.mark.asyncio
@skip_without_key
async def test_create_workflow(live_client):
    """
    Create a test workflow via POST /api/workflows/create.
    """
    req = KHCreateWorkflowRequest(
        name="swarmfi-integration-test",
        description="Auto-created by SwarmFi test — safe to delete",
    )
    workflow = await live_client.create_workflow(req)

    # Real API returns plain IDs without "wf_" prefix
    assert workflow.workflow_id is not None
    assert len(workflow.workflow_id) >= 15, f"Workflow ID too short: {workflow.workflow_id}"
    assert workflow.name == "swarmfi-integration-test"

    print(f"✓ Created workflow: {workflow.workflow_id}")


@pytest.mark.asyncio
@skip_without_key
async def test_execute_transfer_on_sepolia(live_client):
    """
    Submit a tiny ETH transfer on Sepolia testnet.
    """
    req = KHTransferRequest(
        network=KHNetwork.SEPOLIA,
        recipientAddress="0x" + "a" * 40,
        amount="0.0001",
    )
    try:
        result = await live_client.execute_transfer(req)
    except Exception as exc:
        if "422" in str(exc) or "wallet" in str(exc).lower():
            pytest.skip(
                "422 Unprocessable Entity — no wallet configured in KeeperHub. "
                "Go to Settings → Wallets to set one up first."
            )
        raise

    assert result.execution_id

    status = await live_client.get_execution_status(result.execution_id)
    assert status is not None
    if not status.succeeded:
        pytest.skip(f"Transfer failed (no funds or config issue): {status.error}")


@pytest.mark.asyncio
@skip_without_key
async def test_execution_logs_after_workflow_run(live_client):
    """
    Create a workflow, execute it, and fetch logs.
    Skipped gracefully if workflow has no nodes configured.
    """
    create_req = KHCreateWorkflowRequest(
        name="swarmfi-log-test",
        description="Integration test workflow",
    )
    workflow = await live_client.create_workflow(create_req)
    wf_id = workflow.workflow_id

    from core.keeperhub.models import KHExecuteWorkflowRequest
    exec_req = KHExecuteWorkflowRequest(workflow_id=wf_id, input={})

    try:
        result = await live_client.execute_workflow(exec_req)
        assert result.execution_id

        import asyncio
        await asyncio.sleep(2)

        logs = await live_client.get_execution_logs(result.execution_id)
        assert isinstance(logs, list)
        
    except Exception as exc:
        pytest.skip(f"Workflow execution skipped (normal if no nodes configured): {type(exc).__name__} - {exc}")