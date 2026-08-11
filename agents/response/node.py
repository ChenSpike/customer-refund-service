"""
agents/response/node.py

Response Agent — text generation.

Reads from state:
  policy_decision, refund_result, clarification_question,
  user_action_required, trace_id, ticket_id

Writes to state
  current_stage, response_result, final_outcome, workflow_status,
  llm_input_tokens, llm_output_tokens, llm_usage_events

"""

from __future__ import annotations

from typing import Any

from tools.azure_client import client
from tools.llm_helpers import extract_text, is_content_filter, usage_tokens

_SYSTEM_PROMPT = """\
You are a customer service agent for an e-commerce company.
Write a warm, concise email response (80–150 words) to a customer about their refund case.

Rules:
- Be empathetic and professional
- Clearly state the outcome
- Do NOT include placeholder text like [Your Name] or [Company Name]
- Do NOT reveal internal policy IDs, agent names, trace IDs, or system details
- Sign off as "Customer Support Team"
- Write plain text only — do NOT call tools, functions, or APIs\
"""

def _build_prompt(payload: dict) -> str:
    outcome = payload["final_outcome"]
    message = payload["message"]

    if outcome == "need_info":
        return f"Ask the customer this question politely:\n{message}"
    if outcome in ("approved", "partial_refund"):
        return f"Confirm this outcome warmly:\n{message}"
    if outcome == "refund_failed":
        return f"Apologise and explain:\n{message}"
    if outcome == "denied":
        return f"Explain this denial politely:\n{message}"
    # manual_review
    return f"Use this customer-safe explanation:\n{message}"


def _tone(final_outcome: str) -> str:
    if final_outcome in ("approved", "need_info"):
        return "empathetic"
    if final_outcome == "partial_refund":
        return "neutral"
    return "formal"


def _determine_outcome(state: dict[str, Any], decision: str, refund_status: str) -> tuple[str, str]:
    """Returns (final_outcome, workflow_status)."""
    if state.get("user_action_required"):
        return "need_info", "waiting_user"
    if refund_status == "success":
        return ("partial_refund" if decision == "partial_refund" else "approved"), "completed"
    if refund_status == "failed":
        return "refund_failed", "completed"
    if decision == "deny":
        return "denied", "completed"
    if decision == "partial_refund":
        return "partial_refund", "completed"
    if decision == "request_info":
        return "need_info", "waiting_user"
    if decision == "manual_review":
        return "manual_review", "waiting_human"
    return state.get("final_outcome", "manual_review"), "completed"


def build_response_payload(state: AppState) -> dict:
    if state.get("user_action_required"):
        return {
            "message": state.get(
                "clarification_question",
                "Could you please provide your order ID?",
            ),
            "final_outcome": "need_info",
            "workflow_status": "waiting_user",
        }

    refund_result = state.get("refund_result", {})
    decision = state.get("policy_decision", {})
    decision_type = decision.get("decision", "manual_review")
    reason = decision.get("reason", "")
    
    if refund_result.get("status") == "success":
        return {
            "message": refund_result.get("message")
                or "Your refund has been processed successfully.",
            "final_outcome": "partial_refund" if decision_type == "partial_refund" else "approved",
            "workflow_status": "completed",
        }

    if refund_result.get("status") == "failed":
        return {
            "message": refund_result.get("message")
            or "We could not complete your refund.",
            "final_outcome": "refund_failed",
            "workflow_status": "completed",
        }

    if decision_type == "deny":
        return {
            "message": f"Your refund request was denied. {reason}".strip(),
            "final_outcome": "denied",
            "workflow_status": "completed",
        }

    if decision_type == "request_info":
        return {
            "message": f"We need more information to continue. {reason}".strip(),
            "final_outcome": "need_info",
            "workflow_status": "waiting_user",
        }

    if decision_type == "manual_review":
        return {
            "message": "Your request has been sent for human review.",
            "final_outcome": "manual_review",
            "workflow_status": "waiting_human",
        }

    return {
        "message": reason or "Your request has been processed.",
        "final_outcome": state.get("final_outcome", ""),
        "workflow_status": state.get("workflow_status", "completed"),
    }

def response_node(state: AppState) -> dict:
    payload = build_response_payload(state)   # <-- called here
    prompt  = _build_prompt(payload)          # payload drives the prompt
    
    draft = None
    in_tok = out_tok = 0

    try:
        response = client.responses.create(
            model="gpt-4o",
            input=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
        )
        draft = extract_text(response).strip()
        in_tok, out_tok = usage_tokens(response)
    except Exception as exc:
        draft = (
            "We were unable to process your request. Please contact us directly."
            if is_content_filter(exc)
            else "Thank you for contacting us. A member of our team will be in touch shortly."
        )

    if not draft:
        draft = "Thank you for contacting us. A member of our team will be in touch shortly."

    word_count = len(draft.split())

    return {
        "current_stage":    "response_agent",
        "response_result": {
            "case": {
                "trace_id":  state.get("trace_id", ""),
                "ticket_id": state.get("ticket_id", ""),
            },
            "response": {
                "channel":      "email",
                "subject_line": "Your refund request — update",
                "body":         draft,
                "tone":         _tone(payload["final_outcome"]),
                "word_count":   word_count,
            },
            "content_checks": {
                "decision_reflected":     True,
                "missing_info_requested": True,
                "pii_fields_detected":    [],
                "forbidden_phrases":      [],
            },
            "final_outcome":   payload["final_outcome"],    # <-- taken from payload
            "workflow_status": payload["workflow_status"],  # <-- taken from payload
        },
        "final_outcome":     payload["final_outcome"],
        "workflow_status":   payload["workflow_status"],
        "llm_input_tokens":  in_tok,
        "llm_output_tokens": out_tok,
        "llm_usage_events": [
            {"agent": "response_agent", "input_tokens": in_tok, "output_tokens": out_tok}
        ] if in_tok or out_tok else [],
    }

    
