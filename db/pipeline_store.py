"""
Live writes to the shared GCP main_db pipeline tables.

This module owns ALL MySQL write plumbing for the triage slice:

  - tickets          : created at run start, backfilled after triage
  - workflow_runs    : one row per trace, status/current_agent updated on handoff
  - agent_handoffs   : one snapshot per control transfer (Derrick's JSON shapes)
  - audit_log / governance_events : remote variants used by audit_logger's
    remote-first routing (local SQLite remains the fallback store)

Activation rule: writes fire only when `backend.active_backend() == "mysql"`.
On the SQLite fallback, pipeline writes are skipped with a local audit note
(`pipeline_write_skipped`) — offline tests never touch the network.

Failure policy: every write is fail-open. A failed write warns to stderr and
logs `pipeline_write_failed` locally; it never blocks the pipeline.
"""
import json
import sys
import uuid

from db import backend


def _remote_enabled() -> bool:
    return backend.active_backend() == "mysql"


def _note_local(event_type: str, op: str, detail: str,
                trace_id: str | None, ticket_id: str | None) -> None:
    """Record a skip/failure note in the LOCAL audit store (never raises)."""
    try:
        from governance.audit_logger import _local_log_event  # lazy: avoid cycle

        _local_log_event(trace_id or "system", event_type, agent="pipeline_store",
                         ticket_id=ticket_id, payload={"op": op, "detail": detail[:200]})
    except Exception:
        pass


def _execute(sql: str, params: tuple, *, op: str,
             trace_id: str | None = None, ticket_id: str | None = None) -> bool:
    """Run one INSERT/UPDATE against main_db. Fail-open; returns success."""
    if not _remote_enabled():
        _note_local("pipeline_write_skipped", op, "sqlite_backend", trace_id, ticket_id)
        return False
    try:
        import mysql.connector

        conn = mysql.connector.connect(**backend._mysql_config())
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
            cur.close()
        finally:
            conn.close()
        return True
    except Exception as exc:
        print(f"[pipeline_store] WARNING: {op} failed: {exc}", file=sys.stderr)
        _note_local("pipeline_write_failed", op, str(exc), trace_id, ticket_id)
        return False


# ── pipeline tables ───────────────────────────────────────────────────────────

def ensure_ticket(ticket_id: str, customer_id: str | None, raw_text: str) -> None:
    """Create the ticket on first contact; no-op on later turns (PK IGNORE)."""
    _execute(
        "INSERT IGNORE INTO tickets (ticket_id, customer_id, raw_text, status, "
        "injection_flag) VALUES (%s, %s, %s, 'new', 0)",
        (ticket_id, customer_id, raw_text),
        op="ensure_ticket", ticket_id=ticket_id,
    )


def start_workflow_run(trace_id: str, ticket_id: str) -> None:
    _execute(
        "INSERT IGNORE INTO workflow_runs (trace_id, ticket_id, status, "
        "current_agent, policy_version) VALUES (%s, %s, 'running', "
        "'triage_agent', 'v1.0')",
        (trace_id, ticket_id),
        op="start_workflow_run", trace_id=trace_id, ticket_id=ticket_id,
    )


def update_ticket_triaged(ticket_id: str, *, sanitized_text: str, refund_reason: str,
                          requested_amount: float, currency: str = "USD") -> None:
    _execute(
        "UPDATE tickets SET sanitized_text=%s, refund_reason=%s, "
        "requested_amount=%s, currency=%s, status='triaged', injection_flag=0 "
        "WHERE ticket_id=%s",
        (sanitized_text, refund_reason, requested_amount, currency, ticket_id),
        op="update_ticket_triaged", ticket_id=ticket_id,
    )


