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

import re
from typing import Any

from app.state import AppState
from tools.azure_client import client, deployment_for
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
- Do NOT include placeholder text like [Your Name], [Customer's Name], or [Company Name]
- Address the customer directly without using their name if you don't have it
- Write plain text only — do NOT call tools, functions, or APIs\
"""

_APPROVAL_PATTERNS = (
    # Refund is the grammatical subject: "your refund has been processed" or
    # "the refund will be issued".  Do not use proximity-only matching here;
    # phrases such as "review completed before we can confirm the refund" are
    # not approval claims.
    r"\b(?:your\s+|the\s+|this\s+|an?\s+)?(?:additional\s+|partial\s+|full\s+)?"
    r"refund(?:\s+request)?\s+(?:has|have|is|was|were|will|would)\s+"
    r"(?:(?:already|now)\s+)*(?:(?:be|been)\s+)?"
    r"(?:approved|processed|issued|completed|initiated)\b",
    # Active confirmation: "we have approved your refund".
    r"\b(?:we|i|our\s+team|support\s+team)\s+(?:(?:have|has|had|will)\s+)?"
    r"(?:(?:already|now)\s+)*(?:approved|processed|issued|initiated)\s+"
    r"(?:your\s+|the\s+|this\s+|an?\s+)?(?:additional\s+|partial\s+|full\s+)?refund\b",
    # Concise outcome wording: "approved a partial refund".
    r"\b(?:approved|processed|issued|initiated)\s+"
    r"(?:your\s+|the\s+|this\s+|an?\s+)?(?:additional\s+|partial\s+|full\s+)?refund\b",
)
_NEGATED_APPROVAL_PATTERNS = (
    r"\bno(?:\s+[a-z]+){0,3}\s+refund\b[^.!?]{0,35}"
    r"\b(?:approved|processed|issued|completed|initiated)\b(?:\s+yet)?",
    r"\brefund\b[^.!?]{0,25}\b(?:has|have|is|was|were|will|would|can|could)"
    r"\s+(?:not|never)(?:\s+been)?\s+"
    r"(?:approved|processed|issued|completed|initiated)\b(?:\s+yet)?",
    r"\brefund\b[^.!?]{0,25}\b(?:hasn't|haven't|isn't|wasn't|weren't|won't|"
    r"wouldn't|can't|couldn't)(?:\s+been)?\s+"
    r"(?:approved|processed|issued|completed|initiated)\b(?:\s+yet)?",
    r"\b(?:we|i|our\s+team|support\s+team)\s+"
    r"(?:(?:have|has|had|did|will|would|can|could)\s+)?(?:not|never)\s+"
    r"(?:approved|processed|issued|initiated)\s+"
    r"(?:your\s+|the\s+|this\s+|an?\s+)?(?:additional\s+|partial\s+|full\s+)?refund\b",
    r"\b(?:we|i|our\s+team|support\s+team)\s+"
    r"(?:haven't|hasn't|hadn't|didn't|won't|wouldn't|can't|couldn't)\s+"
    r"(?:approved|processed|issued|initiated)\s+"
    r"(?:your\s+|the\s+|this\s+|an?\s+)?(?:additional\s+|partial\s+|full\s+)?refund\b",
)
_DENIAL_PATTERNS = (
    # The request itself has a final negative disposition.
    r"\b(?:(?:this|your|the)\s+)?(?:refund\s+)?request\s+"
    r"(?:is|was|has\s+been|remains)\s+(?:denied|declined|ineligible)\b",
    r"\b(?:refund|policy)\s+(?:outcome|decision)\s+"
    r"(?:is|was|remains)\s+(?:denial|denied|declined)\b",
    # Explicitly unable to perform the refund action. Keep the object adjacent
    # to the action so "cannot approve or deny the case" and "cannot provide a
    # final refund decision yet" are correctly treated as non-final.
    r"\b(?:cannot|can't|unable\s+to|could\s+not|couldn't|will\s+not|won't|not\s+able\s+to)\s+"
    r"(?:approve|process|issue|provide|offer)\s+"
    r"(?:(?:an?|the|your|this)\s+)?(?:(?:additional|partial|full)\s+)?refund\b",
    # Refund as subject with a negative disposition.
    r"\b(?:(?:your|the|this|an?)\s+)?(?:(?:additional|partial|full)\s+)?refund\s+"
    r"(?:cannot|can't|will\s+not|won't|is\s+not|was\s+not|has\s+not\s+been)\s+"
    r"(?:be\s+)?(?:approved|processed|issued|provided|completed)\b",
    r"\b(?:not\s+eligible|ineligible)\s+for\s+(?:(?:an?|the|your)\s+)?refund\b",
)
_INFO_REQUEST_PATTERNS = (
    r"\b(?:could|would|can|will) you\b",
    r"\bplease\b.{0,30}\b(?:provide|share|send|confirm|upload|reply)\b",
    r"\bwe need\b.{0,30}\b(?:information|details|documentation|confirmation)\b",
    r"\b(?:provide|share|send|confirm|upload|reply with)\b",
)
_MANUAL_REVIEW_PATTERNS = (
    r"\b(?:human|manual|review team|specialist|team)\b.{0,30}\breview\b",
    r"\b(?:under|pending|sent for|requires|awaiting)\b.{0,25}\breview\b",
    r"\breview\b.{0,25}\b(?:underway|pending|required|team|specialist)\b",
)
_REFUND_FAILURE_PATTERNS = (
    r"\b(?:could not|couldn't|unable to|failed to|not able to)\b.{0,25}\brefund\b",
    r"\brefund\b.{0,25}\b(?:failed|error|problem|issue|not completed)\b",
    r"\bfollow up\b",
)
_CONTENT_STOP_WORDS = {
    "a", "an", "and", "are", "be", "can", "continue", "could", "for",
    "from", "help", "information", "more", "need", "of", "please",
    "provide", "request", "send", "share", "that", "the", "this", "to",
    "us", "we", "with", "would", "you", "your",
}

_OUTCOME_ANCHORS = {
    "approved": "Your refund request has been approved.",
    "partial_refund": "A partial refund has been processed successfully.",
    "denied": "Your refund request has been denied.",
    "need_info": "We need more information before we can complete the refund review.",
    "manual_review": "Your refund request has been sent for human review.",
    "refund_failed": "We could not complete your refund and will follow up.",
}


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _has_positive_approval(text: str) -> bool:
    # Remove only the sentence-local negated refund clauses, then look for a
    # remaining affirmative claim. This handles "no refund has been approved
    # yet" without hiding a separate, genuinely positive approval sentence.
    affirmative_text = text
    for pattern in _NEGATED_APPROVAL_PATTERNS:
        affirmative_text = re.sub(
            pattern,
            "",
            affirmative_text,
            flags=re.IGNORECASE,
        )
    return _contains_any(affirmative_text, _APPROVAL_PATTERNS)


def _normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _outcome_anchor(final_outcome: str) -> str:
    return _OUTCOME_ANCHORS.get(str(final_outcome or ""), "")


def _ensure_outcome_anchor(body: str, payload: dict) -> tuple[str, bool]:
    """Insert the trusted outcome sentence without hiding Azure's prose."""

    text = str(body or "").strip()
    anchor = str(payload.get("outcome_anchor") or "").strip()
    if not anchor or _normalized_text(anchor) in _normalized_text(text):
        return text, False

    lines = text.splitlines()
    greeting_index = next(
        (
            index
            for index, line in enumerate(lines[:5])
            if re.fullmatch(
                r"\s*(?:hello|hi|dear(?:\s+customer)?|good\s+"
                r"(?:morning|afternoon|evening))\s*[,!.]?\s*",
                line,
                flags=re.IGNORECASE,
            )
        ),
        None,
    )
    if greeting_index is None:
        return f"{anchor}\n\n{text}" if text else anchor, True

    suffix = lines[greeting_index + 1:]
    while suffix and not suffix[0].strip():
        suffix.pop(0)
    anchored_lines = [
        *lines[:greeting_index + 1],
        "",
        anchor,
    ]
    if suffix:
        anchored_lines.extend(["", *suffix])
    return "\n".join(anchored_lines), True


def _required_safe_summary(state: AppState, final_outcome: str) -> str:
    decision = state.get("policy_decision") or {}
    guidance = (state.get("policy_context") or {}).get("response_guidance") or {}
    decision_type = str(decision.get("decision") or "")
    compatible_decisions = {
        "approved": {"approve"},
        "partial_refund": {"partial_refund"},
        "denied": {"deny"},
        "need_info": {"request_info"},
        "manual_review": {"manual_review"},
    }
    if decision_type not in compatible_decisions.get(final_outcome, set()):
        # A resolved human review may legitimately supersede the earlier Policy
        # summary. Do not force that stale summary into the final response.
        return ""
    return str(
        guidance.get("customer_safe_summary")
        or decision.get("customer_safe_summary")
        or ""
    ).strip()


def _required_information(state: AppState, final_outcome: str) -> list[str]:
    if final_outcome != "need_info":
        return []
    decision = state.get("policy_decision") or {}
    guidance = (state.get("policy_context") or {}).get("response_guidance") or {}
    requested = (
        guidance.get("missing_info_to_request")
        or decision.get("missing_info_to_request")
        or []
    )
    if isinstance(requested, str):
        requested = [requested]
    items = [str(item).strip() for item in requested if str(item).strip()]
    if items:
        return items
    missing_fields = state.get("missing_fields") or []
    items = [
        str(field).replace("_", " ").strip()
        for field in missing_fields
        if str(field).strip()
    ]
    if items:
        return items
    question = str(state.get("clarification_question") or "").strip()
    return [question] if question else []


def _response_payload(
    state: AppState,
    *,
    message: str,
    final_outcome: str,
    workflow_status: str,
) -> dict:
    payload = {
        "message": message,
        "final_outcome": final_outcome,
        "workflow_status": workflow_status,
    }
    outcome_anchor = _outcome_anchor(final_outcome)
    if outcome_anchor:
        payload["outcome_anchor"] = outcome_anchor
    safe_summary = _required_safe_summary(state, final_outcome)
    if safe_summary:
        payload["required_safe_summary"] = safe_summary
    required_information = _required_information(state, final_outcome)
    if required_information:
        payload["required_information"] = required_information
    return payload

def _build_prompt(payload: dict) -> str:
    outcome = payload["final_outcome"]
    message = payload["message"]

    if outcome == "need_info":
        prompt = f"Ask the customer this question politely:\n{message}"
    elif outcome in ("approved", "partial_refund"):
        prompt = f"Confirm this outcome warmly:\n{message}"
    elif outcome == "refund_failed":
        prompt = f"Apologise and explain:\n{message}"
    elif outcome == "denied":
        prompt = f"Explain this denial politely:\n{message}"
    else:  # manual_review
        prompt = f"Use this customer-safe explanation:\n{message}"

    outcome_anchor = str(payload.get("outcome_anchor") or "").strip()
    if outcome_anchor:
        prompt += (
            "\nInclude this trusted outcome sentence verbatim:\n"
            f"{outcome_anchor}"
        )
    safe_summary = str(payload.get("required_safe_summary") or "").strip()
    if safe_summary:
        prompt += (
            "\nInclude this customer-safe summary sentence unchanged (punctuation may vary):\n"
            f"{safe_summary}"
        )
    required_information = payload.get("required_information") or []
    if required_information:
        prompt += "\nExplicitly ask for every item below:\n- " + "\n- ".join(required_information)
    return prompt


def validate_response_semantics(body: str, payload: dict) -> dict[str, Any]:
    """Deterministically prove the draft agrees with its customer-safe outcome."""

    text = str(body or "").strip()
    normalized = _normalized_text(text)
    normalized_terms = set(normalized.split())
    outcome = str(payload.get("final_outcome") or "")
    has_approval = _has_positive_approval(text)
    has_denial = _contains_any(text, _DENIAL_PATTERNS)
    has_info_request = _contains_any(text, _INFO_REQUEST_PATTERNS)
    has_manual_review = _contains_any(text, _MANUAL_REVIEW_PATTERNS)
    has_refund_failure = _contains_any(text, _REFUND_FAILURE_PATTERNS)

    if outcome == "approved":
        decision_reflected = has_approval and not (has_denial or has_refund_failure)
    elif outcome == "partial_refund":
        decision_reflected = (
            has_approval
            and bool(re.search(r"\bpartial\b", text, flags=re.IGNORECASE))
            and not (has_denial or has_refund_failure)
        )
    elif outcome == "denied":
        decision_reflected = has_denial and not has_approval
    elif outcome == "need_info":
        decision_reflected = has_info_request and not (has_approval or has_denial)
    elif outcome == "manual_review":
        decision_reflected = has_manual_review and not (has_approval or has_denial)
    elif outcome == "refund_failed":
        decision_reflected = has_refund_failure and not has_approval
    else:
        decision_reflected = False

    required_information = payload.get("required_information") or []
    missing_info_requested = outcome != "need_info" or has_info_request
    if outcome == "need_info" and required_information:
        for item in required_information:
            terms = [
                term
                for term in _normalized_text(str(item)).split()
                if term not in _CONTENT_STOP_WORDS
            ]
            if terms:
                required_matches = min(2, len(set(terms)))
                if len(set(terms).intersection(normalized_terms)) < required_matches:
                    missing_info_requested = False
                    break

    safe_summary = str(payload.get("required_safe_summary") or "").strip()
    safe_summary_reflected = (
        not safe_summary or _normalized_text(safe_summary) in normalized
    )
    outcome_anchor = str(payload.get("outcome_anchor") or "").strip()
    outcome_anchor_reflected = (
        not outcome_anchor or _normalized_text(outcome_anchor) in normalized
    )

    errors: list[str] = []
    if not decision_reflected:
        errors.append(f"draft does not unambiguously reflect outcome '{outcome or 'unknown'}'")
    if not missing_info_requested:
        errors.append("draft does not request all required information")
    if not safe_summary_reflected:
        errors.append("draft omits the required customer-safe summary")
    if not outcome_anchor_reflected:
        errors.append("draft omits the trusted outcome anchor")
    return {
        "decision_reflected": decision_reflected,
        "missing_info_requested": missing_info_requested,
        "safe_summary_reflected": safe_summary_reflected,
        "outcome_anchor_reflected": outcome_anchor_reflected,
        "semantic_errors": errors,
    }


def _tone(final_outcome: str) -> str:
    if final_outcome in ("approved", "need_info"):
        return "empathetic"
    if final_outcome == "partial_refund":
        return "neutral"
    return "formal"


def build_response_payload(state: AppState) -> dict:
    if state.get("user_action_required"):
        return _response_payload(
            state,
            message=state.get(
                "clarification_question",
                "Could you please provide your order ID?",
            ),
            final_outcome="need_info",
            workflow_status="waiting_user",
        )

    human_review = state.get("human_review") or {}
    if human_review.get("status") == "approved":
        approved_next_agent = human_review.get("approved_next_agent")
        if approved_next_agent == "refund_agent":
            return _response_payload(
                state,
                message="Your request was approved by our review team and your refund is now being processed.",
                final_outcome="approved",
                workflow_status="completed",
            )
        return _response_payload(
            state,
            message="Our review team has completed the review of your request.",
            final_outcome=state.get("final_outcome", "approved") or "approved",
            workflow_status="completed",
        )

    if human_review.get("status") == "rejected":
        return _response_payload(
            state,
            message="Our review team has completed the review of your request and we are unable to approve the refund.",
            final_outcome="denied",
            workflow_status="completed",
        )

    refund_result = state.get("refund_result", {})
    decision = state.get("policy_decision", {})
    decision_type = decision.get("decision", "manual_review")
    reason = decision.get("reason", "")
    
    if refund_result.get("status") == "success":
        return _response_payload(
            state,
            message=(
                refund_result.get("message")
                or "Your refund has been processed successfully."
            ),
            final_outcome=(
                "partial_refund" if decision_type == "partial_refund" else "approved"
            ),
            workflow_status="completed",
        )

    if refund_result.get("status") == "failed":
        return _response_payload(
            state,
            message=refund_result.get("message") or "We could not complete your refund.",
            final_outcome="refund_failed",
            workflow_status="completed",
        )

    if decision_type == "deny":
        return _response_payload(
            state,
            message=f"Your refund request was denied. {reason}".strip(),
            final_outcome="denied",
            workflow_status="completed",
        )

    if decision_type == "request_info":
        return _response_payload(
            state,
            message=f"We need more information to continue. {reason}".strip(),
            final_outcome="need_info",
            workflow_status="waiting_user",
        )

    if decision_type == "manual_review":
        return _response_payload(
            state,
            message="Your request has been sent for human review.",
            final_outcome="manual_review",
            workflow_status="waiting_human",
        )

    return _response_payload(
        state,
        message=reason or "Your request has been processed.",
        final_outcome=state.get("final_outcome", ""),
        workflow_status=state.get("workflow_status", "completed"),
    )

def response_node(
    state: AppState,
    *,
    responses_client=None,
    model: str | None = None,
) -> dict:
    active_client = responses_client or client
    active_model = model or deployment_for("response")
    payload = build_response_payload(state)   # <-- called here
    prompt  = _build_prompt(payload)          # payload drives the prompt
    
    draft = None
    in_tok = out_tok = 0

    try:
        response = active_client.responses.create(
            model=active_model,
            input=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
        )
        draft = extract_text(response).strip()
        in_tok, out_tok = usage_tokens(response)
    except Exception as exc:
        category = "content filter" if is_content_filter(exc) else "request"
        # A generated response is part of the audited workflow outcome.  Do not
        # silently persist a generic message as though Azure succeeded.
        raise RuntimeError(f"Azure Response Agent {category} failed") from exc

    if not draft:
        draft = "Thank you for contacting us. A member of our team will be in touch shortly."

    draft, outcome_anchor_inserted = _ensure_outcome_anchor(draft, payload)
    word_count = len(draft.split())
    semantic_checks = validate_response_semantics(draft, payload)
    semantic_checks["outcome_anchor_inserted"] = outcome_anchor_inserted

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
                **semantic_checks,
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


class ResponseNode:
    def __init__(self, *, responses_client, model: str) -> None:
        self.responses_client = responses_client
        self.model = model

    def __call__(self, state: AppState) -> dict:
        return response_node(
            state,
            responses_client=self.responses_client,
            model=self.model,
        )

    
