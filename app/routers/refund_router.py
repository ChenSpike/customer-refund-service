def route_after_refund(state) -> str:
    refund_result = state.get("refund_result", {})

    if refund_result.get("status") in {"success", "failed"}:
        return "response_agent"
    return "response_agent"