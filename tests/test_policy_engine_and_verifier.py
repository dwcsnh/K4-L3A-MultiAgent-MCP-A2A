from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from student_agent.agents import (
    COORDINATOR,
    POLICY_AGENT,
    VERIFIER,
    PolicySpecialist,
    VerifierAgent,
)
from student_agent.contracts import Contracts
from student_agent.invariants import (
    InvariantViolationError,
    check_consistency_invariants,
    verify_all_invariants,
)
from student_agent.models import (
    CaseContext,
    CaseStatus,
    FinancialResolution,
    PartyType,
    PrimaryIssue,
    RankedCause,
    RefundLine,
    ResponsibleParty,
    RootCauseAnalysis,
)
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def contracts(repo_root: Path) -> Contracts:
    return Contracts(repo_root / "contracts" / "schemas")


def make_policy_rules() -> dict[str, Any]:
    return {
        "canceled_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            "responsible_parties": [{"party_id": None, "party_type": "platform"}],
        },
        "unavailable_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 89.0,
            "responsible_parties": [{"party_id": "seller-0c65eb5a1415", "party_type": "seller"}],
        },
        "late_delivery_seller": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_id": "seller-e58fb7bfd033", "party_type": "seller"}],
        },
        "late_delivery_logistics": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 16.0,
            "responsible_parties": [{"party_id": None, "party_type": "logistics_provider"}],
        },
        "valid_split_payment": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
        "payment_mismatch": {
            "case_status": "action_required",
            "recommended_action": "reconcile_payment",
            "refund_brl": 35.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "duplicate_charge": {
            "case_status": "action_required",
            "recommended_action": "refund_duplicate_charge",
            "refund_brl": 64.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "refund_pending": {
            "case_status": "needs_investigation",
            "recommended_action": "monitor_refund",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "refund_failed": {
            "case_status": "action_required",
            "recommended_action": "retry_refund",
            "refund_brl": 52.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "unsupported_claim": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
    }


def make_mock_gateway(rules: dict[str, Any] | None = None) -> AsyncMock:
    gateway = AsyncMock()
    policy_rules = rules or make_policy_rules()

    async def mock_call(tool_name: str, *, case_id: str, **kwargs: str) -> dict[str, Any]:
        if tool_name == "get_order":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": f"ev_0123456789abcdef0123456789abcdef0123456789_order_{case_id}",
                "result_hash": "sha256:" + "a" * 64,
                "domain": "order",
                "data": {
                    "order_id": kwargs.get("order_id", "order_001"),
                    "order_status": "delivered",
                },
            }
        elif tool_name == "get_order_items":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": f"ev_0123456789abcdef0123456789abcdef0123456789_items_{case_id}",
                "result_hash": "sha256:" + "b" * 64,
                "domain": "item",
                "data": [
                    {
                        "order_id": kwargs.get("order_id", "order_001"),
                        "order_item_id": "item_001",
                        "seller_id": "seller_abc_123",
                        "price": "79.00",
                        "freight_value": "18.00",
                    }
                ],
            }
        elif tool_name == "get_order_payments":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": f"ev_0123456789abcdef0123456789abcdef0123456789_payments_{case_id}",
                "result_hash": "sha256:" + "c" * 64,
                "domain": "payment",
                "data": [
                    {
                        "order_id": kwargs.get("order_id", "order_001"),
                        "payment_sequential": "1",
                        "payment_type": "credit_card",
                        "payment_value": "97.00",
                    }
                ],
            }
        elif tool_name == "get_shipment_summary":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": f"ev_0123456789abcdef0123456789abcdef0123456789_shipment_{case_id}",
                "result_hash": "sha256:" + "d" * 64,
                "domain": "shipment",
                "data": {
                    "order_id": kwargs.get("order_id", "order_001"),
                    "order_status": "delivered",
                    "shipping_limits": [
                        {
                            "order_item_id": "item_001",
                            "seller_id": "seller_abc_123",
                            "shipping_limit_at": "2018-01-06T10:00:00Z",
                        }
                    ],
                },
            }
        elif tool_name == "get_policy":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": f"ev_0123456789abcdef0123456789abcdef0123456789_policy_{case_id}",
                "result_hash": "sha256:" + "e" * 64,
                "domain": "policy",
                "data": {
                    "currency": "BRL",
                    "policy_version": "EC_POLICY_V1",
                    "rules": policy_rules,
                },
            }
        raise ValueError(f"Unknown tool: {tool_name}")

    gateway.call.side_effect = mock_call
    return gateway


