import json

from tools.azure_client import client
from tools.llm_helpers import extract_text, usage_tokens

from .models import PolicyDecision
from .prompts import SYSTEM_MSG, VALID_CONFIDENCE, VALID_DECISIONS


def _normalize_decision(raw: dict) -> PolicyDecision:
    decision = raw.get("decision", "manual_review")
    if decision not in VALID_DECISIONS:
        decision = "manual_review"

    refund_amount = raw.get("refund_amount", 0)
    try:
        refund_amount = float(refund_amount)
    except (TypeError, ValueError):
        refund_amount = 0.0

    if decision in {"deny", "request_info", "manual_review"}:
        refund_amount = 0.0
    elif refund_amount <= 0 and decision in {"approve", "partial_refund"}:
        refund_amount = 0.0

    confidence = raw.get("confidence", "low")
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    reason = str(raw.get("reason", "")).strip() or "No reason provided."

    return {
        "decision": decision,
        "refund_amount": refund_amount,
        "reason": reason,
        "confidence": confidence,
    }


def policy_node(state) -> dict:
    triage_output = state["triage_output"]

    response = client.responses.create(
        model="gpt-5.4",
        input=[
            SYSTEM_MSG,
            {
                "role": "user",
                "content": json.dumps(triage_output, ensure_ascii=False),
            },
        ],
        text={"format": {"type": "json_object"}},
    )

    used_in, used_out = usage_tokens(response)

    try:
        raw = json.loads(extract_text(response))
    except json.JSONDecodeError:
        raw = {
            "decision": "manual_review",
            "refund_amount": 0,
            "reason": "Policy model returned invalid JSON.",
            "confidence": "low",
        }

    policy_decision = _normalize_decision(raw)

    return {
        "trace_id": state.get("trace_id"),
        "ticket_id": state.get("ticket_id"),
        "policy_decision": policy_decision,
        "llm_input_tokens": used_in,
        "llm_output_tokens": used_out,
    }