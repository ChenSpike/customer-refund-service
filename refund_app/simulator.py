"""Deterministic offline execution for the canonical 20-case demo corpus."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from demo.catalog import (
    DEFAULT_MANIFEST_PATH,
    DemoCase,
    load_demo_catalog,
    resolve_demo_case,
)


def simulate_case(case: DemoCase) -> dict[str, Any]:
    """Return a stable graph-shaped result derived from fixture expectations."""

    expected = case.expectations
    is_human = expected.route == "human_approval"
    is_refund = expected.outcome == "refund_issued"
    is_request_info = expected.outcome == "need_info"
    triage_status = "block" if case.trace_id in {"demo12", "demo13"} else "allow"
    policy_status = None if triage_status == "block" else "allow"

    governance_detail = None
    if case.trace_id == "demo12":
        governance_detail = "Prompt-injection content requires human review."
    elif case.trace_id == "demo13":
        governance_detail = "Possible cross-customer data exposure requires human review."

    human_review = None
    if is_human:
        human_review = {
            "status": "pending",
            "stage": "triage" if triage_status == "block" else "policy",
            "reason": "governance_block" if triage_status == "block" else "policy_manual_review",
        }

    refund_result = None
    if is_refund:
        refund_result = {
            "status": "success",
            "order_id": case.order_id,
            "amount": float(case.order["amount_paid"]),
            "currency": case.order.get("currency", "USD"),
            "message": "Refund issued for the seeded demo order.",
        }

    if is_refund:
        body = (
            f"Refund approved for {case.order_id}. "
            f"${float(case.order['amount_paid']):.2f} will return to the original payment method."
        )
    elif is_request_info:
        body = f"We found {case.order_id}, but need more information before evaluating the refund."
    elif is_human:
        body = f"{case.order_id} is pending human review; no refund has been issued yet."
    else:
        body = f"The refund request for {case.order_id} does not meet the policy criteria."

    return {
        "trace_id": case.trace_id,
        "ticket_id": case.ticket_id,
        "customer_id": case.customer_id,
        "order_id": case.order_id,
        "selected_order_id": case.selected_order_id,
        "order_resolution_source": (
            "trusted_ui_selection" if case.selected_order_id else "offline_fixture"
        ),
        "message": case.message,
        "expected_message": case.message,
        "route": expected.route,
        "policy_decision": None if triage_status == "block" else expected.policy_decision,
        "final_outcome": expected.outcome,
        "workflow_status": expected.terminal_state,
        "response_body": body,
        "response_content_checks": {
            "decision_reflected": True,
            "missing_info_requested": True,
            "safe_summary_reflected": True,
            "outcome_anchor_reflected": True,
            "pii_fields_detected": [],
            "forbidden_phrases": [],
            "simulation_only": True,
        },
        "governance": {
            "triage": triage_status,
            "policy": policy_status,
            "response": "allow",
            "detail": governance_detail,
        },
        "human_review": human_review,
        "refund_result": refund_result,
    }


def simulate(
    message: str | None = None,
    order_id: str | None = None,
    *,
    case_id: str | None = None,
    customer_id: str | None = None,
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
) -> dict[str, Any]:
    """Resolve exact canonical selectors and simulate one allowlisted case."""

    catalog = load_demo_catalog(manifest_path)
    case = resolve_demo_case(
        catalog,
        case_id=case_id,
        order_id=order_id,
        customer_id=customer_id,
        message=message,
    )
    return simulate_case(case)
