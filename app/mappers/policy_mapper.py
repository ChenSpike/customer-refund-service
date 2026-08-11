from agents.policy.routing import route_policy


def determine_policy_handoff(state) -> str:
    governance = state.get("policy_governance_result") or {}
    decision = state.get("policy_decision") or {}
    return route_policy(
        decision.get("decision", "manual_review"),
        governance.get("status", "block"),
    )


def map_policy_handoff_to_parent_node(state) -> str:
    handoff = state["policy_handoff"]
    return {
        "refund": "refund_agent",
        "response": "response_agent",
        "human_review": "human_approval",
    }[handoff]