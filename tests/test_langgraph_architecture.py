from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langgraph.graph import END, START

from student_agent.contracts import Contracts
from student_agent.graph import (
    build_workflow_graph,
    coordinator_finalize_node,
    coordinator_node,
)
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


def make_mock_gateway() -> AsyncMock:
    gateway = AsyncMock()

    async def mock_call(tool_name: str, *, case_id: str, **kwargs: str) -> dict[str, Any]:
        if tool_name == "get_order":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_0123456789abcdef0123456789abcdef0123456789_order",
                "result_hash": "sha256:" + "a" * 64,
                "domain": "order",
                "data": {
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
                "data": [
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
                "data": [
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
                "data": {
                    "order_id": kwargs.get("order_id", "order_001"),
                    "order_status": "delivered",
                    "order_estimated_delivery_date": "2018-03-10 00:00:00",
                    "order_delivered_customer_date": "2018-03-08 14:00:00",
                    "shipping_limits": [
                        {"order_item_id": "item_001", "shipping_limit_date": "2018-03-05 00:00:00"}
                    ],
                },
            }
        elif tool_name == "get_policy":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_0123456789abcdef0123456789abcdef0123456789_policy",
                "result_hash": "sha256:" + "e" * 64,
                "domain": "policy",
                "data": {
                    "policy_version": kwargs.get("policy_version", "EC_POLICY_V1"),
                    "rules": {
                        "unsupported_claim": {
                            "case_status": "no_action",
                            "recommended_action": "notify_customer_claim_rejected",
                            "refund_brl": 0.0,
                            "responsible_parties": [],
                        }
                    },
                },
            }
        return None

    gateway.call.side_effect = mock_call
    return gateway


def test_graph_contains_all_seven_nodes() -> None:
    graph = build_workflow_graph()
    node_names = set(graph.nodes.keys())
    expected_nodes = {
        "coordinator",
        "order_specialist",
        "payment_specialist",
        "shipment_specialist",
        "policy_specialist",
        "verifier",
        "coordinator_finalize",
    }
    assert expected_nodes.issubset(node_names), f"Missing nodes: {expected_nodes - node_names}"
    assert len(expected_nodes) == 7


def test_graph_edges_topology_and_verifier_to_coordinator_finalize() -> None:
    graph = build_workflow_graph()
    edges = set(graph.edges)

    expected_edges = {
        (START, "coordinator"),
        ("coordinator", "order_specialist"),
        ("order_specialist", "payment_specialist"),
        ("payment_specialist", "shipment_specialist"),
        ("shipment_specialist", "policy_specialist"),
        ("policy_specialist", "verifier"),
        ("verifier", "coordinator_finalize"),
        ("coordinator_finalize", END),
    }

    assert expected_edges.issubset(edges), f"Missing edges: {expected_edges - edges}"

    # Explicitly check verifier does NOT connect to END
    assert ("verifier", END) not in edges, "verifier should connect to coordinator_finalize, not END"
    # Explicitly check coordinator_finalize connects to END
    assert ("coordinator_finalize", END) in edges


@pytest.mark.anyio
async def test_coordinator_triage_entity_extraction(contracts: Contracts, tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)

    state = {
        "case": {
            "case_id": "L3A_TRIAGE_001",
            "opened_at": "2018-03-03T09:00:00-03:00",
            "customer_request": {
                "message": "Hello, my order 1234567890abcdef1234567890abcdef has customer_9999 issues.",
                "claims": [{"claim_id": "claim_01", "topic": "late_delivery_seller"}],
            },
            "policy_version": "EC_POLICY_V1",
        },
        "trace": writer,
    }

    result = await coordinator_node(state)
    context: CaseContext = result["context"]

    # Check extracted entities
    assert "1234567890abcdef1234567890abcdef" in context.entities.order_ids
    assert context.customer_request.get("customer_id") == "customer_9999"
    assert context.claimed_order_id == "1234567890abcdef1234567890abcdef"

    # Check trace events
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    event_types = [e["event_type"] for e in events]
    assert "case_received" in event_types
    assert "task_assigned" in event_types

    task_event = next(e for e in events if e["event_type"] == "task_assigned")
    assert task_event["decision_code"] == "DELEGATE_INVESTIGATION"
    assert task_event["attributes"]["claimed_order_id"] == "1234567890abcdef1234567890abcdef"


@pytest.mark.anyio
async def test_full_workflow_produces_all_seven_lifecycle_events_in_order(
    contracts: Contracts, tmp_path: Path
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)
    gateway = make_mock_gateway()

    case = {
        "case_id": "L3A_LIFECYCLE_007",
        "opened_at": "2018-03-03T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": "order_001",
            "claims": [{"claim_id": "claim_01", "topic": "late_delivery_seller"}],
        },
        "policy_version": "EC_POLICY_V1",
    }

    # Execute workflow directly without external manual emit of case_received or case_finalized
    output = await solve_case(case, gateway, writer)

    contracts.validate_output(output, f"outputs/{case['case_id']}.json")
    verify_all_invariants(output)

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    event_types = [e["event_type"] for e in events]

    # 1. First event is case_received
    assert event_types[0] == "case_received", f"First event was {event_types[0]}"

    # 2. Last event is case_finalized
    assert event_types[-1] == "case_finalized", f"Last event was {event_types[-1]}"

    # 3. All 7 lifecycle events must appear in exact chronological sequence
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
        assert exp in event_types[current_idx:], f"Expected event {exp} at or after index {current_idx}"
        current_idx = event_types.index(exp, current_idx) + 1


@pytest.mark.anyio
async def test_coordinator_finalize_node_execution(contracts: Contracts, tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)

    state = {
        "case": {"case_id": "L3A_FIN_001"},
        "output": {"case_id": "L3A_FIN_001", "assessment": {"primary_issue": "unsupported_claim"}},
        "trace": writer,
    }

    result = await coordinator_finalize_node(state)
    assert result["output"] == state["output"]

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["event_type"] == "case_finalized"
    assert events[0]["actor"] == "coordinator"
    assert events[0]["decision_code"] == "CASE_RESOLVED"
