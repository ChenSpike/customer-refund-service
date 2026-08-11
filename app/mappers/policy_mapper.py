from agents.policy.routing import parent_agent_for_route, route_policy


def determine_policy_handoff(state) -> str:
    governance = state.get("policy_governance_result") or {}
    decision = state.get("policy_decision") or {}
    return route_policy(
        decision.get("decision", "manual_review"),
        governance.get("status", "block"),
    )


def map_policy_handoff_to_parent_node(state) -> str:
    handoff = state["policy_handoff"]
    expected_agent = parent_agent_for_route(handoff)
    persistence = state.get("policy_persistence_result")
    if not isinstance(persistence, dict):
        raise ValueError("policy_persistence_result is required before Policy routing")
    if persistence.get("next_agent") != expected_agent:
        raise ValueError("persisted Policy route disagrees with policy_handoff")
    return expected_agent
