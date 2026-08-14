# Dashboard Guide

What each tab in the AI Governance dashboard shows, where its data comes from, and how it maps to the refund workflow described in `docs/iDOX AI Governance - Proposal.pdf`.

The sidebar groups the 5 tabs into three sections: **PLATFORM** (Overview, Live Monitoring, Violations), **GOVERNANCE** (Approvals), **ADMINISTRATION** (Audit Logs). All tabs poll their endpoint on an interval, there's no websocket push yet.

---

## 1. Overview

**File:** `frontend/src/pages/Dashboard.jsx` · **Refresh:** every 5s
**Data source:** `GET /api/dashboard/stats` + `GET /api/audit-log/query?limit=200`

The landing page. Four stat cards, one chart, one table:

| Card | Meaning |
|---|---|
| Active Workflows | Distinct `trace_id`s seen in the last 1000 audit log rows |
| Governance Events | Audit log rows whose `event_type` contains the word "governance" (currently always near-zero, see gap below) |
| Compliance Rating | `100 - (governance_events / total_audit_entries * 100)`, floored at 0 |
| Pending Approvals | Live count from `human_approvals` where `status = 'pending'` |

Below that: an **Audit Event Volume** area chart (events per day, total vs. high-risk) and a **Risk by Agent** table, both computed client-side from the same 200 audit log rows, risk level per row is inferred from `event_type` (`governance_block` = high, `human_approval` = medium, anything else = low).

**Known gap:** the "Governance Events" stat and the risk classifier both key off `event_type` strings in the audit log (`governance_block`, `human_approval`), but nothing in the current pipeline actually writes those `event_type` values, real governance data lives in the dedicated `governance_events` table instead (see Violations tab below). Until agents/interceptors are wired to also write matching `audit_log` entries, this card and the risk table will under-report.

---

## 2. Live Monitoring

**File:** `frontend/src/pages/Workflows.jsx` · **Refresh:** every 10s
**Data source:** `GET /api/audit-log/query?limit=50`

Lists workflow traces by grouping the last 50 audit log rows by `trace_id`, showing an event count and the earliest timestamp seen per trace. Has a trace-ID search box and a status dropdown (Running/Completed/Failed).

**Known gap:** the status dropdown is wired to component state but never applied to the request or the client-side filter, selecting a status currently does nothing. It's also not reading true status: `workflow_runs.status` (which *does* track running/completed/failed/paused_governance/pending_human) is never queried here; the page infers everything from audit log grouping instead. There's no `GET /api/workflows` (list-all) endpoint yet, only `GET /api/workflows/{trace_id}` (single trace) exists, which is likely why this page works around it via the audit log.

---

## 3. Violations

**File:** `frontend/src/pages/GovernanceEvents.jsx` · **Refresh:** every 5s
**Data source:** `GET /api/governance-events` (filterable by `owasp_category`, `interceptor_action`)

The real governance feed, one row per interceptor check recorded in the `governance_events` table, corresponding to the diagram's three checkpoints (Governance Interceptor after Triage → ASI07, Governance Interceptor after Policy → ASI01, Action Interceptor after Response → ASI02/ASI08). Each card shows:

- OWASP category badge (ASI01/02/06/07/08)
- Agent whose output was checked
- Interceptor action badge: `allow` / `quarantine` / `block`
- Trigger score (e.g. semantic drift score)
- Offending content, the actual text/data that caused the flag
- Raw `flags_json`

The two dropdowns (OWASP category, action) filter server-side via query params.

**Note on naming:** despite being labeled "Violations," this shows *every* interceptor check, including ones the interceptor `allow`ed, not just quarantined/blocked ones. Treat it as a governance audit trail, not a strict violations-only list, unless you filter the Action dropdown to non-`allow`.

---

## 4. Approvals (HITL Queue)

**File:** `frontend/src/pages/HITLQueue.jsx` · **Refresh:** every 3s
**Data source:** `GET /api/approvals/pending` (LEFT JOIN against `governance_events` via `triggering_event_id`)

The human-in-the-loop queue: every `human_approvals` row with `status = 'pending'`. This is where a reviewer actually approves/rejects the Refund Agent's mandatory pause-and-wait (per the proposal, Refund Agent is the only node with `Human Intervention: Yes (Mandatory)`).

Each card shows the free-text `reason`, requested amount, trace ID, and, if the approval has a `triggering_event_id` linking it back to a `governance_events` row, the OWASP category, trigger score, and offending content that caused the escalation, so a reviewer isn't approving/rejecting blind.

Buttons call `PUT /api/approvals/{approval_id}` with `status: approved|rejected`. **The dashboard only records the human's decision, it does not itself gate execution.** The actual pause/resume of the LangGraph run happens in the agent layer, which should poll (or be resumed via callback on) `human_approvals.status`. See the "HITL: dashboard vs. agents" note in `AGENT_INTEGRATION.md` for the split of responsibility.

**Known gap:** `triggering_event_id` is a recent addition (see migration below), approvals created before it was added will show no OWASP context, only the free-text reason.

---

## 5. Audit Logs

**File:** `frontend/src/pages/AuditLog.jsx` · **Refresh:** on filter change only (no polling interval)
**Data source:** `GET /api/audit-log/query` (filterable by `trace_id`, `event_type`)

The raw, append-only compliance trail, every row ever written to `audit_log`, in reverse chronological order, with the full JSON payload rendered inline. This is the lowest-level view; Overview and Live Monitoring are both just different aggregations of this same table. Use this tab when you need to answer "what exactly happened on trace X at time Y" during a live demo.

---

## Schema notes relevant to the dashboard

- `human_approvals.triggering_event_id` (added via migration, nullable FK → `governance_events.event_id`) is what lets the Approvals tab show governance context instead of just a reason string. When creating an approval from an interceptor, pass `triggering_event_id` in the `POST /api/approvals` body to get this linkage.
- MySQL returns `DECIMAL` columns (e.g. `amount_requested`, `trigger_score`) as strings over the JSON API, the frontend must `Number(...)` them before doing arithmetic (`HITLQueue.jsx` does this for `amount_requested`; watch for the same issue anywhere else a decimal field is used numerically).
