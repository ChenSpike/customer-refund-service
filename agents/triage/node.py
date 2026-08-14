import json
import re
import uuid

from openai import BadRequestError

from tools.azure_client import client, deployment_for
from tools.llm_helpers import extract_text, is_content_filter, usage_tokens
from tools.order_lookup import ORDER_LOOKUP_TOOL, order_database_lookup

from .helpers import (
    assistant_msg,
    inputs_from_state,
    light_clean,
    parse_requested_amount,
)
from .prompts import SYSTEM_MSG, VALID_REASONS


_CLASSIFICATION_ORDER_FIELDS = (
    "order_id",
    "product_type",
    "purchase_date",
    "item_status",
    "amount_paid",
    "prior_refund_total",
)

_REFUND_INTENT_RE = re.compile(
    r"\b(?:refund|money back|return (?:it|this|the item))\b",
    re.IGNORECASE,
)
_DISSATISFACTION_PATTERNS = (
    re.compile(r"\buncomfortable\b", re.IGNORECASE),
    re.compile(r"\bheadaches?\b", re.IGNORECASE),
    re.compile(r"\bclamp\s+pressure\b", re.IGNORECASE),
    re.compile(
        r"\b(?:open(?:ed)?|did\s+open)\b.{0,24}\b(?:use(?:d)?|did\s+use|tried)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bchanged?\s+(?:my|our)\s+mind\b", re.IGNORECASE),
    re.compile(r"\b(?:do not|don't|does not|doesn't)\s+like\b", re.IGNORECASE),
)


def _classification_order_facts(raw_result: dict) -> dict:
    """Return the non-PII subset that the classification model may inspect.

    Ownership and contact columns are needed by the local deterministic ASI07
    check, but they are not classification inputs.  Keeping this allowlist next
    to the Azure call makes a future database-column addition fail closed: new
    contact fields cannot silently enter the model context.
    """

    return {
        field: raw_result[field]
        for field in _CLASSIFICATION_ORDER_FIELDS
        if field in raw_result
    }


def _requester_owns_order(user_id: str | None, raw_result: dict) -> bool:
    requester = str(user_id or "").strip()
    owner = str(raw_result.get("order_customer_id") or "").strip()
    return bool(requester and owner and requester == owner)


def _dissatisfaction_reason_fallback(message: str) -> str | None:
    """Map only explicit refund dissatisfaction language to ``doesnt_like_it``.

    Generic statements such as "something is wrong" remain structurally
    missing so Policy can request information (the demo10/demo14 boundary).
    """

    text = re.sub(r"[-_/]+", " ", str(message or ""))
    if not _REFUND_INTENT_RE.search(text):
        return None
    if any(pattern.search(text) for pattern in _DISSATISFACTION_PATTERNS):
        return "doesnt_like_it"
    return None