@pytest.mark.anyio
@pytest.mark.parametrize(
    "issue_topic, expected_status, expected_refund, expected_party_type",
    [
        ("canceled_order_paid", "action_required", 79.0, "platform"),
        ("unavailable_order_paid", "action_required", 89.0, "seller"),
        ("late_delivery_seller", "action_required", 18.0, "seller"),
        ("late_delivery_logistics", "action_required", 16.0, "logistics_provider"),
        ("valid_split_payment", "no_action", 0.0, "customer"),
        ("payment_mismatch", "action_required", 35.0, "payment_provider"),
        ("duplicate_charge", "action_required", 64.0, "payment_provider"),
        ("refund_pending", "needs_investigation", 0.0, "payment_provider"),
        ("refund_failed", "action_required", 52.0, "payment_provider"),
        ("unsupported_claim", "no_action", 0.0, "customer"),
    ],
)
async def test_policy_specialist_decisions_for_all_issue_types(
    contracts: Contracts,
    tmp_path: Path,
    issue_topic: str,
    expected_status: str,
    expected_refund: float,
    expected_party_type: str,
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)
    gateway = make_mock_gateway()

    context = CaseContext(
        case_id=f"L3A_TEST_{issue_topic.upper()}",
        opened_at="2018-01-01T09:00:00Z",
        customer_request={
            "claimed_order_id": "order_test_123",
            "claims": [
                {"claim_id": "c-01", "topic": issue_topic},
                {"claim_id": "c-02", "topic": "requested_full_refund"},
            ],
        },
        policy_version="EC_POLICY_V1",
        claimed_order_id="order_test_123",
        claims=[
            {"claim_id": "c-01", "topic": issue_topic},
            {"claim_id": "c-02", "topic": "requested_full_refund"},
        ],
    )
    context.entities.seller_ids.append("seller_abc_123")
    context.add_evidence("ev_0123456789abcdef0123456789abcdef0123456789_test")

    specialist = PolicySpecialist()
    await specialist.run(context, gateway, writer)

    # 1. Primary Issue correctly classified
    assert context.primary_issue == issue_topic
    assert context.case_status == expected_status

    # 2. Financial Resolution
    assert context.financial_resolution.currency == "BRL"
    assert context.financial_resolution.recommended_refund_brl == expected_refund
    if expected_refund > 0:
        assert len(context.financial_resolution.refund_lines) == 1
        assert context.financial_resolution.refund_lines[0].amount_brl == expected_refund
    else:
        assert len(context.financial_resolution.refund_lines) == 0

    # 3. Responsible Parties
    responsible = context.root_cause_analysis.responsible_parties
    assert len(responsible) >= 1
    assert responsible[0].party_type == expected_party_type
    if expected_party_type == "seller":
        assert responsible[0].party_id == "seller_abc_123"

    # 4. Resolution Actions: 1-8 unique actions
    assert 1 <= len(context.resolution_actions) <= 8
    assert len(context.resolution_actions) == len(set(context.resolution_actions))

    # 5. Calibrated Confidence
    assert 0.0 < context.confidence < 1.0

    # 6. Trace emission of policy_decided
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    policy_events = [e for e in events if e["event_type"] == "policy_decided"]
    assert len(policy_events) == 1
    assert policy_events[0]["actor"] == POLICY_AGENT
    assert policy_events[0]["decision_code"] == f"POLICY_DECISION_{issue_topic.upper()}"
    assert policy_events[0]["attributes"]["recommended_refund_brl"] == expected_refund


def test_verifier_blocks_carrier_fault_for_seller_delay() -> None:
    # Verifier must reject logistics_provider being held responsible for seller delay
    invalid_output = {
        "assessment": {
            "primary_issue": "late_delivery_seller",
            "case_status": "action_required",
            "confidence": 0.92,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 18.0,
            "refund_lines": [
                {"reason_code": "late_delivery_seller", "amount_brl": 18.0, "entity_id": None}
            ],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "SELLER_DISPATCH_SLA_BREACH", "rank": 1}],
            "responsible_parties": [
                {"party_type": "seller", "party_id": "seller_123"},
                {"party_type": "logistics_provider", "party_id": None},
            ],
        },
        "resolution_actions": ["refund_freight"],
    }
    with pytest.raises(InvariantViolationError, match="logistics_provider cannot be responsible"):
        check_consistency_invariants(invalid_output)


def test_verifier_blocks_seller_fault_for_logistics_delay() -> None:
    # Verifier must reject seller being held responsible for logistics delay
    invalid_output = {
        "assessment": {
            "primary_issue": "late_delivery_logistics",
            "case_status": "action_required",
            "confidence": 0.92,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 16.0,
            "refund_lines": [
                {"reason_code": "late_delivery_logistics", "amount_brl": 16.0, "entity_id": None}
            ],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "CARRIER_TRANSIT_DELAY", "rank": 1}],
            "responsible_parties": [
                {"party_type": "logistics_provider", "party_id": None},
                {"party_type": "seller", "party_id": "seller_123"},
            ],
        },
        "resolution_actions": ["refund_freight"],
    }
    with pytest.raises(InvariantViolationError, match="seller cannot be responsible"):
        check_consistency_invariants(invalid_output)


