from datetime import datetime, timezone


def log_governance_event(
    trace_id: str,
    ticket_id: str | None,
    user_id: str | None,
    result: dict,
    stage: str,
) -> str:
    event_id = f"{trace_id}:governance:{stage}"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_id": event_id,
        "trace_id": trace_id,
        "ticket_id": ticket_id,
        "user_id": user_id,
        "stage": stage,
        "verdict": result.get("status"),
        "findings": result.get("findings", []),
        "all_checks": result.get("all_checks", []),
    }
    print("[GOVERNANCE]", payload)
    return event_id
