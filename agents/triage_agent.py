import json
import os
import re
import uuid

from openai import AzureOpenAI, BadRequestError

from db import pipeline_store
from db.backend import active_backend
from governance.audit_logger import log_event, log_governance_event
from state import TriageState
from tools.order_lookup import ORDER_LOOKUP_TOOL, order_database_lookup

client = AzureOpenAI(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    azure_endpoint="https://aoai-ucla-prjs.openai.azure.com/",
    api_version="2025-04-01-preview",
)

_SYSTEM_PROMPT = """You are a customer service triage agent for a refund processing system.

Your job each turn:
1. Check if the customer's message contains an order ID (format: ORD-XXX).
2. If NO order ID is present, reply with exactly this sentence and nothing else:
   "Could you please provide your order ID?"
3. If an order ID IS present, call the Order_Database_Lookup tool immediately.
4. After receiving the order data, respond with ONLY a JSON object:
   {
     "refund_reason": "<one of: wrong_item | not_delivered_within_timeframe | damaged | doesnt_like_it>"
   }

Rules:
- ONLY call Order_Database_Lookup. Never call Refund_Issuer or Knowledge_Base_Search.
- Choose refund_reason based on what the customer describes, not the item_status from the DB.
- Do not include any explanation outside the JSON."""

_VALID_REASONS = {
    "wrong_item",
    "not_delivered_within_timeframe",
    "damaged",
    "doesnt_like_it",
}

_SYSTEM_MSG = {"role": "system", "content": _SYSTEM_PROMPT}


def _light_clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_text(response) -> str:
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if hasattr(part, "text"):
                    return part.text
    return ""


def _usage_tokens(response) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from a Responses API result."""
    u = getattr(response, "usage", None)
    return (getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)


def _is_content_filter(exc: BadRequestError) -> bool:
    """True if a 400 was raised by Azure's content-management/jailbreak filter."""
    s = str(exc)
    return "content_filter" in s or "content management" in s


def _assistant_msg(text: str) -> dict:
    """Input-safe assistant turn for conversation_history.

    We never persist raw Responses *output* items across turns: they carry
    server-only fields (e.g. `status="completed"`) that the Responses *input*
    schema rejects (400 Unknown parameter 'input[..].status'). Storing plain
    {role, content} keeps replay valid; the in-node tool round-trip still uses
    the raw output items locally, which is fine within a single response.
    """
    return {"role": "assistant", "content": text}


def _inputs(state: TriageState) -> tuple[str, str | None, bool]:
    """
    Accept both input shapes:
      - flat:   state["message"], state["user_id"], state["buggy_db"]
      - nested: state["case"]["message"], ...  (Derrick's harness contract,
                as seen in agent_handoffs.input_json on GCP)
    Flat keys win if both are present.
    """
    case = state.get("case") or {}
    message = state.get("message", case.get("message"))
    user_id = state.get("user_id", case.get("user_id"))
    buggy = state.get("buggy_db", case.get("buggy_db", False))
    return message, user_id, buggy