def test_verifier_blocks_unsupported_claim_with_non_zero_refund() -> None:
    invalid_output = {
        "assessment": {
            "primary_issue": "unsupported_claim",
            "case_status": "no_action",
            "confidence": 0.90,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 25.0,
            "refund_lines": [
                {"reason_code": "unsupported_claim", "amount_brl": 25.0, "entity_id": None}
            ],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "CLAIM_WITHOUT_FACTUAL_BASIS", "rank": 1}],
            "responsible_parties": [{"party_type": "customer", "party_id": None}],
        },
        "resolution_actions": ["document_no_action"],
    }
    with pytest.raises(InvariantViolationError, match="Status vs Refund invariant violated"):
        check_consistency_invariants(invalid_output)


def test_verifier_blocks_hardcoded_confidence_one() -> None:
    invalid_output = {
        "assessment": {
            "primary_issue": "late_delivery_seller",
            "case_status": "action_required",
            "confidence": 1.0,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 18.0,
            "refund_lines": [
                {"reason_code": "late_delivery_seller", "amount_brl": 18.0, "entity_id": None}
            ],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "SELLER_DISPATCH_SLA_BREACH", "rank": 1}],
            "responsible_parties": [{"party_type": "seller", "party_id": "seller_123"}],
        },
        "resolution_actions": ["refund_freight"],
    }
    with pytest.raises(InvariantViolationError, match="confidence must not be hardcoded to 1.0"):
        check_consistency_invariants(invalid_output)


def test_verifier_agent_intercepts_and_corrects_inconsistencies(
    contracts: Contracts, tmp_path: Path
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)

    # Context created with potential inconsistencies
    context = CaseContext(
        case_id="L3A_TEST_VERIFY",
        opened_at="2018-01-01T09:00:00Z",
        customer_request={"claimed_order_id": "order_verify"},
        policy_version="EC_POLICY_V1",
        claimed_order_id="order_verify",
        primary_issue=PrimaryIssue.LATE_DELIVERY_SELLER,
        case_status=CaseStatus.ACTION_REQUIRED,
        confidence=1.0,  # Overconfident
        resolution_actions=[
            "refund_freight",
            "refund_freight",
            "notify_customer",
        ],  # Duplicate actions
        financial_resolution=FinancialResolution(
            currency="BRL",
            recommended_refund_brl=18.0,
            refund_lines=[
                RefundLine(
                    reason_code="late_delivery_seller",
                    amount_brl=18.0,
                    entity_id="order_verify",
                )
            ],
        ),
        root_cause_analysis=RootCauseAnalysis(
            ranked_causes=[RankedCause(cause_code="SELLER_DISPATCH_SLA_BREACH", rank=1)],
            responsible_parties=[
                # Contradictory carrier party
                ResponsibleParty(party_type=PartyType.LOGISTICS_PROVIDER, party_id=None),
            ],
        ),
    )
    context.entities.seller_ids.append("seller_corr_01")
    context.add_evidence("ev_0123456789abcdef0123456789abcdef0123456789_verify")

    verifier = VerifierAgent()
    output = verifier.run(context, contracts, writer)

    # Verify that inconsistencies were corrected:
    # 1. Duplicate actions removed
    assert len(output["resolution_actions"]) == len(set(output["resolution_actions"]))
    # 2. Confidence corrected below 1.0
    assert output["assessment"]["confidence"] < 1.0
    # 3. Carrier party removed and seller party ensured
    parties = output["root_cause_analysis"]["responsible_parties"]
    party_types = [p["party_type"] for p in parties]
    assert "logistics_provider" not in party_types
    assert "seller" in party_types
    # 4. Invariants verified without raising
    verify_all_invariants(output, context)
    # 5. Verification completed event emitted
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    ver_events = [e for e in events if e["event_type"] == "verification_completed"]
    assert len(ver_events) == 1
    assert ver_events[0]["actor"] == VERIFIER
    assert ver_events[0]["decision_code"] == "VERIFIED_CONSISTENT"


@pytest.mark.anyio
async def test_full_trace_seven_lifecycle_events_sequential(
    contracts: Contracts, tmp_path: Path
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)
    gateway = make_mock_gateway()

    case = {
        "case_id": "L3A_CASE_003",
        "opened_at": "2018-03-03T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": "71303d7e93b399f5bcd537d124c0bcfa",
            "claims": [
                {"claim_id": "claim-003-a", "topic": "late_delivery_seller"},
                {"claim_id": "claim-003-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }

    # Step 1: case_received
    writer.emit(case_id=case["case_id"], event_type="case_received", actor=COORDINATOR)

    # Steps 2-6: pipeline run
    output = await solve_case(case, gateway, writer)
    contracts.validate_output(output, f"outputs/{case['case_id']}.json")
    verify_all_invariants(output)

    # Step 7: case_finalized
    writer.emit(case_id=case["case_id"], event_type="case_finalized", actor=COORDINATOR)

    # Verify trace events
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    event_types = [e["event_type"] for e in events]

    assert event_types[0] == "case_received"
    assert event_types[-1] == "case_finalized"

    # Verify ordered subsequence:
    expected_order = [
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
        "case_finalized",
    ]
    current_idx = 0
    for exp in expected_order:
        assert exp in event_types[current_idx:], f"Expected event {exp} after index {current_idx}"
        current_idx = event_types.index(exp, current_idx) + 1
