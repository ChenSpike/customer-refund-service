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
    governance_history = _mapping_list(bundle.get("governance_events"), "governance_events")
    approvals = _mapping_list(bundle.get("approvals"), "approvals")
    refund_history = _mapping_list(bundle.get("refunds"), "refunds")
    audit_history = _mapping_list(bundle.get("audit_log"), "audit_log")

    triage = _triage_payload(handoffs)
    policy = _policy_payload(handoffs)
    response = _response_payload(handoffs)
    governance = _effective_governance_rows(governance_history, handoffs=handoffs)
    refunds = _effective_refund_rows(refund_history, audit_rows=audit_history)
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
    ticket = _optional_mapping(bundle.get("ticket"))
    handoffs = _mapping_list(bundle.get("handoffs"), "handoffs")
    governance_rows = _mapping_list(bundle.get("governance_events"), "governance_events")
    approvals = _mapping_list(bundle.get("approvals"), "approvals")
    refunds = _mapping_list(bundle.get("refunds"), "refunds")
    audit_rows = _mapping_list(bundle.get("audit_log"), "audit_log")
    policy_reviews = _mapping_list(bundle.get("policy_reviews"), "policy_reviews")
    orders = _mapping_list(bundle.get("orders"), "orders")

    triage = _triage_payload(handoffs)
    policy = _policy_payload(handoffs)
    response = _response_payload(handoffs)
    effective_governance_rows = _effective_governance_rows(
        governance_rows,
        handoffs=handoffs,
    )
    effective_policy_reviews = _effective_policy_review_rows(
        policy_reviews,
        handoffs=handoffs,
    )
    effective_refunds = _effective_refund_rows(refunds, audit_rows=audit_rows)
    order = _order_section(orders, triage, policy)
    normalized_approvals = []
    for approval in approvals:
        approval_with_financials = {
            **approval,
            "ticket_requested_amount": ticket.get("requested_amount")
            if ticket.get("requested_amount") is not None
            else summary["request"]["requestedAmount"],
            "ticket_currency": ticket.get("currency") or summary["currency"],
            "order_amount_paid": order.get("amountPaid"),
            "order_prior_refund_total": order.get("priorRefundTotal"),
            "order_currency": summary["currency"],
        }
        normalized_approvals.append(normalize_approval_row(approval_with_financials))
    detail = dict(summary)
    detail.update(
        {
            "order": order,
            "policy": _policy_section(policy, effective_policy_reviews),
            "customerResponse": _customer_response_section(response),
            "hasGaps": bool((_optional_mapping(policy.get("policy_evaluation"))).get("gaps_or_conflicts")),
            "governance": _governance_section(policy, effective_governance_rows),
            "governanceEvents": [
                normalize_governance_row(row)
                for row in _ordered_governance_history(governance_rows)
            ],
            "hasFlags": bool(_risk_tag(effective_governance_rows)),
            "pipeline": _pipeline(
                workflow,
                handoffs,
                effective_governance_rows,
                approvals,
                effective_refunds,
                policy,
            ),
            # The repository returns every immutable audit row.  Reorder the
            # case timeline by durable continuation generation so the initial
            # waiting-for-review response and the post-review response are
            # both visible in their true lifecycle order, even when MySQL
            # assigns them the same second-resolution timestamp.
            "notes": _timeline_notes(audit_rows),
            "refund": _refund_section(effective_refunds, summary["amount"]),
            "refundTransactions": [
                _serializable(row)
                for row in _ordered_refund_history(refunds, audit_rows=audit_rows)
            ],
            "pendingApprovalId": next(
                (row.get("approval_id") for row in approvals if row.get("status") == "pending"),
                None,
            ),
            "approvals": normalized_approvals,
            "policyReviews": [
                _normalize_policy_review_row(row)
                for row in _ordered_policy_review_history(policy_reviews)
            ],
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
            # CaseDetail's timeline uses the concise aliases while the audit
            # page retains the richer summary/relativeTime contract.
            "text": summary,
            "time": _relative_time(row.get("created_at")),
        }
    )
    marker = _audit_continuation_marker(raw, payload)
    if marker is not None:
        normalized["continuation"] = _serializable(marker)
    return normalized


