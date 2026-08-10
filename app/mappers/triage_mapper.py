def resolve_triage_handoff(state) -> str:
    governance_result = state.get("triage_governance_result") or state.get(
        "governance_result", {}
    )

    if governance_result.get("status") == "block":
        return "human_review"

    if state.get("user_action_required"):
        return "response"

    if state.get("triage_output"):
        return "policy"

    return "response"


def map_triage_handoff_to_parent_node(state) -> str:
    handoff = state["triage_handoff"]
    return {
        "policy": "policy",
        "response": "response_agent",
        "human_review": "human_approval",
    }[handoff]