def flag_ticket_injection(ticket_id: str) -> None:
    # status='blocked' is a proposal (only 'new'/'triaged' observed on main_db);
    # if the column ever becomes a strict ENUM the write fails open and we still
    # land injection_flag via the fallback UPDATE.
    ok = _execute(
        "UPDATE tickets SET injection_flag=1, status='blocked' WHERE ticket_id=%s",
        (ticket_id,),
        op="flag_ticket_injection", ticket_id=ticket_id,
    )
    if not ok and _remote_enabled():
        _execute(
            "UPDATE tickets SET injection_flag=1 WHERE ticket_id=%s",
            (ticket_id,),
            op="flag_ticket_injection_minimal", ticket_id=ticket_id,
        )


def record_handoff(trace_id: str, ticket_id: str, *, from_agent: str, to_agent: str,
                   input_json: dict, output_json: dict,
                   input_tokens: int | None, output_tokens: int | None) -> None:
    # handoff_id is varchar(36) with no default on main_db — we mint the UUID.
    _execute(
        "INSERT INTO agent_handoffs (handoff_id, trace_id, ticket_id, from_agent, "
        "to_agent, input_json, output_json, input_tokens, output_tokens) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (str(uuid.uuid4()), trace_id, ticket_id, from_agent, to_agent,
         json.dumps(input_json), json.dumps(output_json),
         input_tokens, output_tokens),
        op="record_handoff", trace_id=trace_id, ticket_id=ticket_id,
    )


def update_workflow_run(trace_id: str, *, status: str, current_agent: str) -> None:
    _execute(
        "UPDATE workflow_runs SET status=%s, current_agent=%s WHERE trace_id=%s",
        (status, current_agent, trace_id),
        op="update_workflow_run", trace_id=trace_id,
    )


# ── remote audit variants (called by audit_logger; these RAISE on failure so
#    the caller can fall back to the local store) ─────────────────────────────

# NOTE: main_db's audit_log / governance_events have NO ticket_id column
# (unlike the local mirror) — callers fold ticket_id into payload/flags JSON.

def insert_audit_remote(trace_id: str, event_type: str,
                        agent: str, payload_json: str | None) -> None:
    import mysql.connector

    conn = mysql.connector.connect(**backend._mysql_config())
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_log (trace_id, event_type, agent, payload_json) "
            "VALUES (%s, %s, %s, %s)",
            (trace_id, event_type, agent, payload_json),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def insert_governance_remote(trace_id: str, agent: str,
                             owasp_category: str, trigger_score: float | None,
                             interceptor_action: str, flags_json: str,
                             offending_content: str | None) -> None:
    import mysql.connector

    conn = mysql.connector.connect(**backend._mysql_config())
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO governance_events (event_id, trace_id, agent, "
            "owasp_category, trigger_score, interceptor_action, flags_json, "
            "offending_content) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), trace_id, agent, owasp_category, trigger_score,
             interceptor_action, flags_json, offending_content),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


# ── handoff JSON builders (pure, unit-testable) ──────────────────────────────

def build_handoff_input_json(state: dict) -> dict:
    """Derrick's input shape: nested case + conversation_history."""
    case = state.get("case") or {}
    return {
        "case": {
            "message": state.get("message", case.get("message")),
            "user_id": state.get("user_id", case.get("user_id")),
            "buggy_db": bool(state.get("buggy_db", case.get("buggy_db", False))),
        },
        "conversation_history": [],
    }


def build_handoff_output_json(state: dict) -> dict:
    """Derrick's output shape: triage_output parts + turn metadata.

    conversation_history is flattened to plain strings (as in his seeded rows).
    """
    t = state.get("triage_output") or {}
    history = [m["content"] for m in state.get("conversation_history") or []
               if isinstance(m, dict) and "content" in m]
    return {
        "case": t.get("case", {}),
        "order_facts": t.get("order_facts", {}),
        "customer_request": t.get("customer_request", {}),
        "awaiting_order_id": bool(state.get("awaiting_order_id", False)),
        "conversation_history": history,
        "clarification_question": state.get("clarification_question"),
    }
