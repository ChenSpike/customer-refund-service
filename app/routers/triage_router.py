def route_after_triage(state) -> str:
    governance_result = state.get("governance_result", {})

    if governance_result.get("status") == "block":
        return "human_approval"

    if state.get("awaiting_order_id"):
        return "response_agent"

    if state.get("triage_output"):
        return "policy"

    return "response_agent"