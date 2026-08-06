def route_after_triage(state) -> str:
    governance_result = state.get("triage_governance_result") or state.get(
        "governance_result", {}
    )

    if governance_result.get("status") == "block":
        return "human_approval"

    if state.get("user_action_required"):
        return "response_agent"

    if state.get("triage_output"):
        return "policy"

    return "response_agent"