def triage_node(state, *, responses_client=None, model: str | None = None) -> dict:
    active_client = responses_client or client
    active_model = model or deployment_for("triage")
    message, user_id, buggy = inputs_from_state(state)
    request_context = state.get("request_context") or {}
    selected_order_id = str(
        state.get("requested_order_id")
        or request_context.get("selected_order_id")
        or ""
    ).strip()

    trace_id = state.get("trace_id") or str(uuid.uuid4())
    ticket_id = state.get("ticket_id") or str(uuid.uuid4())
    ids = {
        "trace_id": trace_id,
        "ticket_id": ticket_id,
    }

    history = list(state.get("conversation_history") or [])
    user_msg = {"role": "user", "content": message}

    first_input_tokens = first_output_tokens = 0
    tool_call = None
    first_output: list = []
    if not selected_order_id:
        try:
            first = active_client.responses.create(
                model=active_model,
                input=[SYSTEM_MSG, *history, user_msg],
                tools=[ORDER_LOOKUP_TOOL],
            )
        except BadRequestError as exc:
            if not is_content_filter(exc):
                raise

            return {
                **ids,
                "user_id": user_id,
                "llm_input_tokens": 0,
                "llm_output_tokens": 0,
                "user_action_required": False,
                "content_filter_result": {
                    "status": "block",
                    "reason": "Azure content filter rejected the message.",
                },
                "conversation_history": history + [user_msg],
            }

        first_input_tokens, first_output_tokens = usage_tokens(first)
        tool_call = next(
            (item for item in first.output if item.type == "function_call"),
            None,
        )
        if tool_call is None:
            question = extract_text(first)
            return {
                **ids,
                "user_id": user_id,
                "llm_input_tokens": first_input_tokens,
                "llm_output_tokens": first_output_tokens,
                "user_action_required": True,
                "missing_fields": ["order_id"],
                "clarification_question": question,
                "conversation_history": history + [user_msg, assistant_msg(question)],
            }
        first_output = list(first.output)

    if selected_order_id:
        # The dashboard/refund UI selection is trusted workflow context.  It
        # deliberately overrides an omitted or mistyped order reference in the
        # free-text message while leaving that original text available to the
        # governance checks (demo18 depends on this distinction).
        order_id = selected_order_id
    else:
        args = json.loads(tool_call.arguments)
        order_id = args["order_id"]
    raw_result = order_database_lookup(order_id, buggy=buggy)

    if raw_result is None:
        question = f"I couldn't find order {order_id}. Could you double-check the order ID?"
        return {
            **ids,
            "user_id": user_id,
            "llm_input_tokens": first_input_tokens,
            "llm_output_tokens": first_output_tokens,
            "user_action_required": True,
            "missing_fields": ["order_id"],
            "clarification_question": question,
            "conversation_history": history + [user_msg, assistant_msg(question)],
        }

    # Authorize locally before putting any database result in a model request.
    # The raw row remains in state so the deterministic ASI07 governance node
    # can record the ownership failure and route the case to human review.  No
    # classification call is made for an unauthenticated/cross-customer lookup.
    if not _requester_owns_order(user_id, raw_result):
        return {
            **ids,
            "user_id": user_id,
            "requested_order_id": order_id,
            "llm_input_tokens": first_input_tokens,
            "llm_output_tokens": first_output_tokens,
            "order_lookup_result": raw_result,
            "conversation_history": history + [user_msg],
        }

    call_id = tool_call.call_id if tool_call is not None else f"selected-{trace_id}"
    tool_result = {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(_classification_order_facts(raw_result)),
    }

    if tool_call is None:
        # A verified UI selection makes a separate LLM order-extraction call
        # redundant. Responses accepts this synthetic call as prior input, so
        # the classification request retains the normal tool-result shape.
        first_output = [{
            "type": "function_call",
            "name": "Order_Database_Lookup",
            "arguments": json.dumps({"order_id": order_id}),
            "call_id": call_id,
        }]

    second = active_client.responses.create(
        model=active_model,
        input=[SYSTEM_MSG, *history, user_msg, *first_output, tool_result],
        text={"format": {"type": "json_object"}},
    )

    second_input_tokens, second_output_tokens = usage_tokens(second)
    total_input_tokens = first_input_tokens + second_input_tokens
    total_output_tokens = first_output_tokens + second_output_tokens

    classification = json.loads(extract_text(second))
    raw_reason = classification.get("refund_reason")
    reason = raw_reason if raw_reason in VALID_REASONS else None
    if reason is None:
        reason = _dissatisfaction_reason_fallback(message)
    requested_amount = parse_requested_amount(classification.get("requested_amount"))
    if requested_amount is None and reason is not None:
        # In this refund-service workflow, a recognized reason conventionally
        # means the full order amount when the customer does not quote a number.
        # Do not infer an amount when the reason itself is missing: demo10 and
        # demo14 must retain structurally missing facts for request_info.
        requested_amount = float(raw_result["amount_paid"])

    triage_output = {
        "case": {
            "trace_id": trace_id,
            "ticket_id": ticket_id,
            "goal": "evaluate refund eligibility",
            "policy_version": "v1.0",
        },
        "customer_request": {
            "sanitized_text": light_clean(message),
            "refund_reason": reason,
            "requested_amount": requested_amount,
            "currency": "USD",
        },
        "order_facts": {
            "order_id": raw_result["order_id"],
            "product_type": raw_result["product_type"],
            "purchase_date": raw_result["purchase_date"],
            "item_status": raw_result["item_status"],
            "amount_paid": raw_result["amount_paid"],
            "prior_refund_total": raw_result["prior_refund_total"],
        },
    }

    return {
        **ids,
        "user_id": user_id,
        "requested_order_id": order_id,
        "llm_input_tokens": total_input_tokens,
        "llm_output_tokens": total_output_tokens,
        "order_lookup_result": raw_result,
        "triage_output": triage_output,
        "conversation_history": history + [user_msg, assistant_msg(extract_text(second))],
    }


class TriageNode:
    def __init__(self, *, responses_client, model: str) -> None:
        self.responses_client = responses_client
        self.model = model

    def __call__(self, state) -> dict:
        return triage_node(
            state,
            responses_client=self.responses_client,
            model=self.model,
        )
