from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from student_agent.agents import (
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
    OrderSpecialist,
    PaymentSpecialist,
    PolicySpecialist,
    ShipmentSpecialist,
)
from student_agent.contracts import Contracts
from student_agent.invariants import verify_all_invariants
from student_agent.models import CaseContext
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def contracts(repo_root: Path) -> Contracts:
    return Contracts(repo_root / "contracts" / "schemas")


def make_mock_gateway(
    order_data: dict[str, Any] | None = None,
    items_data: list[dict[str, Any]] | None = None,
    payments_data: list[dict[str, Any]] | None = None,
    shipment_data: dict[str, Any] | None = None,
    policy_data: dict[str, Any] | None = None,
) -> AsyncMock:
    gateway = AsyncMock()

    async def mock_call(tool_name: str, *, case_id: str, **kwargs: str) -> dict[str, Any]:
        # Enforce case_id presence
        assert case_id, "case_id must be provided to gateway.call"

        if tool_name == "get_order":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_0123456789abcdef0123456789abcdef0123456789_order",
                "result_hash": "sha256:" + "a" * 64,
                "domain": "order",
                "data": order_data
                or {
                    "order_id": kwargs.get("order_id", "order_001"),
                    "customer_id": "customer_001",
                    "order_status": "delivered",
                },
            }
        elif tool_name == "get_order_items":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_0123456789abcdef0123456789abcdef0123456789_items",
                "result_hash": "sha256:" + "b" * 64,
                "domain": "item",
                "data": items_data
                or [
                    {
                        "order_id": kwargs.get("order_id", "order_001"),
                        "order_item_id": "item_001",
                        "seller_id": "seller_001",
                        "price": "50.00",
                        "freight_value": "10.00",
                    }
                ],
            }
        elif tool_name == "get_order_payments":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_0123456789abcdef0123456789abcdef0123456789_payments",
                "result_hash": "sha256:" + "c" * 64,
                "domain": "payment",
                "data": payments_data
                or [
                    {
                        "order_id": kwargs.get("order_id", "order_001"),
                        "payment_sequential": "1",
                        "payment_type": "credit_card",
                        "payment_value": "60.00",
                    }
                ],
            }
        elif tool_name == "get_shipment_summary":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_0123456789abcdef0123456789abcdef0123456789_shipment",
                "result_hash": "sha256:" + "d" * 64,
                "domain": "shipment",
                "data": shipment_data
                or {
                    "order_id": kwargs.get("order_id", "order_001"),
                    "order_status": "delivered",
                    "delivered_customer_at": "2018-01-10T10:00:00Z",
                    "estimated_delivery_at": "2018-01-15T10:00:00Z",
                    "delivered_carrier_at": "2018-01-05T10:00:00Z",
                    "shipping_limits": [
                        {
                            "order_item_id": "item_001",
                            "seller_id": "seller_001",
                            "shipping_limit_at": "2018-01-06T10:00:00Z",
                        }
                    ],
                },
            }
        elif tool_name == "get_policy":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_0123456789abcdef0123456789abcdef0123456789_policy",
                "result_hash": "sha256:" + "e" * 64,
                "domain": "policy",
                "data": policy_data
                or {
                    "currency": "BRL",
                    "policy_version": "EC_POLICY_V1",
                    "rules": {
                        "canceled_order_paid": {
                            "case_status": "action_required",
                            "recommended_action": "issue_refund",
                            "refund_brl": 60.0,
                            "responsible_parties": [{"party_type": "platform", "party_id": None}],
                        },
                        "late_delivery_seller": {
                            "case_status": "action_required",
                            "recommended_action": "refund_freight",
                            "refund_brl": 10.0,
                            "responsible_parties": [
                                {"party_type": "seller", "party_id": "seller_001"}
                            ],
                        },
                        "valid_split_payment": {
                            "case_status": "no_action",
                            "recommended_action": "document_no_action",
                            "refund_brl": 0.0,
                            "responsible_parties": [{"party_type": "customer", "party_id": None}],
                        },
                        "unsupported_claim": {
                            "case_status": "no_action",
                            "recommended_action": "document_no_action",
                            "refund_brl": 0.0,
                            "responsible_parties": [{"party_type": "customer", "party_id": None}],
                        },
                    },
                },
            }
        raise ValueError(f"Unexpected tool: {tool_name}")

    gateway.call.side_effect = mock_call
    return gateway


