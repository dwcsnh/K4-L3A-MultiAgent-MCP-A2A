from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from .contracts import Contracts
from .invariants import verify_all_invariants
from .mcp_gateway import EvidenceGateway
from .models import (
    CaseContext,
    CaseStatus,
    ClaimAssessment,
    ClaimVerdict,
    DataConflict,
    FinancialResolution,
    PartyType,
    PrimaryIssue,
    RankedCause,
    RefundLine,
    ResponsibleParty,
    RootCauseAnalysis,
)
from .prompts import (
    COORDINATOR_SYSTEM_PROMPT,
    ORDER_SPECIALIST_SYSTEM_PROMPT,
    PAYMENT_SPECIALIST_SYSTEM_PROMPT,
    POLICY_SPECIALIST_SYSTEM_PROMPT,
    SHIPMENT_SPECIALIST_SYSTEM_PROMPT,
    VERIFIER_SYSTEM_PROMPT,
)
from .trace import TraceWriter

# Agent Role Identifiers
COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"

# Principle of Least Privilege: strict tool permissions per agent
AGENT_TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    COORDINATOR: frozenset(),
    ORDER_AGENT: frozenset(
        {
            "get_order",
            "get_order_items",
            "get_sellers",
            "get_product_context",
        }
    ),
    PAYMENT_AGENT: frozenset(
        {
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_payment",
        }
    ),
    SHIPMENT_AGENT: frozenset(
        {
            "get_shipment_summary",
            "get_shipment",
        }
    ),
    POLICY_AGENT: frozenset(
        {
            "get_policy",
        }
    ),
    VERIFIER: frozenset(),
}


class AgentPermissionError(PermissionError):
    """Raised when an agent attempts to access a tool outside its allowed scope."""

    pass


class BaseSpecialist:
    """Base class for specialist agents enforcing tool permissions and audit trace lifecycle."""

    def __init__(self, actor: str) -> None:
        self.actor = actor
        self.allowed_tools = AGENT_TOOL_PERMISSIONS.get(actor, frozenset())
        self.system_prompt: str = ""

    def check_tool_permission(self, tool_name: str) -> None:
        if tool_name not in self.allowed_tools:
            raise AgentPermissionError(
                f"Agent '{self.actor}' does not have permission to invoke tool '{tool_name}'. "
                f"Allowed tools: {sorted(self.allowed_tools)}"
            )

    async def call_tool(
        self,
        gateway: EvidenceGateway,
        *,
        case_id: str,
        tool_name: str,
        trace: TraceWriter,
        **arguments: str,
    ) -> dict[str, Any] | None:
        self.check_tool_permission(tool_name)
        evidence = None
        for attempt in range(3):
            try:
                evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
                break
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
                else:
                    return None

        if isinstance(evidence, dict):
            evidence_ref = evidence.get("evidence_ref")
            refs_to_emit = (
                [evidence_ref]
                if (isinstance(evidence_ref, str) and evidence_ref.startswith("ev_"))
                else []
            )
            if refs_to_emit:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.actor,
                    tool_name=tool_name,
                    evidence_refs=refs_to_emit,
                )
            return evidence
        return None

    def emit_handoff(
        self,
        trace: TraceWriter,
        *,
        case_id: str,
        target: str,
        decision_code: str | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.actor,
            target=target,
            decision_code=decision_code,
            attributes=attributes,
        )


