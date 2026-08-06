from __future__ import annotations

from typing import Literal


DecisionName = Literal["approve", "deny", "partial_refund", "request_info", "manual_review"]
GovernanceStatus = Literal["allow", "block"]
RouteName = Literal["refund_agent", "response_agent", "human_approval"]


def route_policy(decision: DecisionName, governance_status: GovernanceStatus) -> RouteName:
    """Resolve the parent-graph route without changing business data."""

    if governance_status == "block":
        return "human_approval"
    return {
        "approve": "refund_agent",
        "partial_refund": "refund_agent",
        "deny": "response_agent",
        "request_info": "response_agent",
        "manual_review": "human_approval",
    }[decision]


def handoff_reason(decision: DecisionName, governance_status: GovernanceStatus) -> str:
    if governance_status == "block":
        return "Policy governance requires human review."
    return {
        "approve": "The approved refund requires refund execution.",
        "partial_refund": "The approved partial refund requires refund execution.",
        "deny": "The policy denial requires a customer response.",
        "request_info": "The customer must be asked for the missing information.",
        "manual_review": "The refund policy requires human review.",
    }[decision]
