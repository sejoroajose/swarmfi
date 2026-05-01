from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────

class KHNetwork(str, Enum):
    ETHEREUM = "ethereum"
    BASE     = "base"
    ARBITRUM = "arbitrum"
    POLYGON  = "polygon"
    SEPOLIA  = "sepolia"       # testnet
    BASE_SEPOLIA = "base-sepolia"  # testnet


class KHExecutionStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCESS   = "success"
    COMPLETED = "completed"   # KH returns this synonym from /execute/transfer
    FAILED    = "failed"
    ERROR     = "error"       # KH alternative spelling
    CANCELLED = "cancelled"


class KHWorkflowTriggerType(str, Enum):
    MANUAL   = "manual"
    SCHEDULE = "schedule"
    WEBHOOK  = "webhook"
    EVENT    = "event"


# ── Execution condition (for execute_check_and_execute) ───────────────────────

class KHConditionOperator(str, Enum):
    EQ  = "eq"
    NEQ = "neq"
    GT  = "gt"
    LT  = "lt"
    GTE = "gte"
    LTE = "lte"


class KHCondition(BaseModel):
    operator: KHConditionOperator
    value:    str


# ── Direct execution requests ─────────────────────────────────────────────────

class KHTransferRequest(BaseModel):
    """
    Send ETH or ERC-20 tokens directly — no workflow needed.
    Used for simple value transfers between agents.
    """
    network:           KHNetwork
    recipient_address: str = Field(alias="recipientAddress")
    amount:            str = Field(description="Human-readable amount, e.g. '0.1'")
    token_address:     str | None = Field(
        default=None,
        alias="tokenAddress",
        description="ERC-20 contract address. Omit for native ETH.",
    )

    model_config = {"populate_by_name": True}

    def to_api_dict(self) -> dict[str, Any]:
        d = self.model_dump(by_alias=True, exclude_none=True)
        d["network"] = self.network.value
        return d


class KHContractCallRequest(BaseModel):
    """
    Call any smart contract function.
    KeeperHub auto-detects read vs write and handles gas+signing for writes.

    For Uniswap Universal Router calls, pass:
      contract_address = tx.to       (Universal Router address)
      network          = "base"
      function_name    = "execute"   (main UR entrypoint)
      calldata         = tx.data     (raw hex from Uniswap API)
      value            = tx.value    (ETH value in wei as hex string)
    """
    contract_address: str  = Field(alias="contractAddress")
    network:          KHNetwork
    function_name:    str  = Field(alias="functionName")
    function_args:    str | None = Field(
        default=None,
        alias="functionArgs",
        description="JSON array string of function arguments",
    )
    abi:      str | None = None
    calldata: str | None = Field(
        default=None,
        description="Raw hex calldata — use instead of function_args for Uniswap txs",
    )
    value:    str | None = Field(
        default=None,
        description="ETH value in wei (hex or decimal string)",
    )

    model_config = {"populate_by_name": True}

    def to_api_dict(self) -> dict[str, Any]:
        d = self.model_dump(by_alias=True, exclude_none=True)
        d["network"] = self.network.value
        return d


class KHCheckAndExecuteRequest(BaseModel):
    """
    Read a contract value, evaluate a condition, execute a write if met.
    Used by risk agent to gate swaps behind on-chain checks.
    """
    contract_address: str       = Field(alias="contractAddress")
    network:          KHNetwork
    function_name:    str       = Field(alias="functionName")
    condition:        KHCondition
    action:           dict[str, Any]

    model_config = {"populate_by_name": True}


# ── Execution responses ───────────────────────────────────────────────────────

class KHExecutionResult(BaseModel):
    """Response from any direct execution call."""
    execution_id: str = Field(alias="executionId")
    status:       KHExecutionStatus = KHExecutionStatus.PENDING

    model_config = {"populate_by_name": True}


class KHExecutionStatus_(BaseModel):
    """Response from GET /executions/{id}."""
    execution_id: str             = Field(alias="executionId")
    status:       KHExecutionStatus
    tx_hash:      str | None      = Field(default=None, alias="txHash")
    block_number: int | None      = Field(default=None, alias="blockNumber")
    gas_used:     int | None      = Field(default=None, alias="gasUsed")
    error:        str | None      = None
    explorer_url: str | None      = Field(default=None, alias="explorerUrl")
    created_at:   str | None      = Field(default=None, alias="createdAt")
    completed_at: str | None      = Field(default=None, alias="completedAt")

    model_config = {"populate_by_name": True}

    @property
    def succeeded(self) -> bool:
        return self.status in (KHExecutionStatus.SUCCESS, KHExecutionStatus.COMPLETED)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            KHExecutionStatus.SUCCESS,
            KHExecutionStatus.COMPLETED,
            KHExecutionStatus.FAILED,
            KHExecutionStatus.ERROR,
            KHExecutionStatus.CANCELLED,
        )


# ── Workflow models ────────────────────────────────────────────────────────────

class KHWorkflowNode(BaseModel):
    id:   str
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class KHWorkflowEdge(BaseModel):
    id:     str
    source: str
    target: str


class KHCreateWorkflowRequest(BaseModel):
    name:        str
    description: str | None = None
    project_id:  str | None = Field(default=None, alias="projectId")
    nodes:       list[dict[str, Any]] = Field(default_factory=list)
    edges:       list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class KHWorkflow(BaseModel):
    workflow_id:  str             = Field(alias="id")
    name:         str
    description:  str | None      = None
    status:       str | None      = None
    created_at:   str | None      = Field(default=None, alias="createdAt")

    model_config = {"populate_by_name": True}


class KHExecuteWorkflowRequest(BaseModel):
    workflow_id: str            = Field(alias="workflowId")
    input:       dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


# ── Audit entry written to 0G Storage ────────────────────────────────────────

class KHAuditEntry(BaseModel):
    """
    Permanent record of a KeeperHub execution.
    Written to 0G Storage log after every execution attempt.
    """
    execution_id:  str
    status:        KHExecutionStatus
    tx_hash:       str | None   = None
    block_number:  int | None   = None
    gas_used:      int | None   = None
    error:         str | None   = None
    explorer_url:  str | None   = None
    network:       str          = ""
    contract:      str          = ""
    function_name: str          = ""
    retry_count:   int          = 0
    elapsed_ms:    int          = 0

    @property
    def succeeded(self) -> bool:
        return self.status == KHExecutionStatus.SUCCESS

    def to_log_data(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)

    @property
    def succeeded(self) -> bool:
        return self.status == KHExecutionStatus.SUCCESS