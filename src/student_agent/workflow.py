from __future__ import annotations

from pathlib import Path
from typing import Any

from .agents import CoordinatorAgent
from .contracts import Contracts
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the L3A coordinator and specialist-agent multi-agent workflow.

    Coordinates the sequential DAG pipeline:
    Coordinator -> OrderSpecialist -> PaymentSpecialist
    -> ShipmentSpecialist -> PolicySpecialist -> VerifierAgent -> CoordinatorFinalize
    """
    contracts = getattr(trace, "contracts", None)
    if contracts is None:
        contracts = Contracts(Path("contracts/schemas"))
    coordinator = CoordinatorAgent()
    return await coordinator.coordinate(case, gateway, trace, contracts)
