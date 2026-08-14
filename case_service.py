"""
Case aggregation layer for the Refund Ops Console.

Takes the raw, normalized rows out of DBService (tickets, orders, workflow_runs,
agent_handoffs, governance_events, human_approvals, refund_transactions, audit_log)
and shapes them into the "case" view the frontend renders: queue rows, full case
detail, governance incidents, and console-wide metrics.

Real pipelines don't always populate every field (e.g. a case that never reached
the Policy Agent has no policy_evaluation JSON yet). Where a field is genuinely
absent because of a logging gap rather than "not reached yet", this module fills
in a clearly-labeled best-effort placeholder instead of leaving the UI blank.
"""

import json
from datetime import datetime, timezone

NODE_LABELS = [
    "User Input",
    "Triage Agent",
    "Triage Governance",
    "Policy Agent",
    "Policy Governance",
    "Human Approval",
    "Refund Agent",
    "Response Agent",
]

OWASP_LABELS = {
    "ASI01": "Goal Hijack / Drift",
    "ASI02": "Tool Misuse",
    "ASI06": "Prompt Injection",
    "ASI07": "Data Leakage",
    "ASI08": "Excessive Autonomy",
}

STATUS_ORDER = ["quarantined", "manual_review", "needs_info", "pending_review", "auto_approved", "rejected"]


