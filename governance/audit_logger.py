"""
Audit log writer — remote-first (shared GCP main_db), local SQLite fallback.

Routing: when the mysql backend is active, audit_log / governance_events rows
go to the shared main_db (so the team's Audit tab sees them). When GCP is
unavailable (or DB_BACKEND=sqlite), rows go to the local append-only store
db/idox_triage_outputs_jenny_local.db, whose columns mirror the shared tables
so nothing is lost and later export needs no transformation.

PII policy: `offending_content` stores the RAW value that triggered a verdict
(team decision, matches the shared schema). Treat governance_events as
sensitive (see AGENT_SPEC risk register).

Failure policy: audit writes never raise. Remote failure falls back to local;
local failure warns to stderr. Never blocks the governance verdict.
"""
import json
import sqlite3
import sys
from pathlib import Path

AUDIT_DB_PATH = Path(__file__).parent.parent / "db" / "idox_triage_outputs_jenny_local.db"
_SCHEMA_PATH = Path(__file__).parent.parent / "db" / "audit_schema.sql"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(AUDIT_DB_PATH)
    conn.executescript(_SCHEMA_PATH.read_text())  # idempotent CREATE IF NOT EXISTS
    return conn


def init_audit_db() -> None:
    _connect().close()


def _owasp_category(governance_result: dict) -> str:
    """Content-filter / prompt-injection blocks are ASI01; schema / ownership /
    PII-leak blocks are ASI07."""
    if governance_result.get("failed_check") == "content_filter":
        return "ASI01"
    return governance_result.get("rule", "ASI07")


def _remote_enabled() -> bool:
    try:
        from db import pipeline_store  # lazy: avoid import cycle

        return pipeline_store._remote_enabled()
    except Exception:
        return False


# ── local (SQLite) inserts — also called directly by pipeline_store notes ────

def _local_log_event(trace_id: str, event_type: str, *, agent: str,
                     ticket_id: str | None = None, user_id: str | None = None,
                     payload: dict | None = None) -> None:
    try:
        body = dict(payload) if payload else {}
        if user_id is not None:
            body.setdefault("user_id", user_id)
        payload_json = json.dumps(body) if body else None
        conn = _connect()
        with conn:
            conn.execute(
                "INSERT INTO audit_log (trace_id, ticket_id, event_type, agent, "
                "payload_json) VALUES (?, ?, ?, ?, ?)",
                (trace_id, ticket_id, event_type, agent, payload_json),
            )
        conn.close()
    except Exception as exc:
        print(f"[audit_logger] WARNING: failed to write audit event: {exc}", file=sys.stderr)


# ── public API (signatures unchanged; remote-first, local fallback) ──────────

def log_event(
    trace_id: str,
    event_type: str,
    *,
    agent: str,
    ticket_id: str | None = None,
    user_id: str | None = None,
    payload: dict | None = None,
) -> None:
    body = dict(payload) if payload else {}
    if user_id is not None:
        body.setdefault("user_id", user_id)

    if _remote_enabled():
        try:
            from db import pipeline_store

            # main_db audit_log has no ticket_id column — fold it into payload
            remote_body = dict(body)
            if ticket_id is not None:
                remote_body.setdefault("ticket_id", ticket_id)
            pipeline_store.insert_audit_remote(
                trace_id, event_type, agent,
                json.dumps(remote_body) if remote_body else None)
            return
        except Exception as exc:
            print(f"[audit_logger] WARNING: remote audit write failed, "
                  f"using local store: {exc}", file=sys.stderr)

    _local_log_event(trace_id, event_type, agent=agent, ticket_id=ticket_id,
                     user_id=user_id, payload=payload)


def log_governance_event(
    trace_id: str,
    ticket_id: str | None,
    user_id: str,
    governance_result: dict,
    next_agent: str,
) -> None:
    try:
        flags = {
            "failed_check": governance_result.get("failed_check"),
            "offending_field": governance_result.get("offending_field"),
            "pii_type": governance_result.get("pii_type"),
            "detail": governance_result.get("detail"),
            "next_agent": next_agent,
            "user_id": user_id,
            # main_db governance_events has no ticket_id column — carried here
            "ticket_id": ticket_id,
        }
        owasp = _owasp_category(governance_result)
        action = governance_result["status"]  # allow | block
        offending = governance_result.get("offending_value")  # RAW (see docstring)

        if _remote_enabled():
            try:
                from db import pipeline_store

                pipeline_store.insert_governance_remote(
                    trace_id, "governance_interceptor", owasp,
                    None,  # trigger_score: rule-based interceptor, no score
                    action, json.dumps(flags), offending)
                return
            except Exception as exc:
                print(f"[audit_logger] WARNING: remote governance write failed, "
                      f"using local store: {exc}", file=sys.stderr)

        conn = _connect()
        with conn:
            conn.execute(
                "INSERT INTO governance_events (trace_id, ticket_id, agent, "
                "owasp_category, trigger_score, interceptor_action, flags_json, "
                "offending_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (trace_id, ticket_id, "governance_interceptor", owasp,
                 None, action, json.dumps(flags), offending),
            )
        conn.close()
    except Exception as exc:
        print(f"[audit_logger] WARNING: failed to write governance event: {exc}", file=sys.stderr)
