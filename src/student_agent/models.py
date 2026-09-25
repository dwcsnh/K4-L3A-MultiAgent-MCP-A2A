from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from . import OUTPUT_SCHEMA_VERSION


class PrimaryIssue(StrEnum):
    CANCELED_ORDER_PAID = "canceled_order_paid"
    UNAVAILABLE_ORDER_PAID = "unavailable_order_paid"
    LATE_DELIVERY_SELLER = "late_delivery_seller"
    LATE_DELIVERY_LOGISTICS = "late_delivery_logistics"
    VALID_SPLIT_PAYMENT = "valid_split_payment"
    PAYMENT_MISMATCH = "payment_mismatch"
    DUPLICATE_CHARGE = "duplicate_charge"
    REFUND_PENDING = "refund_pending"
    REFUND_FAILED = "refund_failed"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class CaseStatus(StrEnum):
    ACTION_REQUIRED = "action_required"
    NO_ACTION = "no_action"
    NEEDS_INVESTIGATION = "needs_investigation"


class PartyType(StrEnum):
    SELLER = "seller"
    PLATFORM = "platform"
    LOGISTICS_PROVIDER = "logistics_provider"
    PAYMENT_PROVIDER = "payment_provider"
    CUSTOMER = "customer"
    UNKNOWN = "unknown"


class ClaimVerdict(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    PARTIALLY_SUPPORTED = "partially_supported"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass
class Assessment:
    primary_issue: PrimaryIssue | str
    case_status: CaseStatus | str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_issue": (
                self.primary_issue.value
                if isinstance(self.primary_issue, PrimaryIssue)
                else self.primary_issue
            ),
            "case_status": (
                self.case_status.value
                if isinstance(self.case_status, CaseStatus)
                else self.case_status
            ),
            "confidence": round(float(self.confidence), 4),
        }


@dataclass
class AffectedEntities:
    order_ids: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    payment_references: list[str] = field(default_factory=list)
    shipment_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        def _dedup_list(items: list[str], max_len: int = 20) -> list[str]:
            seen: set[str] = set()
            result: list[str] = []
            for item in items:
                if item and item not in seen:
                    seen.add(item)
                    result.append(item)
                if len(result) >= max_len:
                    break
            return result

        return {
            "order_ids": _dedup_list(self.order_ids),
            "item_ids": _dedup_list(self.item_ids),
            "seller_ids": _dedup_list(self.seller_ids),
            "payment_references": _dedup_list(self.payment_references),
            "shipment_ids": _dedup_list(self.shipment_ids),
        }


@dataclass
class ClaimAssessment:
    claim_id: str
    verdict: ClaimVerdict | str
    confidence: float
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        seen: set[str] = set()
        dedup_refs: list[str] = []
        for ref in self.evidence_refs:
            if ref and ref not in seen:
                seen.add(ref)
                dedup_refs.append(ref)
            if len(dedup_refs) >= 30:
                break
        return {
            "claim_id": self.claim_id,
            "verdict": (
                self.verdict.value if isinstance(self.verdict, ClaimVerdict) else self.verdict
            ),
            "confidence": round(float(self.confidence), 4),
            "evidence_refs": dedup_refs,
        }


@dataclass
class RankedCause:
    cause_code: str
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause_code": self.cause_code,
            "rank": int(self.rank),
        }


@dataclass
class ResponsibleParty:
    party_type: PartyType | str
    party_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "party_type": (
                self.party_type.value if isinstance(self.party_type, PartyType) else self.party_type
            ),
            "party_id": self.party_id,
        }


@dataclass
class RootCauseAnalysis:
    ranked_causes: list[RankedCause] = field(default_factory=list)
    responsible_parties: list[ResponsibleParty] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ranked_causes": [c.to_dict() for c in self.ranked_causes[:5]],
            "responsible_parties": [p.to_dict() for p in self.responsible_parties[:5]],
        }


@dataclass
class DataConflict:
    field: str
    sources: list[str]
    selected_source: str | None
    resolution_code: str

    def to_dict(self) -> dict[str, Any]:
        seen: set[str] = set()
        sources_dedup = [s for s in self.sources if not (s in seen or seen.add(s))][:5]
        return {
            "field": self.field,
            "sources": sources_dedup,
            "selected_source": self.selected_source,
            "resolution_code": self.resolution_code,
        }


