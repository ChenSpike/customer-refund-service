"""
Offline tests for db/pipeline_store.py: Derrick-shaped JSON builders,
sqlite-skip behavior, mysql fail-open, and remote-first audit routing.
conftest pins DB_BACKEND=sqlite and isolates the local audit DB per test.
"""
import sqlite3

import pytest

from db import backend, pipeline_store
from governance import audit_logger


def _local_rows(sql: str, params: tuple = ()) -> list[tuple]:
    conn = audit_logger._connect()  # creates the schema if the DB doesn't exist yet
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class TestHandoffJsonBuilders:
    def test_input_json_matches_derrick_shape_flat_and_nested(self):
        flat = {"user_id": "CUST-POL-001", "message": "Broken keyboard ORD-POL-001",
                "buggy_db": False}
        nested = {"case": {"user_id": "CUST-POL-001",
                           "message": "Broken keyboard ORD-POL-001",
                           "buggy_db": False}}
        expected = {"case": {"message": "Broken keyboard ORD-POL-001",
                             "user_id": "CUST-POL-001", "buggy_db": False},
                    "conversation_history": []}
        assert pipeline_store.build_handoff_input_json(flat) == expected
        assert pipeline_store.build_handoff_input_json(nested) == expected

    def test_output_json_matches_derrick_shape(self):
        state = {
            "triage_output": {
                "case": {"trace_id": "T1", "ticket_id": "K1",
                         "goal": "evaluate refund eligibility", "policy_version": "v1.0"},
                "customer_request": {"sanitized_text": "x", "refund_reason": "damaged",
                                     "requested_amount": 79.99, "currency": "USD"},
                "order_facts": {"order_id": "ORD-POL-001"},
            },
            "awaiting_order_id": False,
            "conversation_history": [
                {"role": "user", "content": "Broken keyboard ORD-POL-001"},
                {"role": "assistant", "content": '{"refund_reason": "damaged"}'},
            ],
        }
        out = pipeline_store.build_handoff_output_json(state)
        assert set(out) == {"case", "order_facts", "customer_request",
                            "awaiting_order_id", "conversation_history",
                            "clarification_question"}
        assert out["awaiting_order_id"] is False
        assert out["clarification_question"] is None
        # history flattened to plain strings, per Derrick's seeded rows
        assert out["conversation_history"] == [
            "Broken keyboard ORD-POL-001", '{"refund_reason": "damaged"}']
        assert out["case"]["policy_version"] == "v1.0"


class TestSqliteSkip:
    def test_writes_skipped_on_sqlite_with_audit_note(self, monkeypatch):
        # Any mysql connection attempt would be a bug on the sqlite backend.
        def _boom(**kwargs):
            raise AssertionError("mysql.connector.connect must not be called")
        monkeypatch.setattr("mysql.connector.connect", _boom)

        pipeline_store.ensure_ticket("TK-1", "CUST-001", "hello")
        pipeline_store.start_workflow_run("TR-1", "TK-1")
        pipeline_store.record_handoff(
            "TR-1", "TK-1", from_agent="triage_agent", to_agent="policy_agent",
            input_json={}, output_json={}, input_tokens=None, output_tokens=None)

        rows = _local_rows(
            "SELECT payload_json FROM audit_log WHERE event_type='pipeline_write_skipped'")
        assert len(rows) == 3
        assert all("sqlite_backend" in r[0] for r in rows)


class TestMysqlFailOpen:
    def test_mysql_failure_is_fail_open(self, monkeypatch):
        monkeypatch.setattr(pipeline_store, "_remote_enabled", lambda: True)

        def _refuse(**kwargs):
            raise ConnectionError("simulated mysql outage")
        monkeypatch.setattr("mysql.connector.connect", _refuse)

        # none of these may raise
        pipeline_store.ensure_ticket("TK-1", "CUST-001", "hello")
        pipeline_store.update_ticket_triaged(
            "TK-1", sanitized_text="x", refund_reason="damaged", requested_amount=1.0)
        pipeline_store.update_workflow_run(
            "TR-1", status="running", current_agent="policy_agent")

        rows = _local_rows(
            "SELECT payload_json FROM audit_log WHERE event_type='pipeline_write_failed'")
        assert len(rows) >= 3
        assert "simulated mysql outage" in rows[0][0]


class TestRemoteAuditRouting:
    def test_log_event_routes_remote_when_mysql_active(self, monkeypatch):
        captured = []
        monkeypatch.setattr(pipeline_store, "_remote_enabled", lambda: True)
        monkeypatch.setattr(pipeline_store, "insert_audit_remote",
                            lambda *a: captured.append(a))

        audit_logger.log_event("TR-9", "run_started", agent="triage_agent",
                               ticket_id="TK-9", user_id="CUST-POL-001",
                               payload={"message_length": 5})

        assert len(captured) == 1
        trace_id, event_type, agent, payload_json = captured[0]
        assert (trace_id, event_type) == ("TR-9", "run_started")
        # ticket_id folded into payload (main_db audit_log has no such column)
        assert '"ticket_id": "TK-9"' in payload_json
        # nothing written locally
        assert _local_rows("SELECT 1 FROM audit_log") == []

    def test_log_event_falls_back_local_when_remote_fails(self, monkeypatch):
        monkeypatch.setattr(pipeline_store, "_remote_enabled", lambda: True)

        def _fail(*a):
            raise ConnectionError("remote down")
        monkeypatch.setattr(pipeline_store, "insert_audit_remote", _fail)

        audit_logger.log_event("TR-9", "run_started", agent="triage_agent")

        rows = _local_rows("SELECT event_type FROM audit_log")
        assert rows == [("run_started",)]

    def test_governance_event_routes_remote_and_falls_back(self, monkeypatch):
        captured = []
        monkeypatch.setattr(pipeline_store, "_remote_enabled", lambda: True)
        monkeypatch.setattr(pipeline_store, "insert_governance_remote",
                            lambda *a: captured.append(a))

        result = {"status": "block", "rule": "ASI07", "failed_check": "ownership",
                  "offending_value": "CUST-002"}
        audit_logger.log_governance_event("TR-9", "TK-9", "CUST-001",
                                          result, "human_approval")
        assert len(captured) == 1
        trace_id, agent, owasp, score, action, flags_json, offending = captured[0]
        assert owasp == "ASI07" and action == "block"
        assert offending == "CUST-002"  # raw offending_content
        assert '"ticket_id": "TK-9"' in flags_json  # folded into flags

        # now remote fails → local row written
        def _fail(*a):
            raise ConnectionError("remote down")
        monkeypatch.setattr(pipeline_store, "insert_governance_remote", _fail)
        audit_logger.log_governance_event("TR-9", "TK-9", "CUST-001",
                                          result, "human_approval")
        rows = _local_rows(
            "SELECT interceptor_action, offending_content FROM governance_events")
        assert rows == [("block", "CUST-002")]


class TestBackendFallbackNoRecursion:
    def test_probe_failure_does_not_recurse(self, monkeypatch):
        # mysql requested but down: active_backend() must terminate and the
        # backend_fallback event must land in the LOCAL store.
        monkeypatch.setenv("DB_BACKEND", "mysql")

        def _refuse(**kwargs):
            raise ConnectionError("probe refused")
        monkeypatch.setattr("mysql.connector.connect", _refuse)
        backend.reset_backend_cache()

        assert backend.active_backend() == "sqlite"
        rows = _local_rows(
            "SELECT 1 FROM audit_log WHERE event_type='backend_fallback'")
        assert rows, "backend_fallback event must be recorded locally"
