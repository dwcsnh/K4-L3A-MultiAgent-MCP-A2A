from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from student_agent.agents import (
    COORDINATOR,
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
    VERIFIER,
    CoordinatorAgent,
    OrderSpecialist,
    PaymentSpecialist,
    PolicySpecialist,
    ShipmentSpecialist,
    VerifierAgent,
)
from student_agent.contracts import Contracts
from student_agent.invariants import verify_all_invariants
from student_agent.models import CaseContext
from student_agent.prompts import (
    COORDINATOR_SYSTEM_PROMPT,
    ORDER_SPECIALIST_SYSTEM_PROMPT,
    PAYMENT_SPECIALIST_SYSTEM_PROMPT,
    POLICY_SPECIALIST_SYSTEM_PROMPT,
    SHIPMENT_SPECIALIST_SYSTEM_PROMPT,
    SYSTEM_PROMPTS,
    VERIFIER_SYSTEM_PROMPT,
)
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
                    "shipping_limits": [{"order_item_id": "item_001", "shipping_limit_date": "2018-03-05 00:00:00"}],
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


def test_prompts_module_defines_all_constants() -> None:
    assert isinstance(COORDINATOR_SYSTEM_PROMPT, str) and len(COORDINATOR_SYSTEM_PROMPT) > 50
    assert isinstance(ORDER_SPECIALIST_SYSTEM_PROMPT, str) and len(ORDER_SPECIALIST_SYSTEM_PROMPT) > 50
    assert isinstance(PAYMENT_SPECIALIST_SYSTEM_PROMPT, str) and len(PAYMENT_SPECIALIST_SYSTEM_PROMPT) > 50
    assert isinstance(SHIPMENT_SPECIALIST_SYSTEM_PROMPT, str) and len(SHIPMENT_SPECIALIST_SYSTEM_PROMPT) > 50
    assert isinstance(POLICY_SPECIALIST_SYSTEM_PROMPT, str) and len(POLICY_SPECIALIST_SYSTEM_PROMPT) > 50
    assert isinstance(VERIFIER_SYSTEM_PROMPT, str) and len(VERIFIER_SYSTEM_PROMPT) > 50

    assert len(SYSTEM_PROMPTS) == 6
    assert SYSTEM_PROMPTS[COORDINATOR] == COORDINATOR_SYSTEM_PROMPT
    assert SYSTEM_PROMPTS[ORDER_AGENT] == ORDER_SPECIALIST_SYSTEM_PROMPT
    assert SYSTEM_PROMPTS[PAYMENT_AGENT] == PAYMENT_SPECIALIST_SYSTEM_PROMPT
    assert SYSTEM_PROMPTS[SHIPMENT_AGENT] == SHIPMENT_SPECIALIST_SYSTEM_PROMPT
    assert SYSTEM_PROMPTS[POLICY_AGENT] == POLICY_SPECIALIST_SYSTEM_PROMPT
    assert SYSTEM_PROMPTS[VERIFIER] == VERIFIER_SYSTEM_PROMPT


def test_all_six_agents_have_correct_system_prompts() -> None:
    coordinator = CoordinatorAgent()
    assert coordinator.system_prompt == COORDINATOR_SYSTEM_PROMPT
    assert coordinator.actor == COORDINATOR

    order_sp = OrderSpecialist()
    assert order_sp.system_prompt == ORDER_SPECIALIST_SYSTEM_PROMPT
    assert order_sp.actor == ORDER_AGENT

    payment_sp = PaymentSpecialist()
    assert payment_sp.system_prompt == PAYMENT_SPECIALIST_SYSTEM_PROMPT
    assert payment_sp.actor == PAYMENT_AGENT

    shipment_sp = ShipmentSpecialist()
    assert shipment_sp.system_prompt == SHIPMENT_SPECIALIST_SYSTEM_PROMPT
    assert shipment_sp.actor == SHIPMENT_AGENT

    policy_sp = PolicySpecialist()
    assert policy_sp.system_prompt == POLICY_SPECIALIST_SYSTEM_PROMPT
    assert policy_sp.actor == POLICY_AGENT

    verifier = VerifierAgent()
    assert verifier.system_prompt == VERIFIER_SYSTEM_PROMPT
    assert verifier.actor == VERIFIER


