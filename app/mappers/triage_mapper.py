from agents.triage.routing import route_triage


def determine_triage_handoff(state) -> str:
    governance_result = state.get("triage_governance_result") or {}
    return route_triage(
        governance_status=governance_result.get("status", "block"),
        user_action_required=bool(state.get("user_action_required")),
        has_triage_output=bool(state.get("triage_output")),
    )


def map_triage_handoff_to_parent_node(state) -> str:
    persistence = state.get("triage_persistence_result") or {}
    handoff = persistence.get("next_agent")
    if handoff is None:
        raise ValueError("triage_persistence_result is required before Triage routing")
    return {
        "policy": "policy",
        "response_agent": "response_agent",
        "human_approval": "human_approval",
    }[handoff]