def normalize_governance_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = _serializable(dict(raw))
    flags = _json_object(raw.get("flags_json"), "governance_events.flags_json", allow_none=True)
    row["flags"] = _serializable(flags)
    marker = _row_continuation_marker(
        raw,
        payload_fields=("flags_json",),
        label="governance_events",
    )
    if marker is not None:
        row["continuation"] = _serializable(marker)
    row["riskLabel"] = OWASP_LABELS.get(str(raw.get("owasp_category")), raw.get("owasp_category"))
    row["relativeTime"] = _relative_time(raw.get("created_at"))
    return row


def normalize_approval_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = _serializable(dict(raw))
    amount_paid = _number(raw.get("order_amount_paid"))
    prior_refund_total = _number(raw.get("order_prior_refund_total"))
    remaining_refundable = round(max(0.0, amount_paid - prior_refund_total), 2)
    requested_source = next(
        (
            value
            for value in (
                raw.get("amount_requested"),
                raw.get("ticket_requested_amount"),
                remaining_refundable,
                amount_paid,
            )
            if value is not None and value != ""
        ),
        0,
    )
    row.update(
        {
            "requested_amount": _number(requested_source),
            "amount_paid": amount_paid,
            "prior_refund_total": prior_refund_total,
            "remaining_refundable": remaining_refundable,
            "currency": raw.get("ticket_currency") or raw.get("order_currency") or "USD",
        }
    )
    for internal_name in (
        "ticket_requested_amount",
        "ticket_currency",
        "order_amount_paid",
        "order_prior_refund_total",
        "order_currency",
    ):
        row.pop(internal_name, None)
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
    row = max(
        refunds,
        key=lambda item: (
            _timestamp_sort_value(item.get("created_at")),
            _primary_key_sort_value(item.get("transaction_id")),
        ),
    )
    return {
        "amount": _number(row.get("amount")),
        "status": row.get("status"),
        "currency": row.get("currency") or "USD",
        "externalRef": row.get("external_ref"),
        "transactionId": row.get("transaction_id"),
        "isPartial": bool(requested_amount and _number(row.get("amount")) < requested_amount),
    }


def _effective_refund_rows(
    rows: list[dict[str, Any]],
    *,
    audit_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    markers = _refund_marker_by_transaction(audit_rows)
    return [max(rows, key=lambda row: _refund_sort_key(row, markers))]


def _ordered_refund_history(
    rows: list[dict[str, Any]],
    *,
    audit_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    markers = _refund_marker_by_transaction(audit_rows)
    return sorted(rows, key=lambda row: _refund_sort_key(row, markers))


def _refund_marker_by_transaction(
    audit_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any] | None]:
    candidates: dict[str, list[tuple[dict[str, Any], dict[str, Any] | None]]] = {}
    for row in audit_rows:
        if row.get("event_type") not in {
            "refund_issued",
            "refund_failed",
            # Compatibility with the dashboard-v2 fixture vocabulary.
            "refund_execution",
        }:
            continue
        payload = _json_object(
            row.get("payload_json"),
            "audit_log.payload_json",
            allow_none=True,
        )
        transaction_id = str(payload.get("transaction_id") or "").strip()
        if not transaction_id:
            continue
        marker = _audit_continuation_marker(row, payload)
        candidates.setdefault(transaction_id, []).append((row, marker))

    result: dict[str, dict[str, Any] | None] = {}
    for transaction_id, transaction_rows in candidates.items():
        _, latest_marker = max(
            transaction_rows,
            key=lambda item: (
                *_continuation_generation_key(item[1]),
                _timestamp_sort_value(item[0].get("created_at")),
                _primary_key_sort_value(item[0].get("log_id")),
            ),
        )
        result[transaction_id] = latest_marker
    return result


def _refund_sort_key(
    row: Mapping[str, Any],
    markers: Mapping[str, Mapping[str, Any] | None],
) -> tuple[int, int, int, float, tuple[int, int, str]]:
    transaction_id = str(row.get("transaction_id") or "")
    return (
        *_continuation_generation_key(markers.get(transaction_id)),
        _timestamp_sort_value(row.get("created_at")),
        _primary_key_sort_value(transaction_id),
    )


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


def _customer_response_section(response: Mapping[str, Any]) -> dict[str, Any] | None:
    """Expose the persisted customer message and its fail-closed checks."""

    message = _optional_mapping(response.get("response"))
    checks = _optional_mapping(response.get("content_checks"))
    if not message and not checks and not response.get("final_outcome"):
        return None
    return {
        "channel": message.get("channel"),
        "subjectLine": message.get("subject_line"),
        "body": message.get("body"),
        "tone": message.get("tone"),
        "wordCount": message.get("word_count"),
        "finalOutcome": response.get("final_outcome"),
        "workflowStatus": response.get("workflow_status"),
        "contentChecks": _serializable(checks),
    }


def _latest_handoff(handoffs: list[dict[str, Any]], agent: str) -> dict[str, Any] | None:
    candidates = [row for row in handoffs if row.get("from_agent") == agent]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: _effective_row_sort_key(
            row,
            primary_key="handoff_id",
            payload_fields=("input_json", "output_json"),
            label="agent_handoffs",
        ),
    )


