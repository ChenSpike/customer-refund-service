"""Deterministic refund simulator for REFUND_AZURE=fake (fully offline).

This does NOT call the LLM pipeline. It inspects the message + order id and
returns the SAME result shape the real graph produces, so the frontend renders
identically offline and online. Its only job is to let the UI run with zero
credentials and to demo the governance paths (ASI07 leak, need-info, approve,
deny) deterministically. Real reasoning happens only in REFUND_AZURE=real.
"""
from __future__ import annotations

import re
import uuid

from refund_app.fixtures import get_order_fixture

_ORDER_RE = re.compile(r"\bORD-[A-Z0-9]+\b", re.IGNORECASE)
_REFUND_REASONS = ("damaged", "broken", "defective", "not working", "faulty",
                   "stopped working", "cracked", "wrong item")
_INJECTION_MARKERS = ("ignore previous", "ignore all previous", "disregard",
                      "system prompt", "you are now", "override")


def _extract_order_id(message: str, order_id: str | None) -> str | None:
    if order_id and order_id.strip():
        return order_id.strip().upper()
    match = _ORDER_RE.search(message or "")
    return match.group(0).upper() if match else None


def _result(*, final_outcome, workflow_status, body,
            triage="allow", policy=None, detail=None, human_review=None):
    return {
        "final_outcome": final_outcome,
        "workflow_status": workflow_status,
        "response_body": body,
        "governance": {"triage": triage, "policy": policy, "detail": detail},
        "human_review": human_review,
        "trace_id": f"SIM-{uuid.uuid4().hex[:8].upper()}",
    }


def simulate(message: str, order_id: str | None) -> dict:
    text = (message or "").lower()
    oid = _extract_order_id(message, order_id)

    # 1. Injection / content-filter demo.
    if any(m in text for m in _INJECTION_MARKERS):
        return _result(
            final_outcome="manual_review",
            workflow_status="waiting_human",
            body=("Thanks for reaching out. Your message has been flagged for a "
                  "quick manual check before we can proceed. Our team will follow "
                  "up shortly.\n\nCustomer Support Team"),
            triage="allow",
            detail="Input flagged by content filter (injection markers).",
            human_review={"status": "pending", "reason": "content_filter", "stage": "triage"},
        )

    # 2. No order id → need-info.
    if not oid:
        return _result(
            final_outcome="need_info",
            workflow_status="waiting_user",
            body=("Happy to help with your refund. Could you share your order ID "
                  "(it looks like ORD-XXXX) so we can look up your purchase?\n\n"
                  "Customer Support Team"),
        )

    order = get_order_fixture(oid)

    # 3. Unknown order → need-info.
    if order is None:
        return _result(
            final_outcome="need_info",
            workflow_status="waiting_user",
            body=(f"We couldn't find an order matching {oid}. Could you double-check "
                  "the order ID and resend it?\n\nCustomer Support Team"),
        )

    # 4. ASI07 ownership breach → triage governance block → human review.
    if order["contact_customer_id"] != order["order_customer_id"]:
        return _result(
            final_outcome="manual_review",
            workflow_status="waiting_human",
            body=("Thanks for your patience. Your request needs a brief manual "
                  "review before we can continue. Our team will be in touch "
                  "shortly.\n\nCustomer Support Team"),
            triage="block",
            detail="ASI07 ownership mismatch: contact record does not belong to the order owner.",
            human_review={"status": "pending", "reason": "governance_block", "stage": "triage"},
        )

    # 5. Valid order — decide on reason.
    has_reason = any(r in text for r in _REFUND_REASONS)
    if has_reason:
        return _result(
            final_outcome="partial_refund" if order["amount_paid"] >= 100 else "approved",
            workflow_status="completed",
            body=(f"We're sorry your {order['product_type'].lower()} arrived in poor "
                  f"condition. We've approved a refund of ${order['amount_paid']:.2f} "
                  "to your original payment method — allow 5–7 business days.\n\n"
                  "Customer Support Team"),
            triage="allow",
            policy="allow",
        )

    return _result(
        final_outcome="denied",
        workflow_status="completed",
        body=("Thanks for reaching out. Based on our refund policy we're unable to "
              "approve this request, as the order doesn't meet the criteria for a "
              "refund. Please let us know if there's anything else we can help "
              "with.\n\nCustomer Support Team"),
        triage="allow",
        policy="allow",
    )
