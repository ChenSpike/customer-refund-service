from langgraph.graph import END, START, StateGraph

from app.state import AppState
from app.routers.policy_router import route_after_policy
from app.routers.triage_router import route_after_triage
from agents.policy.node import policy_node
from agents.triage.node import triage_node
from governance.checkers import (
    check_pii_risk,
    check_semantic_drift,
    check_tool_misuse,
)
from governance.node import GovernanceNode


def refund_node(state: AppState) -> dict:
    policy_decision = state.get("policy_decision", {})
    return {
        "refund_result": {
            "status": "prepared",
            "refund_amount": policy_decision.get("refund_amount", 0),
            "message": "Refund agent placeholder executed.",
        }
    }


def response_node(state: AppState) -> dict:
    if state.get("awaiting_order_id"):
        message = state.get(
            "clarification_question",
            "Could you please provide your order ID?",
        )
    else:
        decision = state.get("policy_decision", {})
        decision_type = decision.get("decision", "manual_review")
        reason = decision.get("reason", "")

        if decision_type == "deny":
            message = f"Your refund request was denied. {reason}".strip()
        elif decision_type == "request_info":
            message = f"We need more information to continue. {reason}".strip()
        elif decision_type == "manual_review":
            message = "Your request has been sent for human review."
        else:
            message = reason or "Your request has been processed."

    return {
        "response_result": {
            "status": "ready",
            "message": message,
        }
    }


def human_approval_node(state: AppState) -> dict:
    governance_result = state.get("governance_result", {})
    policy_decision = state.get("policy_decision", {})

    reason = "manual_review"
    if governance_result.get("status") == "block":
        reason = "governance_block"
    elif policy_decision.get("decision") == "manual_review":
        reason = "policy_manual_review"

    return {
        "human_review": {
            "status": "pending",
            "reason": reason,
        }
    }


def build_graph():
    builder = StateGraph(AppState)

    triage_governance = GovernanceNode(
        name="triage",
        checkers=[check_pii_risk, check_semantic_drift],
    )

    policy_governance = GovernanceNode(
        name="policy",
        checkers=[check_pii_risk, check_semantic_drift, check_tool_misuse],
    )

    builder.add_node("triage", triage_node)
    builder.add_node("triage_governance", triage_governance)
    builder.add_node("policy", policy_node)
    builder.add_node("policy_governance", policy_governance)
    builder.add_node("refund_agent", refund_node)
    builder.add_node("response_agent", response_node)
    builder.add_node("human_approval", human_approval_node)

    builder.add_edge(START, "triage")
    builder.add_edge("triage", "triage_governance")

    builder.add_conditional_edges(
        "triage_governance",
        route_after_triage,
        {
            "policy": "policy",
            "response_agent": "response_agent",
            "human_approval": "human_approval",
        },
    )

    builder.add_edge("policy", "policy_governance")

    builder.add_conditional_edges(
        "policy_governance",
        route_after_policy,
        {
            "refund_agent": "refund_agent",
            "response_agent": "response_agent",
            "human_approval": "human_approval",
        },
    )

    builder.add_edge("refund_agent", END)
    builder.add_edge("response_agent", END)
    builder.add_edge("human_approval", END)

    return builder.compile()