def _effective_governance_rows(
    rows: list[dict[str, Any]],
    *,
    handoffs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the newest governance generation per agent without losing history.

    ``build_case_detail`` separately exposes every row as ``governanceEvents``.
    This selector is only for the current case projection.  Multiple findings
    emitted by one continuation share the same marker, so all rows from the
    winning marker generation are retained.
    """

    by_agent: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_agent.setdefault(str(row.get("agent") or ""), []).append(row)

    effective: list[dict[str, Any]] = []
    for agent, agent_rows in by_agent.items():
        latest = max(
            agent_rows,
            key=lambda row: _effective_row_sort_key(
                row,
                primary_key="event_id",
                payload_fields=("flags_json",),
                label="governance_events",
            ),
        )
        latest_marker = _row_continuation_marker(
            latest,
            payload_fields=("flags_json",),
            label="governance_events",
        )
        latest_handoff = _latest_handoff(handoffs, agent)
        if (
            agent == "policy_agent"
            and latest_handoff is not None
            and _row_generation_key(
                latest_handoff,
                payload_fields=("input_json", "output_json"),
                label="agent_handoffs",
            )
            > _continuation_generation_key(latest_marker)
            and _policy_handoff_has_explicit_allow(latest_handoff)
        ):
            # Policy emits governance rows only for findings.  A newer marked
            # Policy handoff with an explicit allow and no flags is therefore
            # the durable allow generation; retaining an older blocked row as
            # current would mis-project the completed retry.
            continue
        effective.extend(
            row
            for row in agent_rows
            if _same_continuation_generation(
                _row_continuation_marker(
                    row,
                    payload_fields=("flags_json",),
                    label="governance_events",
                ),
                latest_marker,
            )
        )

    return sorted(
        effective,
        key=lambda row: _effective_row_sort_key(
            row,
            primary_key="event_id",
            payload_fields=("flags_json",),
            label="governance_events",
        ),
    )


def _effective_policy_review_rows(
    rows: list[dict[str, Any]],
    *,
    handoffs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    latest = max(
        rows,
        key=lambda row: _effective_row_sort_key(
            row,
            primary_key="policy_review_event_id",
            payload_fields=("evidence_json",),
            label="policy_review_events",
        ),
    )
    latest_marker = _row_continuation_marker(
        latest,
        payload_fields=("evidence_json",),
        label="policy_review_events",
    )
    latest_policy_handoff = _latest_handoff(handoffs, "policy_agent")
    if (
        latest_policy_handoff is not None
        and _row_generation_key(
            latest_policy_handoff,
            payload_fields=("input_json", "output_json"),
            label="agent_handoffs",
        )
        > _continuation_generation_key(latest_marker)
    ):
        # A no-review Policy generation intentionally writes no review row.
        # Historical rows remain available in policyReviews but do not feed
        # the current Policy card.
        return []
    return [
        row
        for row in rows
        if _same_continuation_generation(
            _row_continuation_marker(
                row,
                payload_fields=("evidence_json",),
                label="policy_review_events",
            ),
            latest_marker,
        )
    ]


def _ordered_policy_review_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: _effective_row_sort_key(
            row,
            primary_key="policy_review_event_id",
            payload_fields=("evidence_json",),
            label="policy_review_events",
        ),
    )


def _normalize_policy_review_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = _serializable(dict(raw))
    evidence = _json_object(
        raw.get("evidence_json"),
        "policy_review_events.evidence_json",
        allow_none=True,
    )
    row["evidence"] = _serializable(evidence)
    marker = _row_continuation_marker(
        raw,
        payload_fields=("evidence_json",),
        label="policy_review_events",
    )
    if marker is not None:
        row["continuation"] = _serializable(marker)
    return row


def _policy_handoff_has_explicit_allow(row: Mapping[str, Any]) -> bool:
    output = _json_object(
        row.get("output_json"),
        "policy_agent.output_json",
        allow_none=True,
    )
    governance = _optional_mapping(output.get("governance"))
    action = str(governance.get("interceptor_action") or "").lower()
    return action == "allow" and not list(governance.get("flags") or [])


def _ordered_governance_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: _effective_row_sort_key(
            row,
            primary_key="event_id",
            payload_fields=("flags_json",),
            label="governance_events",
        ),
    )


def _timeline_notes(audit_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # audit_log.log_id is the immutable per-table lifecycle sequence.  Marker
    # generations select *effective* artifacts, but cannot order full history:
    # human_approval_resolved is intentionally unmarked and a child approval
    # may be created after its parent's marked terminal event.
    ordered = sorted(
        audit_rows,
        key=lambda row: (
            _primary_key_sort_value(row.get("log_id")),
            _timestamp_sort_value(row.get("created_at")),
        ),
    )
    return [summarize_audit_row(row) for row in ordered]


def _effective_row_sort_key(
    row: Mapping[str, Any],
    *,
    primary_key: str,
    payload_fields: tuple[str, ...],
    label: str,
    marker_override: Mapping[str, Any] | None = None,
) -> tuple[int, int, int, float, tuple[int, int, str]]:
    marker = (
        dict(marker_override)
        if marker_override is not None
        else _row_continuation_marker(
            row,
            payload_fields=payload_fields,
            label=label,
        )
    )
    generation = _continuation_generation_key(marker)
    return (
        *generation,
        _timestamp_sort_value(row.get("created_at")),
        _primary_key_sort_value(row.get(primary_key)),
    )


def _row_generation_key(
    row: Mapping[str, Any],
    *,
    payload_fields: tuple[str, ...],
    label: str,
) -> tuple[int, int, int]:
    return _continuation_generation_key(
        _row_continuation_marker(
            row,
            payload_fields=payload_fields,
            label=label,
        )
    )


def _row_continuation_marker(
    row: Mapping[str, Any],
    *,
    payload_fields: tuple[str, ...],
    label: str,
) -> dict[str, Any] | None:
    markers: list[dict[str, Any]] = []
    for field in payload_fields:
        payload = _json_object(
            row.get(field),
            f"{label}.{field}",
            allow_none=True,
        )
        marker = payload.get("_continuation")
        if marker is None:
            continue
        if not isinstance(marker, Mapping):
            raise DashboardDataError(f"{label}.{field}._continuation must be an object")
        normalized = dict(marker)
        _validate_continuation_marker(normalized, f"{label}.{field}._continuation")
        markers.append(normalized)

    if not markers:
        return None
    if any(marker != markers[0] for marker in markers[1:]):
        raise DashboardDataError(f"{label} continuation markers disagree")
    return markers[0]


def _audit_continuation_marker(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    marker = payload.get("_continuation")
    if marker is not None:
        if not isinstance(marker, Mapping):
            raise DashboardDataError("audit_log.payload_json._continuation must be an object")
        normalized = dict(marker)
        _validate_continuation_marker(
            normalized,
            "audit_log.payload_json._continuation",
        )
        return normalized

    # The claim row creates the global sequence and therefore cannot embed its
    # own auto-increment id before insertion.  Reconstruct the same marker for
    # timeline ordering from its immutable log id and claim payload.
    if row.get("event_type") == "human_approval_continuation_claimed":
        required = ("approval_id", "claim_token", "attempt")
        if all(payload.get(field) not in (None, "") for field in required):
            normalized = {
                "type": "human_approval",
                "approval_id": payload["approval_id"],
                "claim_token": payload["claim_token"],
                "attempt": payload["attempt"],
                "sequence": row.get("log_id"),
            }
            _validate_continuation_marker(
                normalized,
                "audit_log human approval claim",
            )
            return normalized
    return None


def _validate_continuation_marker(marker: Mapping[str, Any], label: str) -> None:
    continuation_type = marker.get("type")
    if continuation_type not in {"customer_followup", "human_approval"}:
        raise DashboardDataError(f"{label}.type is unsupported")
    for field in ("sequence", "attempt"):
        value = marker.get(field)
        if value is None:
            continue
        if isinstance(value, bool):
            raise DashboardDataError(f"{label}.{field} must be a positive integer")
        try:
            parsed = int(str(value))
        except (TypeError, ValueError) as exc:
            raise DashboardDataError(f"{label}.{field} must be a positive integer") from exc
        if parsed <= 0 or str(parsed) != str(value).strip():
            raise DashboardDataError(f"{label}.{field} must be a positive integer")


def _continuation_generation_key(marker: Mapping[str, Any] | None) -> tuple[int, int, int]:
    if marker is None:
        return (0, 0, 0)
    sequence = _marker_integer(marker, "sequence")
    attempt = _marker_integer(marker, "attempt")
    if sequence is not None:
        # Explicit global sequences form the total order across continuation
        # types.  Attempt orders retries of the same immutable claim sequence.
        return (2, sequence, attempt or 0)
    # Compatibility for customer-followup artifacts created before global
    # sequence markers and for partially upgraded human-approval histories.
    fallback = {"customer_followup": 1, "human_approval": 2}
    return (1, fallback[str(marker.get("type"))], attempt or 0)


def _marker_integer(marker: Mapping[str, Any], field: str) -> int | None:
    value = marker.get(field)
    return int(str(value)) if value is not None else None


def _same_continuation_generation(
    candidate: Mapping[str, Any] | None,
    selected: Mapping[str, Any] | None,
) -> bool:
    if candidate is None or selected is None:
        return candidate is None and selected is None
    return dict(candidate) == dict(selected)


def _timestamp_sort_value(value: Any) -> float:
    parsed = _as_datetime(value)
    if parsed is None:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _primary_key_sort_value(value: Any) -> tuple[int, int, str]:
    if not isinstance(value, bool):
        try:
            numeric = int(str(value))
        except (TypeError, ValueError):
            pass
        else:
            if str(numeric) == str(value).strip():
                return (1, numeric, "")
    return (0, 0, str(value or ""))


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
    if event_type == "response_agent_evaluated":
        output = _optional_mapping(payload.get("output"))
        response = _optional_mapping(output.get("response_result")) or output
        outcome = str(response.get("final_outcome") or "").lower()
        marker = payload.get("_continuation")
        if outcome == "manual_review":
            return "Response recorded that the case is waiting for human review."
        if isinstance(marker, Mapping) and marker.get("type") == "human_approval":
            return "Final Response generated after human approval."
    known = {
        "triage_agent_evaluated": "Triage completed and persisted its handoff.",
        "policy_agent_evaluated": "Policy evaluated the request and persisted its decision.",
        "response_agent_evaluated": "Response generated and persisted the customer-facing result.",
        "refund_execution": "Refund execution completed.",
        "refund_issued": "Refund execution issued the persisted refund.",
        "refund_failed": "Refund execution failed closed; no refund was issued.",
        "governance_block": "Governance blocked the workflow for review.",
        "human_approval_resolved": "A reviewer recorded the human-approval decision.",
        "human_approval_continuation_claimed": "The reviewed workflow continuation was claimed.",
        "human_approval_continued": "The reviewed workflow continuation completed.",
        "human_approval_continuation_failed": "The reviewed workflow continuation failed closed.",
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
