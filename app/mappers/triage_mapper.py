from agents.triage.routing import route_triage


def determine_triage_handoff(state) -> str:
    governance_result = state.get("triage_governance_result") or state.get(
        "governance_result", {}
    )
    return route_triage(
        governance_status=governance_result.get("status", "block"),
        user_action_required=bool(state.get("user_action_required")),
        has_triage_output=bool(state.get("triage_output")),
    )


def map_triage_handoff_to_parent_node(state) -> str:
    handoff = state["triage_handoff"]
    return {
        "policy": "policy",
        "response": "response_agent",
        "human_review": "human_approval",
    }[handoff]