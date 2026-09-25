from __future__ import annotations

import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import (
    CoordinatorAgent,
    OrderSpecialist,
    PaymentSpecialist,
    PolicySpecialist,
    ShipmentSpecialist,
    VerifierAgent,
)
from .contracts import Contracts
from .mcp_gateway import EvidenceGateway
from .models import CaseContext
from .trace import TraceWriter


class AgentWorkflowState(TypedDict, total=False):
    case: dict[str, Any]
    context: CaseContext
    gateway: EvidenceGateway
    trace: TraceWriter
    contracts: Contracts
    output: dict[str, Any]


async def coordinator_node(state: AgentWorkflowState) -> dict[str, Any]:
    case = state["case"]
    trace = state["trace"]
    case_id = case["case_id"]
    customer_request = case.get("customer_request", {})
    claimed_order_id = customer_request.get("claimed_order_id")
    customer_id = customer_request.get("customer_id")
    claims = customer_request.get("claims", [])
    policy_version = case.get("policy_version", "EC_POLICY_V1")
    message = customer_request.get("message", "")

    # Triage and entity extraction
    extracted_order_ids: list[str] = []
    if claimed_order_id:
        extracted_order_ids.append(claimed_order_id)
    if message:
        for match in re.findall(r"\b[0-9a-f]{32}\b", message, re.IGNORECASE):
            if match not in extracted_order_ids:
                extracted_order_ids.append(match)
                if not claimed_order_id:
                    claimed_order_id = match
        for match in re.findall(r"\border_[A-Za-z0-9_-]+\b", message, re.IGNORECASE):
            if match not in extracted_order_ids:
                extracted_order_ids.append(match)
                if not claimed_order_id:
                    claimed_order_id = match

    if customer_id:
        customer_request["customer_id"] = customer_id
    elif message:
        cust_match = re.search(r"\bcustomer_[A-Za-z0-9_-]+\b", message, re.IGNORECASE)
        if cust_match:
            customer_id = cust_match.group(0)
            customer_request["customer_id"] = customer_id

    context = CaseContext(
        case_id=case_id,
        opened_at=case.get("opened_at", ""),
        customer_request=customer_request,
        policy_version=policy_version,
        claimed_order_id=claimed_order_id,
        claims=claims,
    )
    for oid in extracted_order_ids:
        if oid not in context.entities.order_ids:
            context.entities.order_ids.append(oid)

    coordinator = CoordinatorAgent()
    # 1. Lifecycle step 1: case_received
    trace.emit(case_id=case_id, event_type="case_received", actor=coordinator.actor)

    # 2. Lifecycle step 2: task_assigned to order-agent
    triage_attributes: dict[str, str | int | float | bool | None] = {
        "claimed_order_id": claimed_order_id,
        "claims_count": len(claims),
        "customer_id": customer_id,
    }
    coordinator.delegate_task(trace, case_id=case_id, attributes=triage_attributes)
    return {"context": context}


async def order_specialist_node(state: AgentWorkflowState) -> dict[str, Any]:
    context = state["context"]
    gateway = state["gateway"]
    trace = state["trace"]
    specialist = OrderSpecialist()
    await specialist.run(context, gateway, trace)
    return {"context": context}


async def payment_specialist_node(state: AgentWorkflowState) -> dict[str, Any]:
    context = state["context"]
    gateway = state["gateway"]
    trace = state["trace"]
    specialist = PaymentSpecialist()
    await specialist.run(context, gateway, trace)
    return {"context": context}


async def shipment_specialist_node(state: AgentWorkflowState) -> dict[str, Any]:
    context = state["context"]
    gateway = state["gateway"]
    trace = state["trace"]
    specialist = ShipmentSpecialist()
    await specialist.run(context, gateway, trace)
    return {"context": context}


async def policy_specialist_node(state: AgentWorkflowState) -> dict[str, Any]:
    context = state["context"]
    gateway = state["gateway"]
    trace = state["trace"]
    specialist = PolicySpecialist()
    await specialist.run(context, gateway, trace)
    return {"context": context}


async def verifier_node(state: AgentWorkflowState) -> dict[str, Any]:
    context = state["context"]
    contracts = state["contracts"]
    trace = state["trace"]
    verifier = VerifierAgent()
    output = verifier.run(context, contracts, trace)
    return {"output": output}


async def coordinator_finalize_node(state: AgentWorkflowState) -> dict[str, Any]:
    output = state.get("output", {})
    trace = state.get("trace")
    case = state.get("case", {})
    case_id = case.get("case_id", output.get("case_id", ""))

    coordinator = CoordinatorAgent()
    if trace and case_id:
        coordinator.finalize_case(
            trace,
            case_id=case_id,
            decision_code="CASE_RESOLVED",
        )
    return {"output": output}


def build_workflow_graph() -> StateGraph:
    """Build and return the LangGraph StateGraph representing the multi-agent pipeline."""
    graph = StateGraph(AgentWorkflowState)

    graph.add_node("coordinator", coordinator_node)
    graph.add_node("order_specialist", order_specialist_node)
    graph.add_node("payment_specialist", payment_specialist_node)
    graph.add_node("shipment_specialist", shipment_specialist_node)
    graph.add_node("policy_specialist", policy_specialist_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("coordinator_finalize", coordinator_finalize_node)

    graph.add_edge(START, "coordinator")
    graph.add_edge("coordinator", "order_specialist")
    graph.add_edge("order_specialist", "payment_specialist")
    graph.add_edge("payment_specialist", "shipment_specialist")
    graph.add_edge("shipment_specialist", "policy_specialist")
    graph.add_edge("policy_specialist", "verifier")
    graph.add_edge("verifier", "coordinator_finalize")
    graph.add_edge("coordinator_finalize", END)

    return graph


compiled_workflow = build_workflow_graph().compile()
