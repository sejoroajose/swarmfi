"""
tests/test_keeperhub_models.py
Unit tests for core/keeperhub/models.py — no network, no API key.
"""

import pytest
from pydantic import ValidationError

from core.keeperhub.models import (
    KHAuditEntry,
    KHCheckAndExecuteRequest,
    KHCondition,
    KHConditionOperator,
    KHContractCallRequest,
    KHCreateWorkflowRequest,
    KHExecutionStatus,
    KHExecutionStatus_,
    KHNetwork,
    KHTransferRequest,
)

WALLET   = "0x" + "a" * 40
CONTRACT = "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD"


@pytest.mark.unit
class TestKHTransferRequest:
    def test_valid_eth_transfer(self):
        req = KHTransferRequest(
            network=KHNetwork.BASE,
            recipientAddress=WALLET,
            amount="0.1",
        )
        assert req.network == KHNetwork.BASE
        assert req.amount == "0.1"
        assert req.token_address is None

    def test_erc20_transfer(self):
        req = KHTransferRequest(
            network=KHNetwork.BASE,
            recipientAddress=WALLET,
            amount="100",
            tokenAddress="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
        assert req.token_address is not None

    def test_to_api_dict_has_network_string(self):
        req = KHTransferRequest(
            network=KHNetwork.BASE,
            recipientAddress=WALLET,
            amount="1.0",
        )
        d = req.to_api_dict()
        assert d["network"] == "base"
        assert "recipientAddress" in d
        assert "amount" in d

    def test_to_api_dict_excludes_none(self):
        req = KHTransferRequest(
            network=KHNetwork.BASE,
            recipientAddress=WALLET,
            amount="1.0",
        )
        d = req.to_api_dict()
        assert "tokenAddress" not in d

    def test_all_networks_valid(self):
        for net in KHNetwork:
            KHTransferRequest(network=net, recipientAddress=WALLET, amount="1")


@pytest.mark.unit
class TestKHContractCallRequest:
    def _valid(self, **overrides) -> dict:
        base = {
            "contractAddress": CONTRACT,
            "network":         KHNetwork.BASE,
            "functionName":    "execute",
        }
        return base | overrides

    def test_valid_contract_call(self):
        req = KHContractCallRequest(**self._valid())
        assert req.function_name == "execute"
        assert req.contract_address == CONTRACT

    def test_with_calldata(self):
        req = KHContractCallRequest(**self._valid(calldata="0xdeadbeef"))
        assert req.calldata == "0xdeadbeef"

    def test_with_value(self):
        req = KHContractCallRequest(**self._valid(value="0xde0b6b3a7640000"))
        assert req.value is not None

    def test_to_api_dict_camel_case(self):
        req = KHContractCallRequest(**self._valid())
        d = req.to_api_dict()
        assert "contractAddress" in d
        assert "functionName" in d
        assert d["network"] == "base"

    def test_to_api_dict_excludes_none(self):
        req = KHContractCallRequest(**self._valid())
        d = req.to_api_dict()
        assert "calldata" not in d
        assert "value" not in d
        assert "abi" not in d


@pytest.mark.unit
class TestKHCondition:
    def test_all_operators_valid(self):
        for op in KHConditionOperator:
            c = KHCondition(operator=op, value="100")
            assert c.operator == op


@pytest.mark.unit
class TestKHExecutionStatus_:
    def test_success_is_terminal(self):
        s = KHExecutionStatus_(**{
            "executionId": "exec_1",
            "status": KHExecutionStatus.SUCCESS,
        })
        assert s.is_terminal
        assert s.succeeded

    def test_failed_is_terminal(self):
        s = KHExecutionStatus_(**{
            "executionId": "exec_1",
            "status": KHExecutionStatus.FAILED,
        })
        assert s.is_terminal
        assert not s.succeeded

    def test_pending_not_terminal(self):
        s = KHExecutionStatus_(**{
            "executionId": "exec_1",
            "status": KHExecutionStatus.PENDING,
        })
        assert not s.is_terminal

    def test_running_not_terminal(self):
        s = KHExecutionStatus_(**{
            "executionId": "exec_1",
            "status": KHExecutionStatus.RUNNING,
        })
        assert not s.is_terminal

    def test_cancelled_is_terminal(self):
        s = KHExecutionStatus_(**{
            "executionId": "exec_1",
            "status": KHExecutionStatus.CANCELLED,
        })
        assert s.is_terminal


@pytest.mark.unit
class TestKHAuditEntry:
    def test_succeeded_property(self):
        entry = KHAuditEntry(
            execution_id="exec_1",
            status=KHExecutionStatus.SUCCESS,
            tx_hash="0x" + "ab" * 32,
        )
        assert entry.succeeded

    def test_failed_not_succeeded(self):
        entry = KHAuditEntry(
            execution_id="exec_1",
            status=KHExecutionStatus.FAILED,
            error="gas spike",
        )
        assert not entry.succeeded

    def test_to_log_data_excludes_none(self):
        entry = KHAuditEntry(
            execution_id="exec_1",
            status=KHExecutionStatus.SUCCESS,
            tx_hash="0xabc",
        )
        d = entry.to_log_data()
        assert None not in d.values()
        assert "execution_id" in d
        assert "status" in d
        assert "tx_hash" in d

    def test_to_log_data_is_json_serialisable(self):
        import json
        entry = KHAuditEntry(
            execution_id="exec_abc",
            status=KHExecutionStatus.SUCCESS,
            tx_hash="0x" + "ab" * 32,
            block_number=12345678,
            gas_used=150000,
            network="base",
            contract=CONTRACT,
            function_name="execute",
            elapsed_ms=1234,
        )
        json.dumps(entry.to_log_data())