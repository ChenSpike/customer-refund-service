from agents.policy.routing import route_policy


def route_after_policy(state) -> str:
    governance = state.get("policy_governance_result") or {}
    decision = state.get("policy_decision") or {}
    return route_policy(
        decision.get("decision", "manual_review"),
        governance.get("status", "block"),
    )