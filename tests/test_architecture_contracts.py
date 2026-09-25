from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from student_agent.agents import (
    AGENT_TOOL_PERMISSIONS,
    COORDINATOR,
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    VERIFIER,
    AgentPermissionError,
    OrderSpecialist,
    PaymentSpecialist,
    PolicySpecialist,
    ShipmentSpecialist,
)
from student_agent.contracts import ContractError, Contracts
from student_agent.invariants import (
    InvariantViolationError,
    check_consistency_invariants,
    check_evidence_invariants,
    verify_all_invariants,
)
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def contracts(repo_root: Path) -> Contracts:
    return Contracts(repo_root / "contracts" / "schemas")


def test_all_contract_schemas_loaded(contracts: Contracts) -> None:
    expected_schemas = [
        "l3a-output-v2.schema.json",
        "mcp-evidence-response-v1.schema.json",
        "submission-manifest-v2.schema.json",
        "trace-event-v1.schema.json",
    ]
    for schema_name in expected_schemas:
        assert schema_name in contracts._schemas


def test_trace_event_schema_accepts_all_required_lifecycle_events(
    contracts: Contracts, tmp_path: Path
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)
    case_id = "L3A_TEST_001"

    lifecycle_events = [
        ("case_received", COORDINATOR, None, None),
        ("task_assigned", COORDINATOR, ORDER_AGENT, "DELEGATE_INVESTIGATION"),
        ("tool_result_consumed", ORDER_AGENT, None, None),
        ("handoff", ORDER_AGENT, PAYMENT_AGENT, "ORDER_DONE"),
        ("policy_decided", POLICY_AGENT, None, "POLICY_DECISION_CANCELED_ORDER_PAID"),
        ("verification_completed", VERIFIER, None, "VERIFIED_CONSISTENT"),
        ("case_finalized", COORDINATOR, None, None),
    ]

    for event_type, actor, target, decision in lifecycle_events:
        extra_kwargs: dict[str, Any] = {}
        if event_type == "tool_result_consumed":
            extra_kwargs["tool_name"] = "get_order"
            extra_kwargs["evidence_refs"] = ["ev_0123456789abcdef0123456789abcdef0123456789"]

        event = writer.emit(
            case_id=case_id,
            event_type=event_type,
            actor=actor,
            target=target,
            decision_code=decision,
            **extra_kwargs,
        )
        assert event["event_type"] == event_type
        assert event["actor"] == actor


def test_trace_event_schema_rejects_invalid_event_type(
    contracts: Contracts, tmp_path: Path
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)
    with pytest.raises(ContractError):
        writer.emit(
            case_id="L3A_TEST_001",
            event_type="invalid_event_type",
            actor=COORDINATOR,
        )


def test_mcp_evidence_response_schema_validation(contracts: Contracts) -> None:
    valid_evidence = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_abc123def456ghi789jkl0123456789",
        "result_hash": "sha256:" + "a" * 64,
        "domain": "order",
        "data": {"order_id": "test_order_123", "order_status": "canceled"},
    }
    contracts.validate_evidence(valid_evidence)

    # Missing required field result_hash
    invalid_evidence = dict(valid_evidence)
    del invalid_evidence["result_hash"]
    with pytest.raises(ContractError):
        contracts.validate_evidence(invalid_evidence)


def test_agent_tool_permissions_least_privilege() -> None:
    # Coordinator & Verifier have zero tool permissions
    assert len(AGENT_TOOL_PERMISSIONS[COORDINATOR]) == 0
    assert len(AGENT_TOOL_PERMISSIONS[VERIFIER]) == 0

    order_specialist = OrderSpecialist()
    payment_specialist = PaymentSpecialist()
    shipment_specialist = ShipmentSpecialist()
    policy_specialist = PolicySpecialist()

    # Allowed tools
    order_specialist.check_tool_permission("get_order")
    order_specialist.check_tool_permission("get_order_items")
    payment_specialist.check_tool_permission("get_order_payments")
    shipment_specialist.check_tool_permission("get_shipment_summary")
    policy_specialist.check_tool_permission("get_policy")

    # Forbidden cross-calling raises AgentPermissionError
    with pytest.raises(AgentPermissionError):
        order_specialist.check_tool_permission("get_order_payments")

    with pytest.raises(AgentPermissionError):
        payment_specialist.check_tool_permission("get_policy")

    with pytest.raises(AgentPermissionError):
        shipment_specialist.check_tool_permission("get_order")

    with pytest.raises(AgentPermissionError):
        policy_specialist.check_tool_permission("get_shipment_summary")