def test_system_prompts_required_sections_and_invariants() -> None:
    prompts = {
        COORDINATOR: (COORDINATOR_SYSTEM_PROMPT, "Permissions: NO direct MCP tool access."),
        ORDER_AGENT: (ORDER_SPECIALIST_SYSTEM_PROMPT, "Allowed Tools: get_order, get_order_items, get_sellers, get_product_context"),
        PAYMENT_AGENT: (PAYMENT_SPECIALIST_SYSTEM_PROMPT, "Allowed Tools: get_order_payments, get_payment_timeline, get_refund_timeline, get_payment"),
        SHIPMENT_AGENT: (SHIPMENT_SPECIALIST_SYSTEM_PROMPT, "Allowed Tools: get_shipment_summary, get_shipment"),
        POLICY_AGENT: (POLICY_SPECIALIST_SYSTEM_PROMPT, "Allowed Tools: get_policy"),
        VERIFIER: (VERIFIER_SYSTEM_PROMPT, "Permissions: NO direct MCP tool access."),
    }

    for actor, (prompt_text, tools_expected) in prompts.items():
        assert "Role:" in prompt_text, f"Missing 'Role:' in prompt for {actor}"
        assert f"Actor ID: {actor}" in prompt_text, f"Missing 'Actor ID: {actor}' in prompt"
        assert tools_expected in prompt_text, f"Missing tool permissions line in prompt for {actor}"
        assert "Mission:" in prompt_text, f"Missing 'Mission:' in prompt for {actor}"
        assert "responsibilities:" in prompt_text.lower(), f"Missing responsibilities in prompt for {actor}"


def test_policy_prompt_contains_all_eleven_primary_issues() -> None:
    expected_issues = [
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "duplicate_charge",
        "payment_mismatch",
        "valid_split_payment",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
        "insufficient_evidence",
    ]
    for issue in expected_issues:
        assert issue in POLICY_SPECIALIST_SYSTEM_PROMPT, f"Policy prompt missing issue category: {issue}"


def test_verifier_prompt_contains_all_invariants() -> None:
    assert "l3a-output-v2.schema.json" in VERIFIER_SYSTEM_PROMPT
    assert "^ev_[A-Za-z0-9_-]{20,96}$" in VERIFIER_SYSTEM_PROMPT
    assert "Consistency Invariants:" in VERIFIER_SYSTEM_PROMPT
    assert "Status vs Refund:" in VERIFIER_SYSTEM_PROMPT
    assert "Refund vs Action:" in VERIFIER_SYSTEM_PROMPT
    assert "Sum Matching:" in VERIFIER_SYSTEM_PROMPT
    assert "Party Alignment:" in VERIFIER_SYSTEM_PROMPT
    assert "Deduplication:" in VERIFIER_SYSTEM_PROMPT


@pytest.mark.anyio
async def test_trace_log_does_not_leak_system_prompts(contracts: Contracts, tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)
    gateway = make_mock_gateway()

    case = {
        "case_id": "L3A_PROMPT_TRACE_001",
        "opened_at": "2018-03-03T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": "order_001",
            "claims": [
                {"claim_id": "claim-001", "topic": "late_delivery_seller"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }

    writer.emit(case_id=case["case_id"], event_type="case_received", actor=COORDINATOR)
    output = await solve_case(case, gateway, writer)
    contracts.validate_output(output, f"outputs/{case['case_id']}.json")
    verify_all_invariants(output)
    writer.emit(case_id=case["case_id"], event_type="case_finalized", actor=COORDINATOR)

    trace_content = trace_path.read_text(encoding="utf-8")

    # Ensure no system prompt body is leaked in the trace log
    distinctive_phrases = [
        "E-Commerce Dispute Coordinator",
        "Permissions: NO direct MCP tool access",
        "Order & Item Investigation Specialist",
        "Financial & Transaction Reconciliation Specialist",
        "Logistics & Delivery Fulfillment Specialist",
        "Platform Policy & Dispute Adjudication Specialist",
        "Independent Audit, Verification & Invariant Guard Agent",
        "l3a-output-v2.schema.json",
        "Never forge or alter evidence_ref tokens",
        "Customer claims are NOT ground truth",
    ]
    for phrase in distinctive_phrases:
        assert phrase not in trace_content, f"Leaked prompt text found in trace.jsonl: '{phrase}'"


@pytest.mark.anyio
async def test_policy_specialist_call_gpt_4o_mini_uses_system_prompt() -> None:
    specialist = PolicySpecialist()
    context = CaseContext(
        case_id="L3A_TEST_LLM_001",
        opened_at="2018-03-03T09:00:00-03:00",
        customer_request={"message": "I never got my refund"},
        policy_version="EC_POLICY_V1",
        claimed_order_id="order_001",
        claims=[],
    )

    fake_response = MagicMock()
    fake_response.content = json.dumps({"primary_issue": "refund_pending"})

    mock_chat = MagicMock()
    mock_chat.ainvoke = AsyncMock(return_value=fake_response)

    with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key", "OPENAI_MODEL": "gpt-4o-mini"}):
        with patch("langchain_openai.ChatOpenAI", return_value=mock_chat):
            res = await specialist._call_gpt_4o_mini(context)

            assert res == {"primary_issue": "refund_pending"}
            mock_chat.ainvoke.assert_awaited_once()
            messages = mock_chat.ainvoke.await_args[0][0]
            assert len(messages) == 2
            system_msg = messages[0]
            assert system_msg.content == specialist.system_prompt
            assert system_msg.content == POLICY_SPECIALIST_SYSTEM_PROMPT
