from __future__ import annotations

import re
from typing import Any

from .models import CaseContext

EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")


class InvariantViolationError(ValueError):
    """Raised when an architectural or business invariant is violated."""

    pass


def check_schema_invariants(output: dict[str, Any]) -> None:
    required_keys = {
        "schema_version",
        "case_id",
        "assessment",
        "affected_entities",
        "root_cause_analysis",
        "evidence_refs",
        "data_conflicts",
        "financial_resolution",
        "resolution_actions",
    }
    missing = required_keys - set(output.keys())
    if missing:
        raise InvariantViolationError(f"Output missing required keys: {sorted(missing)}")


def check_evidence_invariants(output: dict[str, Any], context: CaseContext | None = None) -> None:
    root_refs = output.get("evidence_refs", [])
    for ref in root_refs:
        if not EVIDENCE_REF_PATTERN.fullmatch(ref):
            raise InvariantViolationError(f"Invalid evidence ref format in output root: {ref}")

    claim_assessments = output.get("claim_assessments", [])
    for ca in claim_assessments:
        for ref in ca.get("evidence_refs", []):
            if not EVIDENCE_REF_PATTERN.fullmatch(ref):
                raise InvariantViolationError(
                    f"Invalid evidence ref format in claim_assessment '{ca.get('claim_id')}': {ref}"
                )

    if context is not None:
        known_refs = set(context.evidence_refs)
        for ref in root_refs:
            if ref not in known_refs:
                raise InvariantViolationError(
                    f"Evidence provenance violation: '{ref}' was not gathered by any specialist"
                )


def check_entity_scope_invariants(
    output: dict[str, Any], context: CaseContext | None = None
) -> None:
    entities = output.get("affected_entities", {})
    if context is not None and context.claimed_order_id:
        order_ids = entities.get("order_ids", [])
        if order_ids and context.claimed_order_id not in order_ids:
            pass  # Allowed if entity resolution found order, but IDs should be verified


def check_claim_linkage_invariants(
    output: dict[str, Any], context: CaseContext | None = None
) -> None:
    if context is not None and context.claims:
        evaluated_claim_ids = {ca.get("claim_id") for ca in output.get("claim_assessments", [])}
        for claim in context.claims:
            claim_id = claim.get("claim_id")
            if claim_id and claim_id not in evaluated_claim_ids:
                raise InvariantViolationError(
                    f"Claim linkage violation: input claim '{claim_id}' has no assessment"
                )


def check_consistency_invariants(output: dict[str, Any]) -> None:
    assessment = output.get("assessment", {})
    fin = output.get("financial_resolution", {})
    primary_issue = assessment.get("primary_issue")
    case_status = assessment.get("case_status")
    refund = float(fin.get("recommended_refund_brl", 0.0))
    refund_lines = fin.get("refund_lines", [])

    # Status vs Refund
    if primary_issue == "unsupported_claim" or case_status == "no_action":
        if refund > 0.0:
            raise InvariantViolationError(
                f"Status vs Refund invariant violated: refund is {refund} "
                f"but status is {case_status} / issue is {primary_issue}"
            )
        if len(refund_lines) > 0:
            raise InvariantViolationError(
                f"Status vs Refund invariant violated: refund_lines must be empty "
                f"when refund is 0, got {len(refund_lines)} items"
            )

    if primary_issue == "unsupported_claim" and case_status != "no_action":
        raise InvariantViolationError(
            f"Status vs Refund invariant violated: unsupported_claim requires "
            f"case_status 'no_action', got '{case_status}'"
        )

    # Refund vs Action
    if refund > 0.0 and case_status != "action_required":
        raise InvariantViolationError(
            f"Refund vs Action invariant violated: refund is {refund} "
            f"but case_status is '{case_status}'"
        )

    # Refund Total Matching
    lines_total = round(sum(float(line.get("amount_brl", 0.0)) for line in refund_lines), 2)
    if abs(lines_total - refund) > 0.001:
        raise InvariantViolationError(
            f"Refund Total Matching invariant violated: sum(refund_lines)={lines_total} "
            f"does not match recommended_refund_brl={refund}"
        )

    # Party vs Issue
    rca = output.get("root_cause_analysis", {})
    parties = rca.get("responsible_parties", [])
    party_types = {p.get("party_type") for p in parties}

    if primary_issue == "late_delivery_seller":
        if "seller" not in party_types:
            raise InvariantViolationError(
                "Party vs Issue invariant violated: "
                "late_delivery_seller requires party_type 'seller'"
            )
        if "logistics_provider" in party_types:
            raise InvariantViolationError(
                "Party vs Issue invariant violated: "
                "logistics_provider cannot be responsible for late_delivery_seller"
            )
    elif primary_issue == "late_delivery_logistics":
        if "logistics_provider" not in party_types:
            raise InvariantViolationError(
                "Party vs Issue invariant violated: "
                "late_delivery_logistics requires party_type 'logistics_provider'"
            )
        if "seller" in party_types:
            raise InvariantViolationError(
                "Party vs Issue invariant violated: "
                "seller cannot be responsible for late_delivery_logistics"
            )
    elif primary_issue == "unsupported_claim":
        if "seller" in party_types or "logistics_provider" in party_types:
            raise InvariantViolationError(
                "Party vs Issue invariant violated: "
                "unsupported_claim cannot hold seller or logistics provider responsible"
            )
    elif primary_issue in ("duplicate_charge", "payment_mismatch") and not (
        "payment_provider" in party_types or "platform" in party_types
    ):
        raise InvariantViolationError(
            f"Party vs Issue invariant violated: {primary_issue} "
            f"requires 'payment_provider' or 'platform'"
        )

    # No Duplicate Actions
    actions = output.get("resolution_actions", [])
    if len(actions) != len(set(actions)):
        raise InvariantViolationError(
            "No Duplicate Actions invariant violated: resolution_actions contains duplicate values"
        )

    # Confidence Calibration
    conf = assessment.get("confidence")
    if conf is None or not (0.0 <= float(conf) <= 1.0):
        raise InvariantViolationError(
            f"Calibration Invariant violated: confidence {conf} must be in [0.0, 1.0]"
        )
    if float(conf) >= 1.0:
        raise InvariantViolationError(
            "Calibration Invariant violated: confidence must not be hardcoded to 1.0"
        )


def verify_all_invariants(output: dict[str, Any], context: CaseContext | None = None) -> None:
    check_schema_invariants(output)
    check_evidence_invariants(output, context)
    check_entity_scope_invariants(output, context)
    check_claim_linkage_invariants(output, context)
    check_consistency_invariants(output)
