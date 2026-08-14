"""Canonical-to-dashboard aggregation for the read-only operations console.

This module retains the useful case views from dashboard-v2 (0842c9e6) while
adapting them to the state-driven persistence contract.  It has no FastAPI or
GCP dependency, so unit tests can supply an in-memory repository.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from .repository import require_final_database_name


NODE_LABELS = (
    "User Input",
    "Triage Agent",
    "Triage Governance",
    "Policy Agent",
    "Policy Governance",
    "Human Approval",
    "Refund Agent",
    "Response Agent",
)

OWASP_LABELS = {
    "ASI00": "No finding",
    "ASI01": "Goal Hijack / Drift",
    "ASI02": "Tool Misuse",
    "ASI06": "Prompt Injection",
    "ASI07": "Data Leakage",
    "ASI08": "Excessive Autonomy",
}

STATUS_ORDER = (
    "quarantined",
    "manual_review",
    "needs_info",
    "pending_review",
    "human_approved",
    "auto_approved",
    "rejected",
)

NODE_COLORS = {
    "done": "oklch(0.5 0.15 150)",
    "current": "oklch(0.55 0.15 80)",
    "blocked": "oklch(0.5 0.19 25)",
    "skipped": "oklch(0.5 0.19 25 / 0.45)",
    "pending": "oklch(0.85 0.01 90)",
}


class DashboardDataError(ValueError):
    """Persisted workflow data does not satisfy the dashboard contract."""


class DashboardNotFound(LookupError):
    """The requested workflow trace does not exist."""


class DashboardReadRepository(Protocol):
    database_name: str

    def list_case_bundles(self, limit: int = 200) -> list[dict[str, Any]]: ...

    def get_case_bundle(self, trace_id: str) -> dict[str, Any] | None: ...

    def query_audit(self, **filters: Any) -> list[dict[str, Any]]: ...

    def query_governance(self, **filters: Any) -> list[dict[str, Any]]: ...

    def pending_approvals(self, limit: int = 100) -> list[dict[str, Any]]: ...


class DashboardService:
    def __init__(self, repository: DashboardReadRepository) -> None:
        require_final_database_name(getattr(repository, "database_name", None))
        self.repository = repository

    def list_cases(self, limit: int = 200) -> list[dict[str, Any]]:
        return [build_case_summary(bundle) for bundle in self.repository.list_case_bundles(limit)]

    def get_case(self, trace_id: str) -> dict[str, Any]:
        bundle = self.repository.get_case_bundle(trace_id)
        if bundle is None:
            raise DashboardNotFound(trace_id)
        return build_case_detail(bundle)

    def metrics(self, limit: int = 500) -> dict[str, Any]:
        bundles = self.repository.list_case_bundles(limit)
        cases = [build_case_summary(bundle) for bundle in bundles]
        audit = [row for bundle in bundles for row in bundle.get("audit_log", [])]
        approvals = [row for bundle in bundles for row in bundle.get("approvals", [])]
        governance = [row for bundle in bundles for row in bundle.get("governance_events", [])]
        return build_metrics(cases, audit, approvals, governance)

    def audit(self, **filters: Any) -> list[dict[str, Any]]:
        return [summarize_audit_row(row) for row in self.repository.query_audit(**filters)]

    def governance(self, **filters: Any) -> list[dict[str, Any]]:
        return [normalize_governance_row(row) for row in self.repository.query_governance(**filters)]

    def pending_approvals(self, limit: int = 100) -> list[dict[str, Any]]:
        return [normalize_approval_row(row) for row in self.repository.pending_approvals(limit)]


def build_case_summary(bundle: Mapping[str, Any]) -> dict[str, Any]:
    workflow = _mapping(bundle.get("workflow"), "workflow")
    ticket = _optional_mapping(bundle.get("ticket"))
    customer = _optional_mapping(bundle.get("customer"))
    handoffs = _mapping_list(bundle.get("handoffs"), "handoffs")
    governance = _mapping_list(bundle.get("governance_events"), "governance_events")
    approvals = _mapping_list(bundle.get("approvals"), "approvals")
    refunds = _mapping_list(bundle.get("refunds"), "refunds")

    triage = _triage_payload(handoffs)
    policy = _policy_payload(handoffs)
    response = _response_payload(handoffs)
    request = _customer_request(ticket, triage, policy)
    decision = _decision_type(policy)
    final_outcome = _response_final_outcome(response)
    status = _derive_status(workflow, decision, final_outcome, governance, approvals, refunds)
    risk_tag = _risk_tag(governance)
    updated = workflow.get("updated_at") or ticket.get("updated_at") or workflow.get("started_at")
    trace_id = str(workflow.get("trace_id") or "")
    if not trace_id:
        raise DashboardDataError("workflow.trace_id is required")

    return {
        "id": ticket.get("ticket_id") or workflow.get("ticket_id") or trace_id,
        "traceId": trace_id,
        "customer": customer.get("full_name") or ticket.get("customer_id") or "Unknown Customer",
        "summary": request["sanitizedText"][:140],
        "reason": request["refundReason"],
        "reasonLabel": _humanize(request["refundReason"]),
        "amount": request["requestedAmount"],
        "currency": request["currency"],
        "status": status,
        "statusSource": _status_source(status, workflow, decision, governance, approvals, refunds),
        "workflowStatus": workflow.get("status"),
        "currentAgent": workflow.get("current_agent"),
        "finalOutcome": final_outcome,
        "riskTag": risk_tag,
        "updated": _relative_time(updated),
        "updatedAt": _iso(updated),
        "request": request,
    }


def build_case_detail(bundle: Mapping[str, Any]) -> dict[str, Any]:
    summary = build_case_summary(bundle)
    workflow = _mapping(bundle.get("workflow"), "workflow")
    handoffs = _mapping_list(bundle.get("handoffs"), "handoffs")
    governance_rows = _mapping_list(bundle.get("governance_events"), "governance_events")
    approvals = _mapping_list(bundle.get("approvals"), "approvals")
    refunds = _mapping_list(bundle.get("refunds"), "refunds")
    audit_rows = _mapping_list(bundle.get("audit_log"), "audit_log")
    policy_reviews = _mapping_list(bundle.get("policy_reviews"), "policy_reviews")
    orders = _mapping_list(bundle.get("orders"), "orders")

    triage = _triage_payload(handoffs)
    policy = _policy_payload(handoffs)
    detail = dict(summary)
    detail.update(
        {
            "order": _order_section(orders, triage, policy),
            "policy": _policy_section(policy, policy_reviews),
            "hasGaps": bool((_optional_mapping(policy.get("policy_evaluation"))).get("gaps_or_conflicts")),
            "governance": _governance_section(policy, governance_rows),
            "hasFlags": bool(_risk_tag(governance_rows)),
            "pipeline": _pipeline(workflow, handoffs, governance_rows, approvals, refunds, policy),
            "notes": [summarize_audit_row(row) for row in audit_rows],
            "refund": _refund_section(refunds, summary["amount"]),
            "pendingApprovalId": next(
                (row.get("approval_id") for row in approvals if row.get("status") == "pending"),
                None,
            ),
            "approvals": [normalize_approval_row(row) for row in approvals],
            "policyReviews": [_serializable(row) for row in policy_reviews],
            "policyVersion": workflow.get("policy_version") or "v1.0",
            "readOnly": True,
        }
    )
    return detail


def build_metrics(
    cases: list[dict[str, Any]],
    audit_rows: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    governance_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    total = len(cases)
    status_counts = Counter(case["status"] for case in cases)
    blocked = [
        row for row in governance_rows
        if str(row.get("interceptor_action") or "").lower() in {"block", "quarantine"}
    ]
    owasp_counts = Counter(str(row.get("owasp_category") or "UNKNOWN") for row in blocked)
    resolved_seconds = []
    for row in approvals:
        created = _as_datetime(row.get("created_at"))
        resolved = _as_datetime(row.get("resolved_at"))
        if created and resolved:
            resolved_seconds.append(max(0.0, (resolved - created).total_seconds()))
    average_review = _format_duration(sum(resolved_seconds) / len(resolved_seconds)) if resolved_seconds else "-"
    auto = status_counts["auto_approved"]
    review = status_counts["manual_review"] + status_counts["quarantined"]
    governance_total = len(governance_rows)

    return {
        "primaryStats": [
            {
                "label": "Automated Throughput",
                "value": f"{round(auto * 100 / total)}%" if total else "0%",
                "detail": f"{auto} of {total} cases completed without human review",
            },
            {
                "label": "Governance Holds",
                "value": str(len(blocked)),
                "detail": f"{review} cases are held or awaiting manual review",
            },
            {
                "label": "System Auditability",
                "value": "Traceable" if audit_rows or not total else "Incomplete",
                "detail": f"{len(audit_rows)} audit events across {total} cases",
            },
            {
                "label": "Avg Review Time",
                "value": average_review,
                "detail": "Measured from approval creation to resolution",
            },
        ],
        "secondaryStats": [
            {"label": "Total Cases", "value": str(total)},
            {"label": "Pending Approvals", "value": str(sum(1 for row in approvals if row.get("status") == "pending"))},
            {"label": "Governance Checks", "value": str(governance_total)},
            {"label": "Audit Events", "value": str(len(audit_rows))},
        ],
        "statusBreakdown": [
            {"status": status, "count": status_counts[status]}
            for status in STATUS_ORDER
            if status_counts[status]
        ],
        "owaspBreakdown": [
            {
                "category": category,
                "label": OWASP_LABELS.get(category, category),
                "count": count,
            }
            for category, count in sorted(owasp_counts.items())
        ],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def summarize_audit_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    payload = _json_object(row.get("payload_json"), "audit_log.payload_json", allow_none=True)
    event_type = str(row.get("event_type") or "system_event")
    actor = str(row.get("agent") or "System")
    category = _audit_category(event_type, actor)
    summary = _audit_summary(event_type, payload)
    normalized = _serializable(row)
    normalized.update(
        {
            "payload": _serializable(payload),
            "summary": summary,
            "category": category,
            "actor": _humanize(actor),
            "relativeTime": _relative_time(row.get("created_at")),
        }
    )
    return normalized


def normalize_governance_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = _serializable(dict(raw))
    flags = _json_object(raw.get("flags_json"), "governance_events.flags_json", allow_none=True)
    row["flags"] = _serializable(flags)
    row["riskLabel"] = OWASP_LABELS.get(str(raw.get("owasp_category")), raw.get("owasp_category"))
    row["relativeTime"] = _relative_time(raw.get("created_at"))
    return row


def normalize_approval_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = _serializable(dict(raw))
    row["notesPayload"] = _notes_payload(raw.get("notes"))
    row["policyIds"] = _json_value(raw.get("policy_ids_json"), "policy_review_events.policy_ids_json", allow_none=True) or []
    trigger_type = raw.get("triggering_event_type")
    if trigger_type == "governance":
        row["trigger"] = {
            "type": "governance",
            "category": raw.get("governance_owasp_category"),
            "score": _number(raw.get("governance_trigger_score")),
            "action": raw.get("governance_action"),
            "detail": raw.get("governance_offending_content"),
        }
    elif trigger_type == "policy_review":
        row["trigger"] = {
            "type": "policy_review",
            "reviewType": raw.get("policy_review_type"),
            "detail": raw.get("policy_review_detail"),
            "policyIds": row["policyIds"],
        }
    else:
        row["trigger"] = {"type": trigger_type or "unknown"}
    return row


def _derive_status(
    workflow: Mapping[str, Any],
    decision: str | None,
    final_outcome: str | None,
    governance: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
) -> str:
    pending = any(row.get("status") == "pending" for row in approvals)
    blocked = any(
        str(row.get("interceptor_action") or "").lower() in {"block", "quarantine"}
        for row in governance
    )
    workflow_status = str(workflow.get("status") or "").lower()
    outcome = str(final_outcome or "").lower()
    refund_statuses = {str(row.get("status") or "").lower() for row in refunds}
    resolved_human_review = any(
        str(row.get("status") or "").lower() in {"approved", "rejected"}
        or row.get("resolved_at") is not None
        for row in approvals
    )
    if pending and blocked:
        return "quarantined"
    if pending:
        return "manual_review"
    if outcome == "need_info" or decision == "request_info" or workflow_status == "waiting_user":
        return "needs_info"
    if outcome in {"denied", "refund_failed"} or decision == "deny" or workflow_status == "failed" or refund_statuses & {"failed", "blocked"}:
        return "rejected"
    if outcome in {"approved", "partial_refund"} or refund_statuses & {"issued", "success"}:
        return "human_approved" if resolved_human_review else "auto_approved"
    if outcome == "manual_review" or decision == "manual_review" or workflow_status in {"pending_human", "waiting_human"}:
        return "manual_review"
    return "pending_review"


def _status_source(
    status: str,
    workflow: Mapping[str, Any],
    decision: str | None,
    governance: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
) -> str:
    if status == "quarantined":
        blocked = [row for row in governance if row.get("interceptor_action") in {"block", "quarantine"}]
        return _governance_label(blocked[-1].get("agent")) if blocked else "Governance"
    if status == "manual_review":
        return "Human Approval" if approvals else "Policy Agent"
    if status == "needs_info":
        return "Policy Agent" if decision == "request_info" else "Triage Agent"
    if status == "rejected":
        return "Refund Agent" if refunds else ("Policy Agent" if decision == "deny" else "System")
    if status == "auto_approved":
        return "Refund Agent"
    if status == "human_approved":
        return "Human Approval"
    return _humanize(workflow.get("current_agent") or "System")


def _customer_request(
    ticket: Mapping[str, Any],
    triage: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    triage_request = _optional_mapping(triage.get("customer_request"))
    policy_request = _optional_mapping(policy.get("customer_request"))
    text = (
        policy_request.get("sanitized_text")
        or triage_request.get("sanitized_text")
        or ticket.get("sanitized_text")
        or ticket.get("raw_text")
        or "(no request text captured)"
    )
    amount = ticket.get("requested_amount")
    if amount is None:
        amount = policy_request.get("requested_amount", triage_request.get("requested_amount"))
    reason = ticket.get("refund_reason") or policy_request.get("refund_reason") or triage_request.get("refund_reason") or "unknown"
    currency = ticket.get("currency") or policy_request.get("currency") or triage_request.get("currency") or "USD"
    return {
        "sanitizedText": str(text),
        "requestedAmount": _number(amount),
        "refundReason": str(reason),
        "currency": str(currency),
    }


def _order_section(
    orders: list[dict[str, Any]],
    triage: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    facts = _optional_mapping(policy.get("order_facts")) or _optional_mapping(triage.get("order_facts"))
    requested_id = facts.get("order_id")
    order = next((row for row in orders if row.get("order_id") == requested_id), None)
    order = order or (orders[0] if len(orders) == 1 else None) or {}
    return {
        "orderId": requested_id or order.get("order_id") or "-",
        "productType": _humanize(facts.get("product_type") or order.get("product_type")),
        "purchaseDate": str(facts.get("purchase_date") or order.get("purchase_date") or "-")[:10],
        "itemStatus": _humanize(facts.get("item_status") or order.get("item_status")),
        "amountPaid": _number(facts.get("amount_paid", order.get("amount_paid"))),
        "priorRefundTotal": _number(facts.get("prior_refund_total", order.get("prior_refund_total"))),
    }


def _policy_section(policy: Mapping[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    evaluation = _optional_mapping(policy.get("policy_evaluation"))
    decision = _optional_mapping(policy.get("decision"))
    matched = []
    for raw in evaluation.get("matched_policies") or []:
        item = _mapping(raw, "policy_evaluation.matched_policies[]")
        matched.append(
            {
                "id": item.get("policy_id") or "-",
                "summary": item.get("rule_summary") or "-",
                "effect": _humanize(item.get("effect")),
            }
        )
    gaps = []
    for raw in evaluation.get("gaps_or_conflicts") or []:
        if isinstance(raw, Mapping):
            gaps.append(
                {
                    "type": _humanize(raw.get("type") or "Note"),
                    "detail": raw.get("detail") or raw.get("description") or json.dumps(dict(raw), ensure_ascii=False),
                }
            )
        else:
            gaps.append({"type": "Note", "detail": str(raw)})
    if not gaps:
        gaps = [
            {"type": _humanize(row.get("review_type")), "detail": row.get("detail") or "Review requested"}
            for row in reviews
        ]
    confidence = decision.get("confidence_level")
    if confidence is None:
        confidence = decision.get("confidence")
    return {
        "matchedPolicies": matched,
        "gaps": gaps,
        "decision": {
            "type": _humanize(decision.get("type") or "pending"),
            "amount": _number(decision.get("refund_amount")),
            "confidence": _humanize(confidence) if confidence is not None else "-",
            "reasonText": decision.get("reason") or "Awaiting Policy Agent evaluation.",
        },
    }


def _governance_section(policy: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    governance = _optional_mapping(policy.get("governance"))
    score = governance.get("semantic_drift_score")
    row_scores = [_number(row.get("trigger_score")) for row in rows if row.get("trigger_score") is not None]
    if score is None:
        score = max(row_scores, default=0.0)
    actions = [str(row.get("interceptor_action") or "allow") for row in rows]
    action = governance.get("interceptor_action") or _worst_action(actions)
    flags = list(governance.get("flags") or [])
    for row in rows:
        payload = _json_object(row.get("flags_json"), "governance_events.flags_json", allow_none=True)
        finding = _optional_mapping(payload.get("finding"))
        flag = finding.get("flag")
        if flag and flag not in flags:
            flags.append(flag)
    offending = next((row.get("offending_content") for row in rows if row.get("offending_content")), None)
    pii = None
    for row in rows:
        if row.get("owasp_category") != "ASI07":
            continue
        payload = _json_object(row.get("flags_json"), "governance_events.flags_json", allow_none=True)
        finding = _optional_mapping(payload.get("finding"))
        pii = {
            "field": finding.get("offending_field") or "customer record",
            "note": finding.get("detail") or "A customer ownership or PII mismatch was detected.",
        }
        break
    return {
        "triggerScore": round(_number(score), 3),
        "action": action,
        "flags": flags,
        "offendingText": offending,
        "piiFlag": pii,
    }


def _pipeline(
    workflow: Mapping[str, Any],
    handoffs: list[dict[str, Any]],
    governance: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    agents = {row.get("from_agent") for row in handoffs}
    triage_done = "triage_agent" in agents
    policy_done = "policy_agent" in agents
    response_done = "response_agent" in agents
    triage_blocked = any(
        row.get("agent") == "triage_agent" and row.get("interceptor_action") in {"block", "quarantine"}
        for row in governance
    )
    policy_blocked = any(
        row.get("agent") == "policy_agent" and row.get("interceptor_action") in {"block", "quarantine"}
        for row in governance
    )
    approval_pending = any(row.get("status") == "pending" for row in approvals)
    approval_exists = bool(approvals)
    decision = _decision_type(policy)
    refund_done = any(row.get("status") in {"issued", "success"} for row in refunds)
    refund_failed = any(row.get("status") in {"failed", "blocked"} for row in refunds)
    refund_expected = decision in {"approve", "partial_refund"} or bool(refunds)

    states = [
        "done",
        "done" if triage_done else "current",
        "blocked" if triage_blocked else ("done" if triage_done else "pending"),
        "done" if policy_done else ("current" if triage_done and not triage_blocked else "pending"),
        "blocked" if policy_blocked else ("done" if policy_done else "pending"),
        "current" if approval_pending else ("done" if approval_exists else "skipped"),
        "blocked" if refund_failed else ("done" if refund_done else ("current" if refund_expected and policy_done else "skipped")),
        "done" if response_done else "pending",
    ]
    if workflow.get("status") in {"completed", "waiting_user", "pending_human", "waiting_human"}:
        states = ["skipped" if state == "current" and index not in {5, 7} else state for index, state in enumerate(states)]
    nodes = []
    for index, (label, state) in enumerate(zip(NODE_LABELS, states, strict=True)):
        nodes.append(
            {
                "label": label,
                "state": state,
                "color": NODE_COLORS[state],
                "lineColor": NODE_COLORS["done"] if state == "done" else NODE_COLORS["pending"],
                "hasNext": index < len(NODE_LABELS) - 1,
            }
        )
    return nodes


def _refund_section(refunds: list[dict[str, Any]], requested_amount: float) -> dict[str, Any] | None:
    if not refunds:
        return None
    row = sorted(refunds, key=lambda item: str(item.get("created_at") or ""))[-1]
    return {
        "amount": _number(row.get("amount")),
        "status": row.get("status"),
        "currency": row.get("currency") or "USD",
        "externalRef": row.get("external_ref"),
        "transactionId": row.get("transaction_id"),
        "isPartial": bool(requested_amount and _number(row.get("amount")) < requested_amount),
    }


def _triage_payload(handoffs: list[dict[str, Any]]) -> dict[str, Any]:
    row = _latest_handoff(handoffs, "triage_agent")
    if row is None:
        return {}
    envelope = _json_object(row.get("output_json"), "triage_agent.output_json")
    nested = envelope.get("triage_output")
    return _mapping(nested, "triage_output") if nested is not None else envelope


def _policy_payload(handoffs: list[dict[str, Any]]) -> dict[str, Any]:
    row = _latest_handoff(handoffs, "policy_agent")
    return _json_object(row.get("output_json"), "policy_agent.output_json") if row else {}


def _response_payload(handoffs: list[dict[str, Any]]) -> dict[str, Any]:
    row = _latest_handoff(handoffs, "response_agent")
    if row is None:
        return {}
    envelope = _json_object(row.get("output_json"), "response_agent.output_json")
    nested = envelope.get("response_result")
    return _mapping(nested, "response_result") if nested is not None else envelope


def _latest_handoff(handoffs: list[dict[str, Any]], agent: str) -> dict[str, Any] | None:
    candidates = [row for row in handoffs if row.get("from_agent") == agent]
    if not candidates:
        return None
    return sorted(candidates, key=lambda row: str(row.get("created_at") or row.get("handoff_id") or ""))[-1]


def _decision_type(policy: Mapping[str, Any]) -> str | None:
    decision = _optional_mapping(policy.get("decision"))
    value = decision.get("type") or decision.get("decision")
    return str(value).lower() if value else None


def _response_final_outcome(response: Mapping[str, Any]) -> str | None:
    value = response.get("final_outcome")
    return str(value).lower() if value else None


def _risk_tag(rows: list[dict[str, Any]]) -> dict[str, str] | None:
    categories = []
    for row in rows:
        if row.get("interceptor_action") not in {"block", "quarantine"}:
            continue
        category = str(row.get("owasp_category") or "GOVERNANCE")
        if category not in categories:
            categories.append(category)
    if not categories:
        return None
    return {
        "code": " + ".join(categories),
        "label": " / ".join(OWASP_LABELS.get(category, category) for category in categories),
    }


def _audit_category(event_type: str, agent: str) -> str:
    value = f"{event_type} {agent}".lower()
    if "governance" in value or "interceptor" in value:
        return "Governance"
    if "policy" in value:
        return "Policy"
    if "triage" in value:
        return "Triage"
    if "refund" in value:
        return "Refund"
    if "admin" in value or "reviewer" in value:
        return "Admin"
    return "System"


def _audit_summary(event_type: str, payload: Mapping[str, Any]) -> str:
    known = {
        "triage_agent_evaluated": "Triage completed and persisted its handoff.",
        "policy_agent_evaluated": "Policy evaluated the request and persisted its decision.",
        "response_agent_evaluated": "Response generated and persisted the customer-facing result.",
        "refund_execution": "Refund execution completed.",
        "governance_block": "Governance blocked the workflow for review.",
    }
    if event_type in known:
        return known[event_type]
    output = _optional_mapping(payload.get("output"))
    decision = _optional_mapping(output.get("decision"))
    if decision.get("type"):
        return f"{_humanize(event_type)}: {_humanize(decision['type'])}."
    return f"{_humanize(event_type)}."


def _json_object(raw: Any, label: str, *, allow_none: bool = False) -> dict[str, Any]:
    value = _json_value(raw, label, allow_none=allow_none)
    if value is None and allow_none:
        return {}
    if not isinstance(value, Mapping):
        raise DashboardDataError(f"{label} must be a JSON object")
    return dict(value)


def _notes_payload(raw: Any) -> dict[str, Any]:
    """Approval notes may be canonical JSON or reviewer-authored plain text."""

    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw}
        return dict(value) if isinstance(value, Mapping) else {"value": value}
    return {"value": _serializable(raw)}


def _json_value(raw: Any, label: str, *, allow_none: bool = False) -> Any:
    if raw is None or raw == "":
        if allow_none:
            return None
        raise DashboardDataError(f"{label} is required")
    if isinstance(raw, (Mapping, list)):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DashboardDataError(f"{label} contains invalid JSON") from exc
    raise DashboardDataError(f"{label} has unsupported type {type(raw).__name__}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DashboardDataError(f"{label} must be an object")
    return dict(value)


def _optional_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_list(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DashboardDataError(f"{label} must be a list")
    return [_mapping(item, f"{label}[]") for item in value]


def _serializable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DashboardDataError(f"Expected a number, got {value!r}") from exc


def _humanize(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("_", " ").strip().title()


def _governance_label(agent: Any) -> str:
    if agent == "triage_agent":
        return "Triage Governance"
    if agent == "policy_agent":
        return "Policy Governance"
    return f"{_humanize(agent)} Governance" if agent else "Governance"


def _worst_action(actions: list[str]) -> str:
    severity = {"allow": 0, "quarantine": 1, "block": 2}
    return max(actions or ["allow"], key=lambda item: severity.get(item, -1))


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value: Any) -> str | None:
    parsed = _as_datetime(value)
    return parsed.isoformat() if parsed else (str(value) if value else None)


def _relative_time(value: Any) -> str:
    parsed = _as_datetime(value)
    if parsed is None:
        return "-"
    if parsed.tzinfo is None:
        now = datetime.now()
    else:
        now = datetime.now(parsed.tzinfo)
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"