@pytest.mark.anyio
async def test_order_specialist_investigation_and_trace(
    contracts: Contracts, tmp_path: Path
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)
    gateway = make_mock_gateway()

    context = CaseContext(
        case_id="L3A_TEST_001",
        opened_at="2018-01-01T09:00:00Z",
        customer_request={"claimed_order_id": "order_001"},
        policy_version="EC_POLICY_V1",
        claimed_order_id="order_001",
    )

    specialist = OrderSpecialist()
    await specialist.run(context, gateway, writer)

    assert "order_001" in context.entities.order_ids
    assert "item_001" in context.entities.item_ids
    assert "seller_001" in context.entities.seller_ids
    assert any("order" in ref for ref in context.evidence_refs)
    assert any("items" in ref for ref in context.evidence_refs)

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    consumed = [e for e in events if e["event_type"] == "tool_result_consumed"]
    assert len(consumed) == 2
    assert consumed[0]["tool_name"] == "get_order"
    assert consumed[0]["actor"] == ORDER_AGENT
    assert consumed[1]["tool_name"] == "get_order_items"

    handoff = [e for e in events if e["event_type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["actor"] == ORDER_AGENT
    assert handoff[0]["target"] == PAYMENT_AGENT


@pytest.mark.anyio
async def test_payment_specialist_investigation_and_trace(
    contracts: Contracts, tmp_path: Path
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)
    gateway = make_mock_gateway()

    context = CaseContext(
        case_id="L3A_TEST_001",
        opened_at="2018-01-01T09:00:00Z",
        customer_request={"claimed_order_id": "order_001"},
        policy_version="EC_POLICY_V1",
        claimed_order_id="order_001",
    )

    specialist = PaymentSpecialist()
    await specialist.run(context, gateway, writer)

    assert len(context.entities.payment_references) > 0
    assert any("payments" in ref for ref in context.evidence_refs)

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    consumed = [e for e in events if e["event_type"] == "tool_result_consumed"]
    assert len(consumed) == 1
    assert consumed[0]["tool_name"] == "get_order_payments"
    assert consumed[0]["actor"] == PAYMENT_AGENT

    handoff = [e for e in events if e["event_type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["actor"] == PAYMENT_AGENT
    assert handoff[0]["target"] == SHIPMENT_AGENT


@pytest.mark.anyio
async def test_shipment_specialist_investigation_and_trace(
    contracts: Contracts, tmp_path: Path
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)
    gateway = make_mock_gateway()

    context = CaseContext(
        case_id="L3A_TEST_001",
        opened_at="2018-01-01T09:00:00Z",
        customer_request={"claimed_order_id": "order_001"},
        policy_version="EC_POLICY_V1",
        claimed_order_id="order_001",
    )

    specialist = ShipmentSpecialist()
    await specialist.run(context, gateway, writer)

    assert any("shipment" in ref for ref in context.evidence_refs)

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    consumed = [e for e in events if e["event_type"] == "tool_result_consumed"]
    assert len(consumed) == 1
    assert consumed[0]["tool_name"] == "get_shipment_summary"
    assert consumed[0]["actor"] == SHIPMENT_AGENT

    handoff = [e for e in events if e["event_type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["actor"] == SHIPMENT_AGENT
    assert handoff[0]["target"] == POLICY_AGENT


@pytest.mark.anyio
async def test_policy_specialist_investigation_and_trace(
    contracts: Contracts, tmp_path: Path
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)
    gateway = make_mock_gateway()

    context = CaseContext(
        case_id="L3A_TEST_001",
        opened_at="2018-01-01T09:00:00Z",
        customer_request={
            "claimed_order_id": "order_001",
            "claims": [{"claim_id": "claim_01", "topic": "valid_split_payment"}],
        },
        policy_version="EC_POLICY_V1",
        claimed_order_id="order_001",
        claims=[{"claim_id": "claim_01", "topic": "valid_split_payment"}],
    )

    specialist = PolicySpecialist()
    await specialist.run(context, gateway, writer)

    assert any("policy" in ref for ref in context.evidence_refs)
    assert context.case_status == "no_action"
    assert context.financial_resolution.recommended_refund_brl == 0.0

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    policy_events = [e for e in events if e["event_type"] == "policy_decided"]
    assert len(policy_events) == 1
    assert policy_events[0]["actor"] == POLICY_AGENT


@pytest.mark.anyio
async def test_full_langgraph_pipeline_execution(contracts: Contracts, tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)

    order_data = {
        "order_id": "order_canceled_123",
        "order_status": "canceled",
    }
    gateway = make_mock_gateway(order_data=order_data)

    case = {
        "case_id": "L3A_CASE_099",
        "opened_at": "2018-01-01T09:00:00Z",
        "customer_request": {
            "claimed_order_id": "order_canceled_123",
            "claims": [
                {"claim_id": "c-01", "topic": "canceled_order_paid"},
                {"claim_id": "c-02", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }

    # Simulate cli emitting case_received first
    writer.emit(case_id="L3A_CASE_099", event_type="case_received", actor="coordinator")

    output = await solve_case(case, gateway, writer)

    # Validate output against contracts and invariants
    contracts.validate_output(output, "test_output")
    verify_all_invariants(output)

    assert output["case_id"] == "L3A_CASE_099"
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["recommended_refund_brl"] > 0
    assert len(output["financial_resolution"]["refund_lines"]) == 1

    # Simulate cli emitting case_finalized
    writer.emit(case_id="L3A_CASE_099", event_type="case_finalized", actor="coordinator")

    # Verify complete 7 lifecycle events order
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    event_types = [e["event_type"] for e in events]

    assert event_types[0] == "case_received"
    assert "task_assigned" in event_types
    assert "tool_result_consumed" in event_types
    assert "handoff" in event_types
    assert "policy_decided" in event_types
    assert "verification_completed" in event_types
    assert event_types[-1] == "case_finalized"
