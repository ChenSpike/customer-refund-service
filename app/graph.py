from langgraph.graph import END, START, StateGraph

from app.state import AppState
from app.routers.policy_router import route_after_policy
from app.routers.refund_router import route_after_refund
from app.routers.triage_router import route_after_triage
from agents.policy.state_adapter import build_policy_state_nodes
from agents.refund.node import refund_node
from agents.triage.node import triage_node
from agents.triage.governance_node import GovernanceNode as TriageGovernanceNode


def response_node(state: AppState) -> dict:
    if state.get("user_action_required"):
        message = state.get(
            "clarification_question",
            "Could you please provide your order ID?",
        )
        final_outcome = "need_info"
        workflow_status = "waiting_user"
    else:
        refund_result = state.get("refund_result", {})
        decision = state.get("policy_decision", {})
        decision_type = decision.get("decision", "manual_review")
        reason = decision.get("reason", "")

        if refund_result.get("status") == "success":
            message = refund_result.get("message") or "Your refund has been processed successfully."
            final_outcome = state.get("final_outcome") or decision_type or "approved"
            workflow_status = "completed"
        elif refund_result.get("status") == "failed":
            message = refund_result.get("message") or "We could not complete your refund."
            final_outcome = "refund_failed"
            workflow_status = "completed"
        elif decision_type == "deny":
            message = f"Your refund request was denied. {reason}".strip()
            final_outcome = "denied"
            workflow_status = "completed"
        elif decision_type == "request_info":
            message = f"We need more information to continue. {reason}".strip()
            final_outcome = "need_info"
            workflow_status = "waiting_user"
        elif decision_type == "manual_review":
            message = "Your request has been sent for human review."
            final_outcome = "manual_review"
            workflow_status = "waiting_human"
        else:
            message = reason or "Your request has been processed."
            final_outcome = state.get("final_outcome", "")
            workflow_status = state.get("workflow_status", "completed")

    return {
        "current_stage": "response_agent",
        "response_result": {
            "status": "ready",
            "message": message,
        }
        ,
        "final_outcome": final_outcome,
        "workflow_status": workflow_status,
    }


def human_approval_node(state: AppState) -> dict:
    governance_result = state.get("policy_governance_result") or state.get(
        "triage_governance_result"
    ) or state.get("governance_result", {})
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
    policy_nodes = build_policy_state_nodes()

    triage_governance = TriageGovernanceNode()

    builder.add_node("triage", triage_node)
    builder.add_node("triage_governance", triage_governance)
    builder.add_node("policy", policy_nodes.policy_reasoning)
    builder.add_node("policy_governance", policy_nodes.policy_governance)
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

    builder.add_conditional_edges(
        "refund_agent",
        route_after_refund,
        {
            "response_agent": "response_agent",
        },
    )
    builder.add_edge("response_agent", END)
    builder.add_edge("human_approval", END)

    return builder.compile()