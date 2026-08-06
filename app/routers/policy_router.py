from agents.policy.state_adapter import route_policy_state


def route_after_policy(state) -> str:
    return route_policy_state(state)