def _parse_json(raw):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _num(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _humanize(s):
    if not s:
        return "-"
    return str(s).replace("_", " ").strip().title()


def _format_duration(seconds):
    seconds = max(0, seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days >= 1:
        return f"{int(days)}d {int(hours)}h"
    if hours >= 1:
        return f"{int(hours)}h {int(mins)}m"
    return f"{int(mins)}m {int(secs)}s"


def _relative_time(dt):
    if dt is None:
        return "-"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    now = datetime.now()
    delta = now - dt
    secs = delta.total_seconds()
    if secs < 0:
        secs = 0
    if secs < 60:
        return "just now"
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = int(mins // 60)
    if hours < 24:
        return f"{hours}h ago"
    days = int(hours // 24)
    return f"{days}d ago"


class CaseAggregator:
    """Builds queue rows / case detail / incidents / metrics from raw DB rows."""

    # ---------- handoff helpers ----------

    @staticmethod
    def _policy_handoff(handoffs):
        """Most recent policy_agent output carries policy_evaluation/decision/governance."""
        candidates = [h for h in handoffs if h.get("from_agent") == "policy_agent"]
        if not candidates:
            return None
        candidates.sort(key=lambda h: h.get("created_at") or "")
        return candidates[-1]

    @staticmethod
    def _triage_handoff(handoffs):
        candidates = [h for h in handoffs if h.get("from_agent") == "triage_agent"]
        if not candidates:
            return None
        candidates.sort(key=lambda h: h.get("created_at") or "")
        return candidates[-1]

    @staticmethod
    def _has_from(handoffs, agent):
        return any(h.get("from_agent") == agent for h in handoffs)

    # ---------- status / pipeline / risk ----------

    @classmethod
    def derive_status(cls, workflow, handoffs, governance_events, approvals, refunds, audit_log):
        blocked_events = [g for g in governance_events if g.get("interceptor_action") in ("block", "quarantine")]
        pending_approvals = [a for a in approvals if a.get("status") == "pending"]
        overridden = any(a.get("event_type") == "quarantine_override" for a in audit_log)
        issued_refund = any(r.get("status") == "issued" for r in refunds)
        blocked_refund = any(r.get("status") in ("failed", "blocked") for r in refunds)
        rejected_approval = any(a.get("status") == "rejected" and a.get("decision") != "request_info" for a in approvals)
        requested_info_approval = any(a.get("status") == "rejected" and a.get("decision") == "request_info" for a in approvals)

        policy_handoff = cls._policy_handoff(handoffs)
        decision = None
        if policy_handoff:
            out = _parse_json(policy_handoff.get("output_json")) or {}
            decision = (out.get("decision") or {}).get("type")

        if blocked_events and pending_approvals and not overridden:
            return "quarantined"
        if pending_approvals:
            triggering_types = {a.get("triggering_event_type") for a in pending_approvals}
            if "governance" in triggering_types or overridden or decision == "manual_review":
                return "manual_review"
            return "pending_review"
        if requested_info_approval or decision == "request_info" or workflow.get("status") == "waiting_user":
            return "needs_info"
        if rejected_approval or blocked_refund or decision == "deny":
            return "rejected"
        if issued_refund or decision == "approve":
            return "auto_approved"
        if decision == "manual_review":
            return "manual_review"
        if workflow.get("status") == "failed":
            return "rejected"
        return "pending_review"

    @staticmethod
    def _governance_agent_label(agent):
        """Display name for the stage a governance check ran after."""
        if agent == "triage_agent":
            return "Triage Governance"
        if agent == "policy_agent":
            return "Policy Governance"
        if not agent:
            return "Governance Interceptor"
        label = _humanize(agent)
        return label if "governance" in label.lower() else f"{label} Governance"

    @classmethod
    def derive_status_source(cls, status, workflow, handoffs, governance_events, approvals, refunds):
        """Which agent/stage produced the case's current status - shown as a
        hover tooltip on the status badge so a reviewer can tell at a glance
        whether e.g. a rejection came from the Policy Agent or a human."""
        blocked_events = sorted(
            [g for g in governance_events if g.get("interceptor_action") in ("block", "quarantine")],
            key=lambda g: g.get("created_at") or "",
        )
        pending_approvals = [a for a in approvals if a.get("status") == "pending"]
        rejected_approval = any(a.get("status") == "rejected" and a.get("decision") != "request_info" for a in approvals)
        requested_info_approval = any(a.get("status") == "rejected" and a.get("decision") == "request_info" for a in approvals)
        issued_refund = any(r.get("status") == "issued" for r in refunds)
        blocked_refund = any(r.get("status") in ("failed", "blocked") for r in refunds)

        policy_handoff = cls._policy_handoff(handoffs)
        decision = None
        if policy_handoff:
            out = _parse_json(policy_handoff.get("output_json")) or {}
            decision = (out.get("decision") or {}).get("type")

        if status == "quarantined":
            return cls._governance_agent_label(blocked_events[-1].get("agent")) if blocked_events else "Governance Interceptor"

        if status == "manual_review":
            triggering_types = {a.get("triggering_event_type") for a in pending_approvals}
            if "governance" in triggering_types and blocked_events:
                return cls._governance_agent_label(blocked_events[-1].get("agent"))
            return "Policy Agent"

        if status == "needs_info":
            if requested_info_approval:
                return "Human Reviewer"
            if decision == "request_info":
                return "Policy Agent"
            return "Triage Agent"

        if status == "rejected":
            if rejected_approval:
                return "Human Reviewer"
            if blocked_refund:
                return "Refund Agent"
            if decision == "deny":
                return "Policy Agent"
            return "System"

        if status == "auto_approved":
            return "Refund Agent" if issued_refund else "Policy Agent"

        # pending_review: whichever agent the case is currently waiting on
        if not cls._has_from(handoffs, "triage_agent"):
            return "Triage Agent"
        if not policy_handoff:
            return "Policy Agent"
        return "System"

    @classmethod
    def build_risk_tag(cls, governance_events):
        flagged = [g for g in governance_events if g.get("interceptor_action") in ("block", "quarantine")]
        if not flagged:
            return None
        categories = []
        for g in flagged:
            cat = g.get("owasp_category")
            if cat and cat not in categories:
                categories.append(cat)
        code = " + ".join(categories) if categories else "GOVERNANCE"
        label = " / ".join(OWASP_LABELS.get(c, c) for c in categories) if categories else "Governance Hold"
        return {"code": code, "label": label}

    @classmethod
    def build_pipeline(cls, workflow, handoffs, governance_events, policy_handoff, approvals, refunds):
        """Maps case data onto the state-driven pipeline stages (see README):
        Triage Agent -> Triage Governance -> Policy Agent -> Policy Governance ->
        (Human Approval | Refund Agent) -> Response Agent.

        This branch's backend (main.py/db tables) predates that architecture, so
        stage membership is inferred from agent_handoffs + governance_events
        rather than a real `current_stage`/`policy_decision` state object.
        """
        policy_done = cls._has_from(handoffs, "policy_agent")
        response_done = cls._has_from(handoffs, "response_agent")

        triage_gov_blocked = next(
            (g for g in governance_events if g.get("interceptor_action") in ("block", "quarantine")
             and not policy_done),
            None,
        )

        triage_done = cls._has_from(handoffs, "triage_agent") or bool(triage_gov_blocked)

        gov = {}
        decision_type = None
        if policy_handoff:
            out = _parse_json(policy_handoff.get("output_json")) or {}
            gov = out.get("governance") or {}
            decision_type = (out.get("decision") or {}).get("type")
        gov_action = gov.get("interceptor_action", "allow")
        policy_gov_blocked = policy_done and gov_action in ("block", "quarantine")

        # Only "approve"/"partial_refund" policy decisions route to Refund Agent;
        # "deny"/"request_info" go straight to Response, "manual_review" (and any
        # governance block) routes to Human Approval instead. A manual_review
        # case still reaches Refund Agent if the human reviewer's own decision
        # was approve/partial_refund (policy_decision.type stays "manual_review"
        # even after resolution) - check the approval's `decision` and any
        # actual refund_transactions row too, not just the policy decision.
        human_decision_refunded = any(a.get("decision") in ("approve", "partial_refund") for a in approvals)
        refund_expected = decision_type in ("approve", "partial_refund") or human_decision_refunded or bool(refunds)

        approval_exists = bool(approvals)
        human_approval_triggered = (
            (bool(triage_gov_blocked) or policy_gov_blocked) and approval_exists
        ) or decision_type == "manual_review"


        pending_approval = any(a.get("status") == "pending" for a in approvals)
        human_decision_no_refund = any(a.get("decision") in ("deny", "request_info") for a in approvals)
        refund_skipped = not refund_expected and not pending_approval and (
            decision_type in ("deny", "request_info") or human_decision_no_refund
        )

        refund_state = "pending"
        if workflow.get("status") == "completed":
            refund_state = "done"
        elif workflow.get("status") == "failed":
            refund_state = "blocked"

        human_resolved = response_done or refund_state in ("done", "blocked") or refund_skipped

        human_pending = human_approval_triggered and not human_resolved

        states = ["done"]  # User Input always exists once a case row exists
        states.append("done" if triage_done else "current")
        states.append("blocked" if triage_gov_blocked else ("done" if triage_done else "pending"))
        states.append("done" if policy_done else ("current" if triage_done and not triage_gov_blocked else "pending"))
        states.append("blocked" if policy_gov_blocked else ("done" if policy_done else "pending"))
        if human_approval_triggered:
            states.append("current" if human_pending else "done")
        elif decision_type in ("deny", "request_info"):
            # Policy Agent denied/needs-info directly - routes straight to
            # Response Agent, no human ever in the loop. Same muted-red
            # treatment as Refund Agent, not a neutral "never reached" grey.
            states.append("skipped")
        else:
            states.append("pending")  # this case's path never needed a human
        if refund_skipped:
            states.append("skipped")
        else:
            states.append(refund_state if refund_expected else "pending")
        states.append("done" if response_done else "pending")

        already_active = any(s in ("current", "blocked") for s in states)
        if not already_active:
            for idx in (1, 3, 5, 6, 7):
                if idx == 5 and not human_approval_triggered:
                    continue
                if idx == 6 and not refund_expected:
                    continue
                if states[idx] == "pending":
                    states[idx] = "current"
                    break

        color = {"done": "oklch(0.5 0.15 150)", "current": "oklch(0.55 0.15 80)",
                 "blocked": "oklch(0.5 0.19 25)", "skipped": "oklch(0.5 0.19 25 / 0.55)",
                 "pending": "oklch(0.85 0.01 90)"}
        nodes = []
        for i, label in enumerate(NODE_LABELS):
            st = states[i]
            nodes.append({
                "label": label,
                "state": st,
                "color": color[st],
                "lineColor": "oklch(0.5 0.15 150 / 0.4)" if st == "done" else "oklch(0.88 0.01 90)",
                "hasNext": i < len(NODE_LABELS) - 1,
            })
        return nodes

    # ---------- customer request / order facts ----------

    @staticmethod
    def _order_facts(order_row, triage_handoff):
        if triage_handoff:
            out = _parse_json(triage_handoff.get("output_json")) or {}
            facts = out.get("order_facts")
            if facts:
                return {
                    "orderId": facts.get("order_id", "-"),
                    "productType": _humanize(facts.get("product_type")),
                    "purchaseDate": (facts.get("purchase_date") or "-")[:10],
                    "itemStatus": _humanize(facts.get("item_status")),
                    "amountPaid": _num(facts.get("amount_paid")),
                    "priorRefundTotal": _num(facts.get("prior_refund_total")),
                }
        if order_row:
            return {
                "orderId": order_row.get("order_id", "-"),
                "productType": _humanize(order_row.get("product_type")),
                "purchaseDate": str(order_row.get("purchase_date") or "-")[:10],
                "itemStatus": _humanize(order_row.get("item_status")),
                "amountPaid": _num(order_row.get("amount_paid")),
                "priorRefundTotal": _num(order_row.get("prior_refund_total")),
            }
        return {
            "orderId": "-", "productType": "-", "purchaseDate": "-",
            "itemStatus": "Unknown", "amountPaid": 0.0, "priorRefundTotal": 0.0,
        }

    @staticmethod
    def _customer_request(ticket, triage_handoff):
        text = ticket.get("sanitized_text") or ticket.get("raw_text") or ""
        amount = ticket.get("requested_amount")
        reason = ticket.get("refund_reason")
        if triage_handoff:
            out = _parse_json(triage_handoff.get("output_json")) or {}
            cr = out.get("customer_request") or {}
            text = cr.get("sanitized_text") or text
            amount = amount if amount is not None else cr.get("requested_amount")
            reason = reason or cr.get("refund_reason")
        return {
            "sanitizedText": text or "(no text captured)",
            "requestedAmount": _num(amount),
        }, (reason or "unknown")

    # ---------- policy evaluation ----------

    @classmethod
    def _policy_section(cls, policy_handoff):
        if not policy_handoff:
            return (
                {"matchedPolicies": [], "gaps": [],
                 "decision": {"type": "Pending", "reasonText": "Awaiting Policy Agent evaluation.", "confidence": "-"}},
                False,
            )
        out = _parse_json(policy_handoff.get("output_json")) or {}
        pe = out.get("policy_evaluation") or {}
        decision = out.get("decision") or {}

        matched = []
        for p in pe.get("matched_policies") or []:
            matched.append({
                "id": p.get("policy_id", "-"),
                "summary": p.get("rule_summary", "-"),
                "effect": _humanize(p.get("effect")),
            })
        if not matched and decision:
            matched.append({
                "id": "-",
                "summary": decision.get("reason") or "No specific policy rule matched; evaluated on general criteria.",
                "effect": "General evaluation",
            })

        gaps = []
        for g in pe.get("gaps_or_conflicts") or []:
            if isinstance(g, dict):
                gaps.append({"type": _humanize(g.get("type", "Note")), "detail": g.get("detail") or g.get("description") or json.dumps(g)})
            else:
                gaps.append({"type": "Note", "detail": str(g)})

        confidence = decision.get("confidence_level")
        if confidence:
            confidence = confidence.capitalize()
        elif decision.get("confidence") is not None:
            confidence = str(decision.get("confidence"))
        else:
            confidence = "-"

        return (
            {
                "matchedPolicies": matched,
                "gaps": gaps,
                "decision": {
                    "type": _humanize(decision.get("type", "pending")),
                    "amount": _num(decision.get("refund_amount")),
                    "confidence": confidence,
                    "reasonText": decision.get("reason") or "No reasoning recorded.",
                },
            },
            len(gaps) > 0,
        )

    # ---------- governance panel ----------

    @classmethod
    def _governance_section(cls, policy_handoff, governance_events):
        score = None
        action = None
        flags = []
        if policy_handoff:
            out = _parse_json(policy_handoff.get("output_json")) or {}
            gov = out.get("governance") or {}
            score = gov.get("trigger_score")
            action = gov.get("interceptor_action")
            flags = list(gov.get("flags") or [])

        if score is None:
            scored = [g.get("trigger_score") for g in governance_events if g.get("trigger_score") is not None]
            score = max(_num(s) for s in scored) if scored else 0.0
        if action is None:
            severity = {"block": 3, "quarantine": 2, "allow": 1}
            worst = max(governance_events, key=lambda g: severity.get(g.get("interceptor_action"), 0), default=None)
            action = worst.get("interceptor_action") if worst else "allow"
        if not flags:
            flags = list({g.get("owasp_category") for g in governance_events if g.get("interceptor_action") != "allow" and g.get("owasp_category")})

        offending_text = next((g.get("offending_content") for g in governance_events if g.get("offending_content")), None)

        pii_flag = None
        for g in governance_events:
            f = _parse_json(g.get("flags_json")) or {}
            if f.get("pii_type") or f.get("failed_check") in ("ownership", "pii_scan"):
                pii_flag = {
                    "field": f.get("offending_field") or "customer record",
                    "note": f.get("detail") or "A data ownership or PII mismatch was detected in this case's records.",
                }
                break

        return {
            "triggerScore": round(_num(score), 2),
            "action": action or "allow",
            "flags": flags,
            "offendingText": offending_text,
            "piiFlag": pii_flag,
        }

    # ---------- refund outcome ----------

    @staticmethod
    def _refund_section(refunds, requested_amount):
        """What actually got issued/failed/blocked - distinct from the amount
        the customer originally requested, so a partial refund shows as
        partial instead of looking identical to a full approval."""
        if not refunds:
            return None
        latest = sorted(refunds, key=lambda r: r.get("created_at") or "")[-1]
        amount = _num(latest.get("amount"))
        return {
            "amount": amount,
            "status": latest.get("status"),
            "currency": latest.get("currency") or "USD",
            "externalRef": latest.get("external_ref"),
            "isPartial": latest.get("status") == "issued" and requested_amount and amount < requested_amount,
        }

    # ---------- timeline ----------

    EVENT_TEXT = {
        "run_started": "Workflow started.",
        "order_lookup_performed": "Looked up order record.",
        "classification_completed": "Classified the refund reason.",
        "triage_output_ready": "Triage output ready, handed off to Policy Agent.",
        "interceptor_allow": "Interceptor allowed the handoff to proceed.",
        "interceptor_block": "Interceptor blocked the handoff.",
        "llm_content_filtered": "Content filter blocked suspected prompt injection.",
        "policy_agent_evaluated": "Policy Agent evaluated the request.",
        "handoff_ready": "Handoff ready for the next agent.",
        "refund_execution": "Refund transaction processed.",
        "quarantine_override": "Admin overrode the quarantine hold.",
    }

    @classmethod
    def _note_text(cls, row):
        event_type = row.get("event_type", "")
        payload = _parse_json(row.get("payload_json")) or {}
        base = cls.EVENT_TEXT.get(event_type, _humanize(event_type))
        if event_type == "order_lookup_performed" and payload.get("order_id"):
            base = f"Looked up order {payload['order_id']}."
        elif event_type == "classification_completed" and payload.get("refund_reason"):
            base = f"Classified refund reason as {_humanize(payload['refund_reason'])}."
        elif event_type == "interceptor_block" and payload.get("failed_check"):
            base = f"Interceptor blocked the handoff - failed check: {_humanize(payload['failed_check'])}."
        elif event_type == "interceptor_allow" and payload.get("checks_passed"):
            base = f"Interceptor allowed the handoff - passed: {', '.join(payload['checks_passed'])}."
        elif event_type == "refund_execution":
            amt = payload.get("amount")
            status = payload.get("status", "processed")
            base = f"Refund {status}" + (f" for ${_num(amt):.2f}." if amt is not None else ".")
        elif event_type == "policy_agent_evaluated":
            output = payload.get("output") or {}
            decision = output.get("decision") or {}
            handoff = output.get("handoff") or {}
            if decision.get("type"):
                base = f"Policy Agent decided: {_humanize(decision['type'])}. {handoff.get('reason') or decision.get('reason') or ''}".strip()
        elif event_type.startswith("admin_"):
            action_label = _humanize(event_type.replace("admin_", ""))
            note_text = payload.get("notes")
            base = f"Admin action: {action_label}." + (f" {note_text}" if note_text else "")
        elif row.get("note_text"):
            base = row["note_text"]
        return base

    CATEGORY_BY_PREFIX = [
        ("admin_", "Admin"),
        ("quarantine_override", "Admin"),
        ("interceptor_", "Governance"),
        ("llm_content_filtered", "Governance"),
        ("policy_agent_evaluated", "Policy"),
        ("classification_completed", "Triage"),
        ("order_lookup_performed", "Triage"),
        ("triage_output_ready", "Triage"),
        ("refund_execution", "Refund"),
        ("run_started", "System"),
        ("handoff_ready", "System"),
    ]

    @classmethod
    def categorize(cls, event_type):
        event_type = event_type or ""
        for prefix, category in cls.CATEGORY_BY_PREFIX:
            if event_type.startswith(prefix):
                return category
        return "System"

    @classmethod
    def build_notes(cls, audit_rows):
        notes = []
        for row in sorted(audit_rows, key=lambda r: r.get("created_at") or "", reverse=True):
            agent = row.get("agent")
            if agent and "_" in agent and agent.islower():
                actor = _humanize(agent)
            else:
                actor = agent or "System"
            notes.append({
                "time": _relative_time(row.get("created_at")),
                "actor": actor,
                "text": cls._note_text(row),
            })
        if not notes:
            notes = [{"time": "-", "actor": "System", "text": "No timeline events recorded yet."}]
        return notes

    # ---------- top-level builders ----------

    @classmethod
    def build_case_summary(cls, bundle):
        workflow = bundle["workflow"]
        ticket = bundle["ticket"] or {}
        customer = bundle["customer"] or {}
        handoffs = bundle["handoffs"]
        governance_events = bundle["governance_events"]
        approvals = bundle["approvals"]
        refunds = bundle["refunds"]
        audit_log = bundle["audit_log"]

        request, reason = cls._customer_request(ticket, cls._triage_handoff(handoffs))
        status = cls.derive_status(workflow, handoffs, governance_events, approvals, refunds, audit_log)
        status_source = cls.derive_status_source(status, workflow, handoffs, governance_events, approvals, refunds)
        risk_tag = cls.build_risk_tag(governance_events)
        updated = max(
            [t for t in [ticket.get("updated_at"), workflow.get("updated_at")] if t],
            default=workflow.get("started_at"),
        )

        return {
            "id": ticket.get("ticket_id", workflow["trace_id"]),
            "traceId": workflow["trace_id"],
            "customer": customer.get("full_name") or ticket.get("customer_id") or "Unknown Customer",
            "summary": (request["sanitizedText"] or "")[:140],
            "reason": reason,
            "reasonLabel": _humanize(reason),
            "amount": request["requestedAmount"],
            "currency": ticket.get("currency") or "USD",
            "status": status,
            "statusSource": status_source,
            "riskTag": risk_tag,
            "updated": _relative_time(updated),
            "request": request,
        }

    @classmethod
    def build_case_detail(cls, bundle):
        summary = cls.build_case_summary(bundle)
        workflow = bundle["workflow"]
        ticket = bundle["ticket"] or {}
        handoffs = bundle["handoffs"]
        governance_events = bundle["governance_events"]
        approvals = bundle["approvals"]
        audit_log = bundle["audit_log"]

        triage_handoff = cls._triage_handoff(handoffs)
        policy_handoff = cls._policy_handoff(handoffs)

        order = cls._order_facts(bundle.get("order"), triage_handoff)
        policy, has_gaps = cls._policy_section(policy_handoff)
        governance = cls._governance_section(policy_handoff, governance_events)
        pipeline = cls.build_pipeline(workflow, handoffs, governance_events, policy_handoff, approvals, bundle["refunds"])
        notes = cls.build_notes(audit_log)
        refund = cls._refund_section(bundle["refunds"], summary["amount"])

        detail = dict(summary)
        detail.update({
            "order": order,
            "policy": policy,
            "hasGaps": has_gaps,
            "governance": governance,
            "hasFlags": len(governance["flags"]) > 0,
            "pipeline": pipeline,
            "notes": notes,
            "refund": refund,
            "pendingApprovalId": next((a.get("approval_id") for a in approvals if a.get("status") == "pending"), None),
            "policyVersion": workflow.get("policy_version") or "v1.0",
        })
        return detail

    @classmethod
    def build_incidents(cls, cases):
        return [c for c in cases if c.get("riskTag")]

    @classmethod
    def build_metrics(cls, all_cases, audit_log_rows, approvals_rows, governance_rows):
        total = len(all_cases) or 1
        auto = sum(1 for c in all_cases if c["status"] == "auto_approved")
        quarantined = sum(1 for c in all_cases if c["status"] == "quarantined")
        review = sum(1 for c in all_cases if c["status"] in ("pending_review", "manual_review"))
        rejected = sum(1 for c in all_cases if c["status"] == "rejected")

        blocking_events = [g for g in governance_rows if g.get("interceptor_action") in ("block", "quarantine")]
        total_gov_checks = len(governance_rows) or 1
        injection_events = [g for g in blocking_events if g.get("owasp_category") in ("ASI01", "ASI02", "ASI06")]

        resolved = [a for a in approvals_rows if a.get("resolved_at") and a.get("created_at")]
        review_seconds = []
        for a in resolved:
            try:
                created = a["created_at"] if isinstance(a["created_at"], datetime) else datetime.fromisoformat(str(a["created_at"]))
                resolved_at = a["resolved_at"] if isinstance(a["resolved_at"], datetime) else datetime.fromisoformat(str(a["resolved_at"]))
                review_seconds.append((resolved_at - created).total_seconds())
            except (ValueError, TypeError):
                continue
        if review_seconds:
            avg_s = sum(review_seconds) / len(review_seconds)
            avg_review = _format_duration(avg_s)
        else:
            avg_review = "-"

        primary_stats = [
            {"label": "Operational Expense", "value": "Reduced" if quarantined + review < total else "High",
             "detail": f"{quarantined} of {total} cases required governance intervention"},
            {"label": "PII Leakage Risk", "value": "Low" if len(blocking_events) <= 2 else "Elevated",
             "detail": f"{len(blocking_events)} interceptor holds across {total_gov_checks} checks"},
            {"label": "System Auditability", "value": "Traceable",
             "detail": f"{len(audit_log_rows)} audit events logged across all cases"},
            {"label": "Avg Handling Time", "value": "Instant" if auto else "N/A",
             "detail": f"{auto} of {total} cases resolved without human intervention"},
        ]
        secondary_stats = [
            {"label": "Injection/Drift Detection Holds", "value": str(len(injection_events))},
            {"label": "Auto-Resolution Rate", "value": f"{round(auto / total * 100)}%"},
            {"label": "Claims Needing Review", "value": f"{round(review / total * 100)}%"},
            {"label": "Avg Human Review Time", "value": avg_review},
        ]

        status_breakdown = [
            {"status": s, "count": sum(1 for c in all_cases if c["status"] == s)}
            for s in ("auto_approved", "manual_review", "needs_info", "pending_review", "quarantined", "rejected")
        ]
        owasp_breakdown = [
            {"category": code, "label": label, "count": sum(1 for g in blocking_events if g.get("owasp_category") == code)}
            for code, label in OWASP_LABELS.items()
        ]

        return {
            "primaryStats": primary_stats,
            "secondaryStats": secondary_stats,
            "statusBreakdown": status_breakdown,
            "owaspBreakdown": owasp_breakdown,
        }


def summarize_audit_row(row):
    """Human-readable summary + category for one audit_log row, for the Audit Log page."""
    agent = row.get("agent")
    if agent and "_" in agent and agent.islower():
        actor = _humanize(agent)
    else:
        actor = agent or "System"
    return {
        "summary": CaseAggregator._note_text(row),
        "category": CaseAggregator.categorize(row.get("event_type")),
        "actor": actor,
        "relativeTime": _relative_time(row.get("created_at")),
    }


def _row_customer(row):
    if row.get("full_name"):
        return {"full_name": row.get("full_name"), "email": row.get("email")}
    return None


def _row_ticket(row):
    return {
        "ticket_id": row.get("ticket_id"),
        "customer_id": row.get("t_customer_id"),
        "raw_text": row.get("raw_text"),
        "sanitized_text": row.get("sanitized_text"),
        "refund_reason": row.get("refund_reason"),
        "requested_amount": row.get("requested_amount"),
        "currency": row.get("currency"),
        "status": row.get("ticket_status"),
        "injection_flag": row.get("injection_flag"),
        "created_at": row.get("ticket_created_at"),
        "updated_at": row.get("ticket_updated_at"),
    }


def build_all_case_bundles(db, limit=200):
    """Bulk-load every workflow run plus its children in a handful of queries
    (instead of N+1 per case) and return one bundle dict per case."""
    runs = db.list_workflow_runs(limit=limit)
    trace_ids = [r["trace_id"] for r in runs]
    handoffs_by_trace = db.bulk_handoffs(trace_ids)
    governance_by_trace = db.bulk_governance_events(trace_ids)
    approvals_by_trace = db.bulk_approvals(trace_ids)
    refunds_by_trace = db.bulk_refund_transactions(trace_ids)
    audit_by_trace = db.bulk_audit_log(trace_ids)

    bundles = []
    for row in runs:
        trace_id = row["trace_id"]
        bundles.append({
            "workflow": row,
            "ticket": _row_ticket(row),
            "customer": _row_customer(row),
            "order": None,  # queue rows use the handoff-embedded order_facts only; detail view fetches the real order
            "handoffs": handoffs_by_trace.get(trace_id, []),
            "governance_events": governance_by_trace.get(trace_id, []),
            "approvals": approvals_by_trace.get(trace_id, []),
            "refunds": refunds_by_trace.get(trace_id, []),
            "audit_log": audit_by_trace.get(trace_id, []),
        })
    return bundles


def build_case_list(db, limit=200):
    bundles = build_all_case_bundles(db, limit=limit)
    return [CaseAggregator.build_case_summary(b) for b in bundles]
