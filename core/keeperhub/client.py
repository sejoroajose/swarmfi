"""
core/keeperhub/client.py
Async KeeperHub REST API client — aligned with real API responses.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.keeperhub.models import (
    KHCheckAndExecuteRequest,
    KHContractCallRequest,
    KHCreateWorkflowRequest,
    KHExecuteWorkflowRequest,
    KHExecutionResult,
    KHExecutionStatus,
    KHExecutionStatus_,
    KHTransferRequest,
    KHWorkflow,
)

log = structlog.get_logger(__name__)

KH_BASE_URL      = "https://app.keeperhub.com/api"
_REQUEST_TIMEOUT = 30.0


# ── Mock backend ──────────────────────────────────────────────────────────────

class _MockKeeperHubBackend:
    def __init__(self) -> None:
        self.transfer_calls: list[dict] = []
        self.contract_call_calls: list[dict] = []
        self.check_execute_calls: list[dict] = []
        self.workflow_calls: list[dict] = []
        self.execute_workflow_calls: list[dict] = []
        self._executions: dict[str, KHExecutionStatus_] = {}
        self.fail_next: bool = False

    def _make_execution(self, network: str = "base") -> KHExecutionResult:
        eid = "direct_" + uuid.uuid4().hex[:12]
        status = KHExecutionStatus.FAILED if self.fail_next else KHExecutionStatus.SUCCESS
        self.fail_next = False
        self._executions[eid] = KHExecutionStatus_(**{
            "executionId": eid,
            "status": status,
            "txHash": ("0x" + "ab" * 32) if status == KHExecutionStatus.SUCCESS else None,
            "blockNumber": 12345678 if status == KHExecutionStatus.SUCCESS else None,
            "gasUsed": 150000 if status == KHExecutionStatus.SUCCESS else None,
            "error": "Mock failure" if status == KHExecutionStatus.FAILED else None,
            "explorerUrl": f"https://basescan.org/tx/0x{'ab'*32}" if status == KHExecutionStatus.SUCCESS else None,
        })
        return KHExecutionResult(**{"executionId": eid, "status": KHExecutionStatus.PENDING})

    async def execute_transfer(self, req: KHTransferRequest) -> KHExecutionResult:
        self.transfer_calls.append(req.to_api_dict())
        return self._make_execution(req.network.value)

    async def execute_contract_call(self, req: KHContractCallRequest) -> KHExecutionResult:
        self.contract_call_calls.append(req.to_api_dict())
        return self._make_execution(req.network.value)

    async def execute_check_and_execute(self, req: KHCheckAndExecuteRequest) -> KHExecutionResult:
        self.check_execute_calls.append(req.model_dump())
        return self._make_execution()

    async def get_execution_status(self, execution_id: str) -> KHExecutionStatus_:
        if execution_id not in self._executions:
            raise KeyError(f"Execution not found: {execution_id}")
        return self._executions[execution_id]

    async def get_execution_logs(self, execution_id: str) -> list[dict[str, Any]]:
        return [{"timestamp": "2026-01-01T00:00:00Z", "message": f"Mock log for {execution_id}"}]

    async def create_workflow(self, req: KHCreateWorkflowRequest) -> KHWorkflow:
        wid = uuid.uuid4().hex[:20]  # Real style: no wf_ prefix
        self.workflow_calls.append(req.model_dump())
        return KHWorkflow(
            workflow_id=wid,
            name=req.name,
            description=getattr(req, 'description', None),
        )

    async def execute_workflow(self, req: KHExecuteWorkflowRequest) -> KHExecutionResult:
        self.execute_workflow_calls.append(req.model_dump())
        return self._make_execution()

    def reset(self) -> None:
        self.transfer_calls.clear()
        self.contract_call_calls.clear()
        self.check_execute_calls.clear()
        self.workflow_calls.clear()
        self.execute_workflow_calls.clear()
        self._executions.clear()
        self.fail_next = False


# ── Live client ───────────────────────────────────────────────────────────────

class _LiveKeeperHubClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "_LiveKeeperHubClient":
        self._http = httpx.AsyncClient(
            base_url=KH_BASE_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(_REQUEST_TIMEOUT),
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http:
            await self._http.aclose()

    def _retrying(self) -> AsyncRetrying:
        return AsyncRetrying(
            retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            reraise=True,
        )

    def _unwrap_response(self, data: Any) -> dict:
        """Handle both direct response and {'data': {...}} wrapper."""
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data if isinstance(data, dict) else {}

    # ── Direct Execution ─────────────────────────────────────────────────────

    async def execute_transfer(self, req: KHTransferRequest) -> KHExecutionResult:
        assert self._http
        async for attempt in self._retrying():
            with attempt:
                resp = await self._http.post("/execute/transfer", json=req.to_api_dict())
                resp.raise_for_status()
        data = self._unwrap_response(resp.json())
        return KHExecutionResult(**{
            "executionId": data.get("executionId") or data.get("id", ""),
            "status": KHExecutionStatus(data.get("status", "pending")),
        })

    async def execute_contract_call(self, req: KHContractCallRequest) -> KHExecutionResult:
        assert self._http
        async for attempt in self._retrying():
            with attempt:
                resp = await self._http.post("/execute/contract-call", json=req.to_api_dict())
                resp.raise_for_status()
        data = self._unwrap_response(resp.json())
        return KHExecutionResult(**{
            "executionId": data.get("executionId") or data.get("id", ""),
            "status": KHExecutionStatus(data.get("status", "pending")),
        })

    async def execute_check_and_execute(self, req: KHCheckAndExecuteRequest) -> KHExecutionResult:
        assert self._http
        async for attempt in self._retrying():
            with attempt:
                resp = await self._http.post(
                    "/execute/check-and-execute",
                    json=req.model_dump(by_alias=True, exclude_none=True),
                )
                resp.raise_for_status()
        data = self._unwrap_response(resp.json())
        return KHExecutionResult(executionId=data.get("executionId", ""))

    async def get_execution_status(self, execution_id: str) -> KHExecutionStatus_:
        assert self._http
        async for attempt in self._retrying():
            with attempt:
                resp = await self._http.get(f"/execute/{execution_id}/status")
                resp.raise_for_status()
        data = self._unwrap_response(resp.json())

        raw_status = data.get("status", "pending")
        status_map = {
            "completed": KHExecutionStatus.SUCCESS,
            "error":     KHExecutionStatus.FAILED,
            "pending":   KHExecutionStatus.PENDING,
            "running":   KHExecutionStatus.RUNNING,
        }
        mapped = status_map.get(raw_status, KHExecutionStatus.PENDING)

        return KHExecutionStatus_(**{
            "executionId": execution_id,
            "status": mapped,
            "txHash": data.get("transactionHash"),
            "error": data.get("error"),
            "explorerUrl": data.get("transactionLink"),
        })

    async def get_execution_logs(self, execution_id: str) -> list[dict[str, Any]]:
        assert self._http
        async for attempt in self._retrying():
            with attempt:
                resp = await self._http.get(f"/workflows/executions/{execution_id}/logs")
                resp.raise_for_status()
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    # ── Workflows API ────────────────────────────────────────────────────────

    async def create_workflow(self, req: KHCreateWorkflowRequest) -> KHWorkflow:
        assert self._http
        async for attempt in self._retrying():
            with attempt:
                resp = await self._http.post(
                    "/workflows/create",
                    json=req.model_dump(by_alias=True, exclude_none=True),
                )
                resp.raise_for_status()

        payload = self._unwrap_response(resp.json())
        return KHWorkflow.model_validate(payload)

    async def execute_workflow(self, req: KHExecuteWorkflowRequest) -> KHExecutionResult:
        assert self._http
        async for attempt in self._retrying():
            with attempt:
                resp = await self._http.post(
                    f"/workflow/{req.workflow_id}/execute",
                    json=req.input or {},
                )
                resp.raise_for_status()
        data = self._unwrap_response(resp.json())
        return KHExecutionResult(**{
            "executionId": data.get("executionId", ""),
            "status": KHExecutionStatus(data.get("status", "pending")),
        })


# ── Public facade ─────────────────────────────────────────────────────────────

class KeeperHubClient:
    def __init__(self, backend: _LiveKeeperHubClient | _MockKeeperHubBackend) -> None:
        self._backend = backend
        self._is_live = isinstance(backend, _LiveKeeperHubClient)

    @classmethod
    def from_env(cls) -> "KeeperHubClient":
        api_key = os.getenv("KEEPERHUB_API_KEY", "").strip()
        if api_key:
            log.info("KeeperHub client: live API mode")
            return cls(_LiveKeeperHubClient(api_key))
        log.info("KeeperHub client: mock mode (set KEEPERHUB_API_KEY for live)")
        return cls(_MockKeeperHubBackend())

    @property
    def is_live(self) -> bool:
        return self._is_live

    async def __aenter__(self) -> "KeeperHubClient":
        if isinstance(self._backend, _LiveKeeperHubClient):
            await self._backend.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        if isinstance(self._backend, _LiveKeeperHubClient):
            await self._backend.__aexit__(*args)

    async def execute_transfer(self, req: KHTransferRequest) -> KHExecutionResult:
        result = await self._backend.execute_transfer(req)
        log.info("KH transfer submitted", execution_id=result.execution_id)
        return result

    async def execute_contract_call(self, req: KHContractCallRequest) -> KHExecutionResult:
        result = await self._backend.execute_contract_call(req)
        log.info("KH contract call submitted", execution_id=result.execution_id)
        return result

    async def execute_check_and_execute(self, req: KHCheckAndExecuteRequest) -> KHExecutionResult:
        result = await self._backend.execute_check_and_execute(req)
        log.info("KH check-and-execute submitted", execution_id=result.execution_id)
        return result

    async def get_execution_status(self, execution_id: str) -> KHExecutionStatus_:
        return await self._backend.get_execution_status(execution_id)

    async def get_execution_logs(self, execution_id: str) -> list[dict[str, Any]]:
        return await self._backend.get_execution_logs(execution_id)

    async def wait_for_completion(
        self, execution_id: str, poll_interval: float = 2.0, timeout: float = 120.0
    ) -> KHExecutionStatus_:
        import asyncio
        elapsed = 0.0
        while elapsed < timeout:
            status = await self.get_execution_status(execution_id)
            if status.is_terminal:
                log.info(
                    "KH execution complete",
                    execution_id=execution_id[:16],
                    status=status.status.value,
                )
                return status
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        log.warning("KH execution timed out", execution_id=execution_id[:16])
        return await self.get_execution_status(execution_id)

    async def create_workflow(self, req: KHCreateWorkflowRequest) -> KHWorkflow:
        return await self._backend.create_workflow(req)

    async def execute_workflow(self, req: KHExecuteWorkflowRequest) -> KHExecutionResult:
        return await self._backend.execute_workflow(req)

    def reset_mock(self) -> None:
        if isinstance(self._backend, _MockKeeperHubBackend):
            self._backend.reset()

    @property
    def mock(self) -> _MockKeeperHubBackend | None:
        return self._backend if isinstance(self._backend, _MockKeeperHubBackend) else None