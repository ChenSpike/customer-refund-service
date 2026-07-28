def route_after_policy(state) -> str:
    governance_result = state.get("governance_result", {})

    if governance_result.get("status") == "block":
        return "human_approval"

    decision = state.get("policy_decision", {}).get("decision")

    if decision in {"approve", "partial_refund"}:
        return "refund_agent"
    if decision == "deny":
        return "response_agent"
    if decision == "request_info":
        return "response_agent"
    return "human_approval"