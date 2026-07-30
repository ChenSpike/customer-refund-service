import json
import uuid

from openai import BadRequestError

from tools.azure_client import client
from tools.llm_helpers import extract_text, is_content_filter, usage_tokens
from tools.order_lookup import ORDER_LOOKUP_TOOL, order_database_lookup

from .helpers import (
    assistant_msg,
    inputs_from_state,
    light_clean,
    parse_requested_amount,
)
from .prompts import SYSTEM_MSG, VALID_REASONS


def triage_node(state) -> dict:
    message, user_id, buggy = inputs_from_state(state)

    trace_id = state.get("trace_id") or str(uuid.uuid4())
    ticket_id = state.get("ticket_id") or str(uuid.uuid4())
    ids = {
        "trace_id": trace_id,
        "ticket_id": ticket_id,
    }

    history = list(state.get("conversation_history") or [])
    user_msg = {"role": "user", "content": message}

    try:
        first = client.responses.create(
            model="gpt-5.4",
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
            "awaiting_order_id": False,
            "content_filter_blocked": True,
            "injection_flag": True,
            "governance_result": {
                "status": "block",
                "rule": "ASI07",
                "failed_check": "content_filter",
                "detail": "Azure content filter rejected the message.",
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
            "awaiting_order_id": True,
            "clarification_question": question,
            "conversation_history": history + [user_msg, assistant_msg(question)],
        }

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
            "awaiting_order_id": True,
            "clarification_question": question,
            "conversation_history": history + [user_msg, assistant_msg(question)],
        }

    tool_result = {
        "type": "function_call_output",
        "call_id": tool_call.call_id,
        "output": json.dumps(raw_result),
    }

    second = client.responses.create(
        model="gpt-5.4",
        input=[SYSTEM_MSG, *history, user_msg, *first.output, tool_result],
        text={"format": {"type": "json_object"}},
    )

    second_input_tokens, second_output_tokens = usage_tokens(second)
    total_input_tokens = first_input_tokens + second_input_tokens
    total_output_tokens = first_output_tokens + second_output_tokens

    classification = json.loads(extract_text(second))
    raw_reason = classification.get("refund_reason", "doesnt_like_it")
    reason = raw_reason if raw_reason in VALID_REASONS else "doesnt_like_it"
    requested_amount = parse_requested_amount(
        classification.get("requested_amount"),
        raw_result["amount_paid"],
    )

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
        "llm_input_tokens": total_input_tokens,
        "llm_output_tokens": total_output_tokens,
        "awaiting_order_id": False,
        "order_lookup_result": raw_result,
        "triage_output": triage_output,
        "conversation_history": history + [user_msg, assistant_msg(extract_text(second))],
    }