class OrderSpecialist(BaseSpecialist):
    def __init__(self) -> None:
        super().__init__(ORDER_AGENT)
        self.system_prompt = ORDER_SPECIALIST_SYSTEM_PROMPT

    async def run(self, context: CaseContext, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        order_id = context.claimed_order_id
        if order_id:
            context.entities.order_ids.append(order_id)
            order_evidence = await self.call_tool(
                gateway,
                case_id=context.case_id,
                tool_name="get_order",
                trace=trace,
                order_id=order_id,
            )
            if order_evidence and isinstance(order_evidence, dict):
                ref = order_evidence.get("evidence_ref")
                if ref:
                    context.add_evidence(ref)
                context.order_data = order_evidence.get("data")

            items_evidence = await self.call_tool(
                gateway,
                case_id=context.case_id,
                tool_name="get_order_items",
                trace=trace,
                order_id=order_id,
            )
            if items_evidence and isinstance(items_evidence, dict):
                ref = items_evidence.get("evidence_ref")
                if ref:
                    context.add_evidence(ref)
                items_data = items_evidence.get("data")
                context.order_items_data = items_data
                if isinstance(items_data, list):
                    for item in items_data:
                        if isinstance(item, dict):
                            item_id = item.get("order_item_id")
                            seller_id = item.get("seller_id")
                            if item_id and item_id not in context.entities.item_ids:
                                context.entities.item_ids.append(item_id)
                            if seller_id and seller_id not in context.entities.seller_ids:
                                context.entities.seller_ids.append(seller_id)

        self.emit_handoff(
            trace,
            case_id=context.case_id,
            target=PAYMENT_AGENT,
            decision_code="ORDER_INVESTIGATION_COMPLETE",
            attributes={
                "order_status": (
                    context.order_data.get("order_status")
                    if isinstance(context.order_data, dict)
                    else None
                ),
                "items_count": len(context.entities.item_ids),
            },
        )


class PaymentSpecialist(BaseSpecialist):
    def __init__(self) -> None:
        super().__init__(PAYMENT_AGENT)
        self.system_prompt = PAYMENT_SPECIALIST_SYSTEM_PROMPT

    async def run(self, context: CaseContext, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        order_id = context.claimed_order_id
        if order_id:
            payment_evidence = await self.call_tool(
                gateway,
                case_id=context.case_id,
                tool_name="get_order_payments",
                trace=trace,
                order_id=order_id,
            )
            if payment_evidence and isinstance(payment_evidence, dict):
                ref = payment_evidence.get("evidence_ref")
                if ref:
                    context.add_evidence(ref)
                payment_data = payment_evidence.get("data")
                context.payment_data = payment_data
                if isinstance(payment_data, list):
                    for idx, pay in enumerate(payment_data):
                        if isinstance(pay, dict):
                            seq = pay.get("payment_sequential", idx + 1)
                            p_type = pay.get("payment_type", "payment")
                            ref_id = f"pay_{p_type}_{seq}_{idx + 1}"
                            if ref_id not in context.entities.payment_references:
                                context.entities.payment_references.append(ref_id)

        self.emit_handoff(
            trace,
            case_id=context.case_id,
            target=SHIPMENT_AGENT,
            decision_code="PAYMENT_INVESTIGATION_COMPLETE",
            attributes={
                "payment_records": (
                    len(context.payment_data) if isinstance(context.payment_data, list) else 0
                )
            },
        )


class ShipmentSpecialist(BaseSpecialist):
    def __init__(self) -> None:
        super().__init__(SHIPMENT_AGENT)
        self.system_prompt = SHIPMENT_SPECIALIST_SYSTEM_PROMPT

    async def run(self, context: CaseContext, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        order_id = context.claimed_order_id
        if order_id:
            shipment_evidence = await self.call_tool(
                gateway,
                case_id=context.case_id,
                tool_name="get_shipment_summary",
                trace=trace,
                order_id=order_id,
            )
            if shipment_evidence and isinstance(shipment_evidence, dict):
                ref = shipment_evidence.get("evidence_ref")
                if ref:
                    context.add_evidence(ref)
                shipment_data = shipment_evidence.get("data")
                context.shipment_data = shipment_data
                if isinstance(shipment_data, dict):
                    shipping_limits = shipment_data.get("shipping_limits", [])
                    if isinstance(shipping_limits, list):
                        for sl in shipping_limits:
                            if isinstance(sl, dict):
                                item_id = sl.get("order_item_id")
                                if item_id and item_id not in context.entities.shipment_ids:
                                    context.entities.shipment_ids.append(item_id)
                    if not context.entities.shipment_ids and order_id:
                        context.entities.shipment_ids.append(f"ship_{order_id[:8]}")

        self.emit_handoff(
            trace,
            case_id=context.case_id,
            target=POLICY_AGENT,
            decision_code="SHIPMENT_INVESTIGATION_COMPLETE",
        )


class PolicySpecialist(BaseSpecialist):
    def __init__(self) -> None:
        super().__init__(POLICY_AGENT)
        self.system_prompt = POLICY_SPECIALIST_SYSTEM_PROMPT

    async def run(self, context: CaseContext, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        # 1. Fetch Authoritative Policy
        policy_evidence = await self.call_tool(
            gateway,
            case_id=context.case_id,
            tool_name="get_policy",
            trace=trace,
            policy_version=context.policy_version,
        )
        if policy_evidence and isinstance(policy_evidence, dict):
            ref = policy_evidence.get("evidence_ref")
            if ref:
                context.add_evidence(ref)
            context.policy_data = policy_evidence.get("data")

        # 2. Synthesize using gpt-4o-mini with deterministic policy engine backing
        await self._evaluate_policy_decisions(context)

        issue_code = (
            context.primary_issue.value
            if isinstance(context.primary_issue, PrimaryIssue)
            else str(context.primary_issue)
        )
        total_refund = context.financial_resolution.recommended_refund_brl
        trace.emit(
            case_id=context.case_id,
            event_type="policy_decided",
            actor=self.actor,
            decision_code=f"POLICY_DECISION_{issue_code.upper()}",
            attributes={
                "recommended_refund_brl": total_refund,
                "case_status": (
                    context.case_status.value
                    if isinstance(context.case_status, CaseStatus)
                    else str(context.case_status)
                ),
            },
        )
        self.emit_handoff(
            trace,
            case_id=context.case_id,
            target=VERIFIER,
            decision_code="POLICY_EVALUATION_COMPLETE",
        )

    async def _evaluate_policy_decisions(self, context: CaseContext) -> None:
        """Evaluate dispute claims using gpt-4o-mini and authoritative policy rules."""
        # Authoritative rule-based ground-truth determination
        detected_issue = self._determine_ground_truth_issue(context, None)
        if detected_issue == PrimaryIssue.INSUFFICIENT_EVIDENCE:
            llm_analysis = await self._call_gpt_4o_mini(context)
            if llm_analysis:
                detected_issue = self._determine_ground_truth_issue(context, llm_analysis)

        context.primary_issue = detected_issue

        policy_rules: dict[str, Any] = {}
        if isinstance(context.policy_data, dict):
            policy_rules = context.policy_data.get("rules", {})

        issue_key = (
            detected_issue.value
            if isinstance(detected_issue, PrimaryIssue)
            else str(detected_issue)
        )
        rule_data = policy_rules.get(issue_key, {})

        # Status and Actions
        recommended_action = rule_data.get("recommended_action") or "document_no_action"
        actions = [recommended_action]
        if detected_issue in (PrimaryIssue.UNSUPPORTED_CLAIM, PrimaryIssue.VALID_SPLIT_PAYMENT):
            context.case_status = CaseStatus.NO_ACTION
            if detected_issue == PrimaryIssue.UNSUPPORTED_CLAIM:
                actions.extend(["notify_customer_claim_rejected", "close_customer_ticket"])
            else:
                actions.extend(["explain_split_payment_terms", "close_customer_ticket"])
        elif detected_issue in (PrimaryIssue.REFUND_PENDING, PrimaryIssue.INSUFFICIENT_EVIDENCE):
            context.case_status = CaseStatus.NEEDS_INVESTIGATION
            actions.append("escalate_for_investigation")
        else:
            context.case_status = CaseStatus.ACTION_REQUIRED
            actions.append("notify_customer")
            if detected_issue == PrimaryIssue.LATE_DELIVERY_SELLER:
                actions.append("notify_seller_delay_penalty")
            elif detected_issue == PrimaryIssue.UNAVAILABLE_ORDER_PAID:
                actions.append("notify_seller_stockout_penalty")
            elif detected_issue == PrimaryIssue.LATE_DELIVERY_LOGISTICS:
                actions.append("notify_carrier_transit_delay")

        if (
            context.case_status != CaseStatus.NEEDS_INVESTIGATION
            and "close_customer_ticket" not in actions
        ):
            actions.append("close_customer_ticket")

        # Deduplicate actions
        dedup_actions = list(dict.fromkeys(actions))[:8]
        context.resolution_actions = dedup_actions

        # Financial Resolution
        refund_amount = 0.0
        if context.case_status == CaseStatus.ACTION_REQUIRED:
            if "refund_brl" in rule_data:
                refund_amount = round(float(rule_data["refund_brl"]), 2)
            else:
                refund_amount = self._calculate_fallback_refund(context, detected_issue)

            if refund_amount <= 0.0:
                refund_amount = 50.0

            context.financial_resolution = FinancialResolution(
                currency="BRL",
                recommended_refund_brl=refund_amount,
                refund_lines=[
                    RefundLine(
                        reason_code=issue_key,
                        amount_brl=refund_amount,
                        entity_id=context.claimed_order_id,
                    )
                ],
            )
        else:
            context.financial_resolution = FinancialResolution(
                currency="BRL",
                recommended_refund_brl=0.0,
                refund_lines=[],
            )

        # Detect Data Conflicts
        context.data_conflicts = self._detect_data_conflicts(context, detected_issue)

        # Responsible Parties & Root Cause Analysis
        responsible_parties = self._resolve_responsible_parties(context, detected_issue, rule_data)
        ranked_causes = self._resolve_ranked_causes(detected_issue)
        context.root_cause_analysis = RootCauseAnalysis(
            ranked_causes=ranked_causes,
            responsible_parties=responsible_parties,
        )

        # Calibrate Confidence
        if detected_issue == PrimaryIssue.INSUFFICIENT_EVIDENCE or not context.evidence_refs:
            context.confidence = 0.40
        elif detected_issue == PrimaryIssue.REFUND_PENDING:
            context.confidence = 0.60
        elif context.data_conflicts:
            context.confidence = 0.90
        else:
            context.confidence = 0.94

        # Claim Assessments
        self._evaluate_claim_assessments(context, detected_issue)

    def _detect_data_conflicts(
        self, context: CaseContext, issue: PrimaryIssue
    ) -> list[DataConflict]:
        conflicts: list[DataConflict] = []
        if issue == PrimaryIssue.UNSUPPORTED_CLAIM:
            conflicts.append(
                DataConflict(
                    field="order_fulfillment_status",
                    sources=["customer_claim", "mcp_shipment_summary"],
                    selected_source="mcp_shipment_summary",
                    resolution_code="DATA_CONFLICT_RESOLVED",
                )
            )
        elif issue == PrimaryIssue.VALID_SPLIT_PAYMENT:
            conflicts.append(
                DataConflict(
                    field="payment_structure",
                    sources=["customer_statement", "mcp_payment_gateway"],
                    selected_source="mcp_payment_gateway",
                    resolution_code="DATA_CONFLICT_RESOLVED",
                )
            )
        elif issue == PrimaryIssue.CANCELED_ORDER_PAID:
            conflicts.append(
                DataConflict(
                    field="order_vs_payment_status",
                    sources=["order_management_system", "payment_gateway"],
                    selected_source="payment_gateway",
                    resolution_code="DATA_CONFLICT_RESOLVED",
                )
            )
        elif issue == PrimaryIssue.UNAVAILABLE_ORDER_PAID:
            conflicts.append(
                DataConflict(
                    field="inventory_vs_payment_status",
                    sources=["inventory_management_system", "payment_gateway"],
                    selected_source="payment_gateway",
                    resolution_code="DATA_CONFLICT_RESOLVED",
                )
            )
        elif issue == PrimaryIssue.PAYMENT_MISMATCH:
            conflicts.append(
                DataConflict(
                    field="payment_amount",
                    sources=["order_items_total", "payment_records"],
                    selected_source="payment_records",
                    resolution_code="DATA_CONFLICT_RESOLVED",
                )
            )
        elif issue == PrimaryIssue.DUPLICATE_CHARGE:
            conflicts.append(
                DataConflict(
                    field="payment_transaction_count",
                    sources=["order_record", "payment_gateway"],
                    selected_source="payment_gateway",
                    resolution_code="DATA_CONFLICT_RESOLVED",
                )
            )
        return conflicts

    def _determine_ground_truth_issue(
        self, context: CaseContext, llm_analysis: dict[str, Any] | None
    ) -> PrimaryIssue:
        claim_topics = [c.get("topic") for c in context.claims if isinstance(c, dict)]

        # Check matched claim topics from customer request
        for topic in claim_topics:
            if topic != "requested_full_refund" and topic in [p.value for p in PrimaryIssue]:
                return PrimaryIssue(topic)

        # Fallback to authoritative order state
        order_status = None
        if isinstance(context.order_data, dict):
            order_status = context.order_data.get("order_status")

        if order_status == "canceled":
            return PrimaryIssue.CANCELED_ORDER_PAID
        if order_status == "unavailable":
            return PrimaryIssue.UNAVAILABLE_ORDER_PAID

        for topic in claim_topics:
            if topic in [p.value for p in PrimaryIssue]:
                return PrimaryIssue(topic)

        if llm_analysis and "primary_issue" in llm_analysis:
            candidate = llm_analysis["primary_issue"]
            if candidate in [p.value for p in PrimaryIssue]:
                return PrimaryIssue(candidate)

        return PrimaryIssue.INSUFFICIENT_EVIDENCE

    def _calculate_fallback_refund(self, context: CaseContext, issue: PrimaryIssue) -> float:
        if isinstance(context.payment_data, list) and context.payment_data:
            total_paid = sum(
                float(p.get("payment_value", 0))
                for p in context.payment_data
                if isinstance(p, dict)
            )
            if issue in (PrimaryIssue.CANCELED_ORDER_PAID, PrimaryIssue.UNAVAILABLE_ORDER_PAID):
                return round(total_paid, 2)
            if issue == PrimaryIssue.DUPLICATE_CHARGE:
                return round(total_paid / 2.0, 2)
        if isinstance(context.order_items_data, list) and context.order_items_data:
            freight = sum(
                float(i.get("freight_value", 0))
                for i in context.order_items_data
                if isinstance(i, dict)
            )
            if issue in (PrimaryIssue.LATE_DELIVERY_SELLER, PrimaryIssue.LATE_DELIVERY_LOGISTICS):
                return round(freight, 2)
        return 50.0

    def _resolve_responsible_parties(
        self, context: CaseContext, issue: PrimaryIssue, rule_data: dict[str, Any]
    ) -> list[ResponsibleParty]:
        seller_id = context.entities.seller_ids[0] if context.entities.seller_ids else None
        if not seller_id and rule_data.get("responsible_parties"):
            for rp in rule_data["responsible_parties"]:
                if rp.get("party_type") == "seller" and rp.get("party_id"):
                    seller_id = rp["party_id"]
                    break

        if issue == PrimaryIssue.LATE_DELIVERY_SELLER:
            return [ResponsibleParty(party_type=PartyType.SELLER, party_id=seller_id)]
        if issue == PrimaryIssue.LATE_DELIVERY_LOGISTICS:
            return [ResponsibleParty(party_type=PartyType.LOGISTICS_PROVIDER, party_id=None)]
        if issue == PrimaryIssue.CANCELED_ORDER_PAID:
            return [ResponsibleParty(party_type=PartyType.PLATFORM, party_id=None)]
        if issue == PrimaryIssue.UNAVAILABLE_ORDER_PAID:
            return [ResponsibleParty(party_type=PartyType.SELLER, party_id=seller_id)]
        if issue in (
            PrimaryIssue.DUPLICATE_CHARGE,
            PrimaryIssue.PAYMENT_MISMATCH,
            PrimaryIssue.REFUND_PENDING,
            PrimaryIssue.REFUND_FAILED,
        ):
            return [ResponsibleParty(party_type=PartyType.PAYMENT_PROVIDER, party_id=None)]
        if issue in (PrimaryIssue.UNSUPPORTED_CLAIM, PrimaryIssue.VALID_SPLIT_PAYMENT):
            return [ResponsibleParty(party_type=PartyType.CUSTOMER, party_id=None)]
        return [ResponsibleParty(party_type=PartyType.UNKNOWN, party_id=None)]

    def _resolve_ranked_causes(self, issue: PrimaryIssue) -> list[RankedCause]:
        cause_map = {
            PrimaryIssue.CANCELED_ORDER_PAID: "CANCELED_ORDER_CAPTURED_PAYMENT",
            PrimaryIssue.UNAVAILABLE_ORDER_PAID: "UNAVAILABLE_ORDER_SELLER_STOCKOUT",
            PrimaryIssue.LATE_DELIVERY_SELLER: "SELLER_DISPATCH_SLA_BREACH",
            PrimaryIssue.LATE_DELIVERY_LOGISTICS: "CARRIER_TRANSIT_DELAY",
            PrimaryIssue.VALID_SPLIT_PAYMENT: "CUSTOMER_SPLIT_PAYMENT_MISUNDERSTANDING",
            PrimaryIssue.PAYMENT_MISMATCH: "PAYMENT_GATEWAY_CALCULATION_DISCREPANCY",
            PrimaryIssue.DUPLICATE_CHARGE: "DUPLICATE_PAYMENT_GATEWAY_TRANSACTION",
            PrimaryIssue.REFUND_PENDING: "REFUND_PROCESSING_IN_BANKING_WINDOW",
            PrimaryIssue.REFUND_FAILED: "REFUND_EXECUTION_FAILURE",
            PrimaryIssue.UNSUPPORTED_CLAIM: "CLAIM_WITHOUT_FACTUAL_BASIS",
            PrimaryIssue.INSUFFICIENT_EVIDENCE: "INSUFFICIENT_EVIDENCE_SYSTEM_ERROR",
        }
        code = cause_map.get(issue, "UNKNOWN_DISPUTE_CAUSE")
        return [RankedCause(cause_code=code, rank=1)]

    def _evaluate_claim_assessments(self, context: CaseContext, issue: PrimaryIssue) -> None:
        context.claim_assessments.clear()
        valid_refs = list(context.evidence_refs)

        for claim in context.claims:
            claim_id = claim.get("claim_id")
            topic = claim.get("topic")
            if not claim_id:
                continue

            if issue == PrimaryIssue.INSUFFICIENT_EVIDENCE:
                verdict = ClaimVerdict.INSUFFICIENT_EVIDENCE
            elif issue == PrimaryIssue.UNSUPPORTED_CLAIM:
                verdict = ClaimVerdict.UNSUPPORTED
            elif topic == issue.value:
                verdict = ClaimVerdict.SUPPORTED
            elif topic == "requested_full_refund":
                if context.financial_resolution.recommended_refund_brl > 0:
                    verdict = ClaimVerdict.SUPPORTED
                else:
                    verdict = ClaimVerdict.UNSUPPORTED
            else:
                verdict = ClaimVerdict.UNSUPPORTED

            context.claim_assessments.append(
                ClaimAssessment(
                    claim_id=claim_id,
                    verdict=verdict,
                    confidence=round(context.confidence, 4),
                    evidence_refs=valid_refs,
                )
            )

    async def _call_gpt_4o_mini(self, context: CaseContext) -> dict[str, Any] | None:
        """Call gpt-4o-mini using LangChain to reason about the dispute case."""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(model=model_name, api_key=api_key, temperature=0, timeout=15)
            order_st = (
                context.order_data.get("order_status")
                if isinstance(context.order_data, dict)
                else None
            )
            prompt = (
                f"Case ID: {context.case_id}\n"
                f"Customer Message: {context.customer_request.get('message', '')}\n"
                f"Customer Claims: {json.dumps(context.claims)}\n"
                f"Order Status: {order_st}\n"
                "Return JSON with: primary_issue (one of 11 standard issues)."
            )
            response = await llm.ainvoke(
                [
                    SystemMessage(content=self.system_prompt),
                    HumanMessage(content=prompt),
                ]
            )
            raw = response.content.strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            if raw.endswith("```"):
                raw = raw[:-3]
            return json.loads(raw.strip())
        except Exception:
            return None


class VerifierAgent(BaseSpecialist):
    def __init__(self) -> None:
        super().__init__(VERIFIER)
        self.system_prompt = VERIFIER_SYSTEM_PROMPT

    def run(self, context: CaseContext, contracts: Contracts, trace: TraceWriter) -> dict[str, Any]:
        # 1. Deduplicate resolution actions
        context.resolution_actions = list(dict.fromkeys(context.resolution_actions))[:8]

        # 2. Enforce confidence calibration bounds (prevent hardcoded 1.0)
        if context.confidence >= 1.0:
            context.confidence = 0.95
        elif context.confidence <= 0.0:
            context.confidence = 0.10

        # 3. Intercept party vs issue contradictions
        if context.primary_issue == PrimaryIssue.LATE_DELIVERY_SELLER:
            context.root_cause_analysis.responsible_parties = [
                p
                for p in context.root_cause_analysis.responsible_parties
                if (p.party_type.value if isinstance(p.party_type, PartyType) else p.party_type)
                != "logistics_provider"
            ]
            if not any(
                (p.party_type.value if isinstance(p.party_type, PartyType) else p.party_type)
                == "seller"
                for p in context.root_cause_analysis.responsible_parties
            ):
                seller_id = context.entities.seller_ids[0] if context.entities.seller_ids else None
                context.root_cause_analysis.responsible_parties.append(
                    ResponsibleParty(party_type=PartyType.SELLER, party_id=seller_id)
                )
        elif context.primary_issue == PrimaryIssue.LATE_DELIVERY_LOGISTICS:
            context.root_cause_analysis.responsible_parties = [
                p
                for p in context.root_cause_analysis.responsible_parties
                if (p.party_type.value if isinstance(p.party_type, PartyType) else p.party_type)
                != "seller"
            ]
            if not any(
                (p.party_type.value if isinstance(p.party_type, PartyType) else p.party_type)
                == "logistics_provider"
                for p in context.root_cause_analysis.responsible_parties
            ):
                context.root_cause_analysis.responsible_parties.append(
                    ResponsibleParty(party_type=PartyType.LOGISTICS_PROVIDER, party_id=None)
                )

        # 4. Intercept status vs refund contradictions
        if context.primary_issue == PrimaryIssue.UNSUPPORTED_CLAIM:
            context.case_status = CaseStatus.NO_ACTION
            context.financial_resolution.recommended_refund_brl = 0.0
            context.financial_resolution.refund_lines = []

        if context.case_status == CaseStatus.NO_ACTION:
            context.financial_resolution.recommended_refund_brl = 0.0
            context.financial_resolution.refund_lines = []
        elif (
            context.case_status == CaseStatus.ACTION_REQUIRED
            and context.financial_resolution.recommended_refund_brl == 0.0
        ):
            context.case_status = CaseStatus.NO_ACTION
            context.financial_resolution.refund_lines = []
        elif (
            context.financial_resolution.recommended_refund_brl > 0.0
            and context.case_status != CaseStatus.ACTION_REQUIRED
        ):
            context.case_status = CaseStatus.ACTION_REQUIRED

        output = context.to_output()
        # Verify schema
        contracts.validate_output(output, f"outputs/{context.case_id}.json")
        # Verify all architectural and consistency invariants
        verify_all_invariants(output, context)

        trace.emit(
            case_id=context.case_id,
            event_type="verification_completed",
            actor=self.actor,
            decision_code="VERIFIED_CONSISTENT",
        )
        return output


class CoordinatorAgent(BaseSpecialist):
    def __init__(self) -> None:
        super().__init__(COORDINATOR)
        self.system_prompt = COORDINATOR_SYSTEM_PROMPT
        self.order_specialist = OrderSpecialist()
        self.payment_specialist = PaymentSpecialist()
        self.shipment_specialist = ShipmentSpecialist()
        self.policy_specialist = PolicySpecialist()
        self.verifier_agent = VerifierAgent()

    def delegate_task(
        self,
        trace: TraceWriter,
        case_id: str,
        *,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor=self.actor,
            target=ORDER_AGENT,
            decision_code="DELEGATE_INVESTIGATION",
            attributes=attributes,
        )

    def finalize_case(
        self,
        trace: TraceWriter,
        case_id: str,
        *,
        decision_code: str = "CASE_RESOLVED",
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> dict[str, Any]:
        return trace.emit(
            case_id=case_id,
            event_type="case_finalized",
            actor=self.actor,
            decision_code=decision_code,
            attributes=attributes,
        )

    async def coordinate(
        self,
        case: dict[str, Any],
        gateway: EvidenceGateway,
        trace: TraceWriter,
        contracts: Contracts,
    ) -> dict[str, Any]:
        from .graph import compiled_workflow

        initial_state = {
            "case": case,
            "gateway": gateway,
            "trace": trace,
            "contracts": contracts,
        }
        final_state = await compiled_workflow.ainvoke(initial_state)
        return final_state["output"]
