from __future__ import annotations

from typing import Literal


DecisionName = Literal["approve", "deny", "partial_refund", "request_info", "manual_review"]
GovernanceStatus = Literal["allow", "block"]
RouteName = Literal["refund", "response", "human_review"]
ParentAgent = Literal["refund_agent", "response_agent", "human_approval"]


_PARENT_AGENT_BY_ROUTE: dict[RouteName, ParentAgent] = {
    "refund": "refund_agent",
    "response": "response_agent",
    "human_review": "human_approval",
}


def route_policy(decision: DecisionName, governance_status: GovernanceStatus) -> RouteName:
    """Resolve the parent-graph route without changing business data."""

    if governance_status == "block":
        return "human_review"
    return {
        "approve": "refund",
        "partial_refund": "refund",
        "deny": "response",
        "request_info": "response",
        "manual_review": "human_review",
    }[decision]


def parent_agent_for_route(route: RouteName) -> ParentAgent:
    """Map the Policy subgraph handoff to its parent-graph node."""

    return _PARENT_AGENT_BY_ROUTE[route]


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