def triage_node(state: TriageState) -> dict:
    message, user_id, buggy = _inputs(state)

    # Correlation IDs live in state from the very first turn so that every
    # audit event of a run/ticket can be joined, including Cases A and B.
    trace_id = state.get("trace_id") or str(uuid.uuid4())
    ticket_id = state.get("ticket_id") or str(uuid.uuid4())
    # Azure usage accumulated across turns; lands in agent_handoffs token columns
    in_tok = int(state.get("llm_input_tokens") or 0)
    out_tok = int(state.get("llm_output_tokens") or 0)
    ids = {"trace_id": trace_id, "ticket_id": ticket_id}

    def _log(event_type: str, payload: dict | None = None) -> None:
        log_event(trace_id, event_type, agent="triage_agent",
                  ticket_id=ticket_id, user_id=user_id, payload=payload)

    # Shared main_db rows FIRST (INSERT IGNORE → idempotent across turns):
    # main_db FKs hang off workflow_runs.trace_id → tickets.ticket_id, so the
    # ticket and run rows must exist before any remote audit event references
    # this trace_id.
    pipeline_store.ensure_ticket(ticket_id, user_id, message)
    pipeline_store.start_workflow_run(trace_id, ticket_id)
    _log("run_started", {"message_length": len(message), "buggy_db": buggy})

    # history holds prior turns as input-safe {role, content} items (see
    # _assistant_msg) — never raw Responses output items — so it can be replayed.
    history: list = list(state.get("conversation_history") or [])
    user_msg = {"role": "user", "content": message}

    # First call — extract order_id (tool call) or ask for it (plain text).
    # This is where a user-controlled prompt injection lands. If Azure's own
    # content filter rejects it (jailbreak/injection), treat that as an ASI07
    # governance block and route to human review instead of crashing the graph.
    try:
        first = client.responses.create(
            model="gpt-5.4-pro",
            input=[_SYSTEM_MSG, *history, user_msg],
            tools=[ORDER_LOOKUP_TOOL],
        )
    except BadRequestError as exc:
        if not _is_content_filter(exc):
            raise
        _log("llm_content_filtered", {"defense": "azure_content_filter",
                                      "action": "route_to_human_approval"})
        result = {
            "status": "block",
            "rule": "ASI07",
            "failed_check": "content_filter",
            "detail": "Azure content filter rejected the message "
                      "(possible prompt injection / jailbreak).",
            "interceptor_action": "block",
        }
        log_governance_event(trace_id, ticket_id, user_id, result, "human_approval")
        # The interceptor node never runs on this path, so the pipeline rows
        # are written here: flag the ticket, snapshot the handoff, park the run.
        pipeline_store.flag_ticket_injection(ticket_id)
        pipeline_store.record_handoff(
            trace_id, ticket_id,
            from_agent="triage_agent", to_agent="human_approval",
            input_json=pipeline_store.build_handoff_input_json(state),
            output_json={
                "case": {"trace_id": trace_id, "ticket_id": ticket_id,
                         "goal": "evaluate refund eligibility",
                         "policy_version": "v1.0"},
                "order_facts": {}, "customer_request": {},
                "awaiting_order_id": False,
                "conversation_history": [m["content"] for m in history
                                         if isinstance(m, dict) and "content" in m],
                "clarification_question": None,
                "governance_result": result,
            },
            input_tokens=None, output_tokens=None,  # call was rejected pre-model
        )
        pipeline_store.update_workflow_run(
            trace_id, status="pending_human", current_agent="human_approval")
        return {
            **ids,
            "llm_input_tokens": in_tok, "llm_output_tokens": out_tok,
            "awaiting_order_id": False,
            "content_filter_blocked": True,
            "injection_flag": True,   # → shared tickets.injection_flag
            "governance_result": result,
            "next_agent": "human_approval",
            "conversation_history": history + [user_msg],
        }

    t_in, t_out = _usage_tokens(first)
    in_tok, out_tok = in_tok + t_in, out_tok + t_out

    tool_call = next(
        (item for item in first.output if item.type == "function_call"), None
    )

    # No tool call → ask user for order ID, save turn to history (Case A)
    if tool_call is None:
        question = _extract_text(first)
        _log("clarification_requested", {"question": question})
        return {
            **ids,
            "llm_input_tokens": in_tok, "llm_output_tokens": out_tok,
            "awaiting_order_id": True,
            "clarification_question": question,
            "conversation_history": history + [user_msg, _assistant_msg(question)],
        }

    # Execute Order_Database_Lookup
    args = json.loads(tool_call.arguments)
    order_id: str = args["order_id"]
    raw_result = order_database_lookup(order_id, buggy=buggy)
    _log("order_lookup_performed", {
        "order_id": order_id,
        "backend": active_backend(),
        "buggy": buggy,
        "found": raw_result is not None,
    })

    tool_result = {
        "type": "function_call_output",
        "call_id": tool_call.call_id,
        "output": json.dumps(raw_result) if raw_result else json.dumps({"error": "not found"}),
    }

    if raw_result is None:  # Case B
        _log("order_not_found", {"order_id": order_id})
        question = (
            f"I couldn't find order {order_id}. "
            "Could you double-check the order ID?"
        )
        return {
            **ids,
            "llm_input_tokens": in_tok, "llm_output_tokens": out_tok,
            "awaiting_order_id": True,
            "clarification_question": question,
            "conversation_history": history + [user_msg, _assistant_msg(question)],
        }

    # Second call — classify refund reason
    # Must pass all of first.output (reasoning item + function_call) together
    second = client.responses.create(
        model="gpt-5.4",
        input=[_SYSTEM_MSG, *history, user_msg, *first.output, tool_result],
        text={"format": {"type": "json_object"}},
    )

    t_in, t_out = _usage_tokens(second)
    in_tok, out_tok = in_tok + t_in, out_tok + t_out

    classification = json.loads(_extract_text(second))
    raw_reason = classification.get("refund_reason", "doesnt_like_it")
    reason = raw_reason if raw_reason in _VALID_REASONS else "doesnt_like_it"
    _log("classification_completed", {
        "refund_reason": reason,
        "fallback_applied": reason != raw_reason,
    })

    triage_output = {
        "case": {
            "trace_id": trace_id,
            "ticket_id": ticket_id,
            "goal": "evaluate refund eligibility",
            "policy_version": "v1.0",
        },
        "customer_request": {
            "sanitized_text": _light_clean(message),
            "refund_reason": reason,
            "requested_amount": raw_result["amount_paid"],
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

    _log("triage_output_ready", {
        "order_id": order_id,
        "refund_reason": reason,
        "requested_amount": raw_result["amount_paid"],
    })
    cr = triage_output["customer_request"]
    pipeline_store.update_ticket_triaged(
        ticket_id,
        sanitized_text=cr["sanitized_text"],
        refund_reason=cr["refund_reason"],
        requested_amount=cr["requested_amount"],
        currency=cr["currency"],
    )
    # Handoff row is written by the interceptor — destination unknown until verdict.
    return {
        **ids,
        "llm_input_tokens": in_tok, "llm_output_tokens": out_tok,
        "awaiting_order_id": False,
        "order_lookup_result": raw_result,
        "triage_output": triage_output,
        "conversation_history": history + [user_msg, _assistant_msg(_extract_text(second))],
    }