def test_invariant_status_vs_refund() -> None:
    # Unsupported claim with refund > 0 must fail
    invalid_output = {
        "assessment": {
            "primary_issue": "unsupported_claim",
            "case_status": "no_action",
            "confidence": 0.9,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 50.0,
            "refund_lines": [{"reason_code": "full_refund", "amount_brl": 50.0, "entity_id": None}],
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "resolution_actions": ["close_case"],
    }
    with pytest.raises(InvariantViolationError, match="Status vs Refund invariant violated"):
        check_consistency_invariants(invalid_output)


def test_invariant_refund_vs_action() -> None:
    # Refund > 0 but case_status is needs_investigation (must be action_required)
    invalid_output = {
        "assessment": {
            "primary_issue": "canceled_order_paid",
            "case_status": "needs_investigation",
            "confidence": 0.9,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 100.0,
            "refund_lines": [
                {"reason_code": "canceled_order", "amount_brl": 100.0, "entity_id": None}
            ],
        },
        "root_cause_analysis": {
            "ranked_causes": [],
            "responsible_parties": [{"party_type": "platform", "party_id": None}],
        },
        "resolution_actions": ["issue_refund"],
    }
    with pytest.raises(InvariantViolationError, match="Refund vs Action invariant violated"):
        check_consistency_invariants(invalid_output)


def test_invariant_refund_total_matching() -> None:
    # Sum of refund lines does not match recommended_refund_brl
    invalid_output = {
        "assessment": {
            "primary_issue": "canceled_order_paid",
            "case_status": "action_required",
            "confidence": 0.9,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 100.0,
            "refund_lines": [
                {"reason_code": "canceled_order", "amount_brl": 80.0, "entity_id": None}
            ],
        },
        "root_cause_analysis": {
            "ranked_causes": [],
            "responsible_parties": [{"party_type": "platform", "party_id": None}],
        },
        "resolution_actions": ["issue_refund"],
    }
    with pytest.raises(InvariantViolationError, match="Refund Total Matching invariant violated"):
        check_consistency_invariants(invalid_output)


def test_invariant_party_vs_issue() -> None:
    # late_delivery_seller without seller responsible party must fail
    invalid_output = {
        "assessment": {
            "primary_issue": "late_delivery_seller",
            "case_status": "action_required",
            "confidence": 0.9,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "DELAY", "rank": 1}],
            "responsible_parties": [{"party_type": "customer", "party_id": "cust1"}],
        },
        "resolution_actions": ["warn_customer"],
    }
    with pytest.raises(InvariantViolationError, match="Party vs Issue invariant violated"):
        check_consistency_invariants(invalid_output)


def test_invariant_no_duplicate_actions() -> None:
    invalid_output = {
        "assessment": {
            "primary_issue": "unsupported_claim",
            "case_status": "no_action",
            "confidence": 0.9,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "resolution_actions": ["close_case", "close_case"],
    }
    with pytest.raises(InvariantViolationError, match="No Duplicate Actions invariant violated"):
        check_consistency_invariants(invalid_output)


def test_invariant_evidence_ref_format() -> None:
    output_with_bad_ref = {
        "evidence_refs": ["invalid_ref_without_prefix"],
        "claim_assessments": [],
    }
    with pytest.raises(InvariantViolationError, match="Invalid evidence ref format"):
        check_evidence_invariants(output_with_bad_ref)


@pytest.mark.anyio
async def test_solve_case_orchestration_and_schema_compliance(
    contracts: Contracts, tmp_path: Path
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)

    sample_case = {
        "case_id": "L3A_TEST_001",
        "opened_at": "2018-01-01T09:00:00Z",
        "customer_request": {
            "claimed_order_id": "order_123",
            "claims": [{"claim_id": "claim_01", "topic": "canceled_order_paid"}],
        },
        "policy_version": "EC_POLICY_V1",
    }

    mock_gateway = AsyncMock()

    output = await solve_case(sample_case, mock_gateway, writer)

    # 1. Output must satisfy public schema
    contracts.validate_output(output, "test output")
    assert output["case_id"] == "L3A_TEST_001"
    assert output["schema_version"] == "day09-l3a-output-v2"

    # 2. Output must satisfy all verification invariants
    verify_all_invariants(output)

    # 3. Trace must contain observable DAG events in order
    trace_events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    event_types = [e["event_type"] for e in trace_events]

    assert "task_assigned" in event_types
    assert "handoff" in event_types
    assert "policy_decided" in event_types
    assert "verification_completed" in event_types