@dataclass
class RefundLine:
    reason_code: str
    amount_brl: float
    entity_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "amount_brl": round(float(self.amount_brl), 2),
            "entity_id": self.entity_id,
        }


@dataclass
class FinancialResolution:
    currency: str = "BRL"
    recommended_refund_brl: float = 0.0
    refund_lines: list[RefundLine] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": "BRL",
            "recommended_refund_brl": round(float(self.recommended_refund_brl), 2),
            "refund_lines": [line.to_dict() for line in self.refund_lines[:10]],
        }


@dataclass
class CaseOutput:
    case_id: str
    assessment: Assessment
    affected_entities: AffectedEntities
    root_cause_analysis: RootCauseAnalysis
    evidence_refs: list[str]
    data_conflicts: list[DataConflict]
    financial_resolution: FinancialResolution
    resolution_actions: list[str]
    claim_assessments: list[ClaimAssessment] | None = None

    def to_dict(self) -> dict[str, Any]:
        seen_refs: set[str] = set()
        dedup_refs: list[str] = []
        for r in self.evidence_refs:
            if r and r not in seen_refs:
                seen_refs.add(r)
                dedup_refs.append(r)
            if len(dedup_refs) >= 30:
                break

        seen_actions: set[str] = set()
        dedup_actions: list[str] = []
        for a in self.resolution_actions:
            if a and a not in seen_actions:
                seen_actions.add(a)
                dedup_actions.append(a)
            if len(dedup_actions) >= 8:
                break

        payload: dict[str, Any] = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "case_id": self.case_id,
            "assessment": self.assessment.to_dict(),
            "affected_entities": self.affected_entities.to_dict(),
            "root_cause_analysis": self.root_cause_analysis.to_dict(),
            "evidence_refs": dedup_refs,
            "data_conflicts": [dc.to_dict() for dc in self.data_conflicts[:5]],
            "financial_resolution": self.financial_resolution.to_dict(),
            "resolution_actions": dedup_actions,
        }
        if self.claim_assessments is not None:
            payload["claim_assessments"] = [ca.to_dict() for ca in self.claim_assessments[:5]]
        return payload


@dataclass
class CaseContext:
    """State carried through the multi-agent DAG workflow."""

    case_id: str
    opened_at: str
    customer_request: dict[str, Any]
    policy_version: str
    claimed_order_id: str | None = None
    claims: list[dict[str, Any]] = field(default_factory=list)

    # Accumulated evidences and entities
    evidence_refs: list[str] = field(default_factory=list)
    entities: AffectedEntities = field(default_factory=AffectedEntities)

    # Specialist raw artifacts
    order_data: dict[str, Any] | None = None
    order_items_data: dict[str, Any] | None = None
    payment_data: dict[str, Any] | None = None
    shipment_data: dict[str, Any] | None = None
    policy_data: dict[str, Any] | None = None

    # Investigations and outputs
    data_conflicts: list[DataConflict] = field(default_factory=list)
    claim_assessments: list[ClaimAssessment] = field(default_factory=list)
    primary_issue: PrimaryIssue | str = PrimaryIssue.INSUFFICIENT_EVIDENCE
    case_status: CaseStatus | str = CaseStatus.NEEDS_INVESTIGATION
    confidence: float = 0.5
    root_cause_analysis: RootCauseAnalysis = field(default_factory=RootCauseAnalysis)
    financial_resolution: FinancialResolution = field(default_factory=FinancialResolution)
    resolution_actions: list[str] = field(default_factory=list)

    def add_evidence(self, ref: str) -> None:
        if ref and ref not in self.evidence_refs:
            self.evidence_refs.append(ref)

    def to_output(self) -> dict[str, Any]:
        assessment = Assessment(
            primary_issue=self.primary_issue,
            case_status=self.case_status,
            confidence=self.confidence,
        )
        output = CaseOutput(
            case_id=self.case_id,
            assessment=assessment,
            affected_entities=self.entities,
            root_cause_analysis=self.root_cause_analysis,
            evidence_refs=list(self.evidence_refs),
            data_conflicts=self.data_conflicts,
            financial_resolution=self.financial_resolution,
            resolution_actions=self.resolution_actions,
            claim_assessments=self.claim_assessments if self.claim_assessments else None,
        )
        return output.to_dict()
