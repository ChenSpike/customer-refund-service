def route_triage(
    *,
    governance_status: str,
    user_action_required: bool,
    has_triage_output: bool,
) -> str:
    if governance_status == "block":
        return "human_review"

    if user_action_required:
        return "response"

    if has_triage_output:
        return "policy"

    return "response"