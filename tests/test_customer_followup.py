from __future__ import annotations

import json
import os
import threading
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import refund_app.api as api
from db.backend import DatabaseGovernanceEventRepository
from db.database import CloudDatabaseError, GCPRepository
from db.followup_context import followup_fence
from db.followup_store import (
    CustomerFollowupClaim,
    CustomerFollowupConflictError,
    CustomerFollowupStore,
)
from demo.catalog import DEMO_IDS, load_demo_catalog
from governance import GovernanceStatement
from refund_app.followup import (
    CustomerFollowupExecutionError,
    CustomerFollowupService,
)


def _graph_state(case_id: str = "demo10") -> dict:
    case = load_demo_catalog().get(case_id)
    follow_up = case.follow_up
    assert follow_up is not None
    return {
        "trace_id": case.trace_id,
        "ticket_id": case.ticket_id,
        "user_id": case.customer_id,
        "message": follow_up.message,
        "requested_order_id": case.order_id,
        "order_resolution_source": "trusted_ui_selection",
        "order_lookup_result": {"order_id": case.order_id},
        "triage_governance_result": {"status": "allow"},
        "policy_governance_result": {"status": "allow"},
        "response_governance_result": {"status": "allow"},
        "policy_decision": {"decision": "approve"},
        "policy_persistence_result": {"next_agent": "refund_agent"},
        "refund_result": {
            "status": "success",
            "order_id": case.order_id,
            "amount": follow_up.requested_amount,
            "currency": follow_up.currency,
        },
        "final_outcome": "refund_issued",
        "workflow_status": "completed",
        "response_result": {
            "response": {"body": "Refund issued."},
            "content_checks": {
                "decision_reflected": True,
                "missing_info_requested": True,
                "safe_summary_reflected": True,
                "outcome_anchor_reflected": True,
                "pii_fields_detected": [],
                "forbidden_phrases": [],
            },
        },
    }


class _Graph:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.inputs: list[dict] = []
        self.policy_dates: list[str | None] = []

    def invoke(self, value: dict) -> dict:
        self.inputs.append(value)
        self.policy_dates.append(os.getenv("POLICY_EVALUATION_DATE"))
        return deepcopy(self.state)


class _ServiceStore:
    def __init__(self, claim: CustomerFollowupClaim | None = None) -> None:
        self.repository = object()
        self.lease_seconds = 30
        self.claim_result = claim or CustomerFollowupClaim(
            receipt_log_id=41,
            claim_token="claim-token-1",
            assistant_response="Please tell us what was wrong with the item.",
        )
        self.claimed = []
        self.completed = []
        self.failed = []

    def claim(self, case):
        self.claimed.append(case.trace_id)
        return self.claim_result

    def complete(self, case, result, claim_token):
        self.completed.append((case.trace_id, result, claim_token))
        return {
            **result,
            "persistence": {"database": "final", "matched": True},
            "follow_up": {"idempotent": False, "status": "completed", "receipt_log_id": 41},
        }

    def fail(self, case, error, claim_token):
        self.failed.append((case.trace_id, error, claim_token))

    def heartbeat(self, _case, _claim_token):
        return True


def test_service_resumes_same_roots_through_trusted_triage_context() -> None:
    case = load_demo_catalog().get("demo10")
    graph = _Graph(_graph_state())
    store = _ServiceStore()

    result = CustomerFollowupService(store, graph=graph).run(case)

    assert result["matched_expectations"] is True
    assert result["persistence"] == {"database": "final", "matched": True}
    assert store.claimed == ["demo10"]
    assert len(store.completed) == 1
    assert store.failed == []
    graph_input = graph.inputs[0]
    assert graph_input["trace_id"] == "demo10"
    assert graph_input["ticket_id"] == "ticket-demo10"
    assert graph_input["user_id"] == "customer-demo10"
    assert graph_input["requested_order_id"] == "order-demo10"
    assert graph_input["request_context"]["request_origin"] == "refund_portal"
    assert graph_input["request_context"]["selected_order_id"] == "order-demo10"
    assert graph_input["request_context"]["continuation_type"] == "customer_followup"
    assert graph_input["request_context"]["evaluation_date"] == case.evaluation_date
    assert graph_input["request_context"]["followup_claim_token"] == "claim-token-1"
    assert graph_input["conversation_history"] == [
        {"role": "user", "content": case.message},
        {"role": "assistant", "content": "Please tell us what was wrong with the item."},
    ]
    assert graph.policy_dates == [case.evaluation_date]


def test_identical_completed_replay_returns_stored_result_without_graph() -> None:
    stored = {
        **_graph_state(),
        "success": True,
        "matched_expectations": True,
        "persistence": {"database": "final", "matched": True},
    }
    store = _ServiceStore(CustomerFollowupClaim(receipt_log_id=55, completed_result=stored))
    graph = _Graph({})

    result = CustomerFollowupService(store, graph=graph).run(
        load_demo_catalog().get("demo10")
    )

    assert result["follow_up"] == {
        "idempotent": True,
        "status": "already_completed",
        "receipt_log_id": 55,
        "recovered_without_graph": False,
    }
    assert graph.inputs == []
    assert store.completed == []


def test_unexpected_continuation_fails_closed() -> None:
    state = _graph_state()
    state["policy_decision"] = {"decision": "deny"}
    store = _ServiceStore()

    with pytest.raises(CustomerFollowupExecutionError, match="acceptance contract"):
        CustomerFollowupService(store, graph=_Graph(state)).run(
            load_demo_catalog().get("demo10")
        )

    assert len(store.failed) == 1
    assert store.completed == []


def test_service_heartbeats_claim_while_graph_is_waiting() -> None:
    heartbeat_seen = threading.Event()

    class HeartbeatStore(_ServiceStore):
        lease_seconds = 0.03

        def __init__(self):
            super().__init__()
            self.lease_seconds = 0.03

        def heartbeat(self, case, claim_token):
            assert case.trace_id == "demo10"
            assert claim_token == "claim-token-1"
            heartbeat_seen.set()
            return True

    class WaitingGraph:
        def invoke(self, _value):
            assert heartbeat_seen.wait(timeout=1)
            return _graph_state()

    result = CustomerFollowupService(
        HeartbeatStore(), graph=WaitingGraph()
    ).run(load_demo_catalog().get("demo10"))

    assert result["matched_expectations"] is True
    assert heartbeat_seen.is_set()


class _MemoryDatabase:
    def __init__(self, case_id: str = "demo10") -> None:
        self.case = load_demo_catalog().get(case_id)
        self.follow_up = self.case.follow_up
        assert self.follow_up is not None
        self.workflow = {
            "ticket_id": self.case.ticket_id,
            "status": "waiting_user",
            "current_agent": "triage_agent",
            "policy_version": "v1.0",
            "customer_id": self.case.customer_id,
            "raw_text": self.case.message,
            "order_id": self.case.order_id,
        }
        checks = _graph_state(case_id)["response_result"]["content_checks"]
        initial_response = {
            "response_result": {
                "response": {"body": "Please tell us what was wrong with the item."},
                "content_checks": checks,
                "final_outcome": "need_info",
                "workflow_status": "waiting_user",
            },
            "response_handoff": "end",
        }
        self.tables = {
            "agent_handoffs": [
                self._handoff("triage-0", "triage_agent", "policy_agent", {"triage_output": {"cycle": "initial"}}),
                self._handoff("policy-0", "policy_agent", "response_agent", {"decision": {"type": "request_info"}}),
                self._handoff("response-0", "response_agent", "end", initial_response),
            ],
            "audit_log": [
                self._audit(1, "triage_agent_evaluated", "triage_agent", {"cycle": "initial"}),
                self._audit(2, "policy_agent_evaluated", "policy_agent", {"cycle": "initial"}),
                self._audit(3, "response_agent_evaluated", "response_agent", {"cycle": "initial"}),
            ],
            "governance_events": [
                self._governance("gov-triage", "triage_agent"),
                self._governance("gov-policy", "policy_agent"),
                self._governance("gov-response", "response_agent"),
            ],
            "policy_review_events": [self._policy_review()],
            "human_approvals": [],
            "refund_transactions": [],
        }
        self.next_log_id = 10
        self.claim_ages: dict[int, int] = {}
        self.statements: list[tuple[str, tuple | None]] = []

    def _handoff(self, handoff_id, source, target, output, *, claim_token=None):
        input_payload = {"cycle": "initial"}
        output_payload = dict(output)
        if claim_token is not None:
            marker = {"type": "customer_followup", "claim_token": claim_token}
            input_payload["_continuation"] = marker
            output_payload["_continuation"] = marker
        return {
            "handoff_id": handoff_id,
            "trace_id": self.case.trace_id,
            "ticket_id": self.case.ticket_id,
            "from_agent": source,
            "to_agent": target,
            "input_json": json.dumps(input_payload),
            "output_json": json.dumps(output_payload),
            "input_tokens": 1,
            "output_tokens": 1,
            "created_at": "2026-07-01 10:00:00",
        }

    def _audit(self, log_id, event_type, agent, payload):
        return {
            "log_id": log_id,
            "trace_id": self.case.trace_id,
            "event_type": event_type,
            "agent": agent,
            "payload_json": json.dumps(payload),
            "created_at": "2026-07-01 10:00:00",
        }

    def _governance(self, event_id, agent, *, claim_token=None):
        flags = {}
        if claim_token is not None:
            flags["_continuation"] = {
                "type": "customer_followup",
                "claim_token": claim_token,
            }
        return {
            "event_id": event_id,
            "trace_id": self.case.trace_id,
            "agent": agent,
            "owasp_category": "ASI00",
            "trigger_score": 0.0,
            "interceptor_action": "allow",
            "flags_json": json.dumps(flags),
            "offending_content": None,
            "created_at": "2026-07-01 10:00:00",
        }

    def _policy_review(self):
        return {
            "policy_review_event_id": "review-0",
            "trace_id": self.case.trace_id,
            "policy_version": "v1.0",
            "review_type": "missing_fact",
            "policy_ids_json": "[]",
            "evidence_json": "{}",
            "detail": "The reason is missing.",
            "created_at": "2026-07-01 10:00:00",
        }

    def install_partial_attempt(self) -> None:
        self.tables["agent_handoffs"] = [
            row for row in self.tables["agent_handoffs"] if row["from_agent"] != "triage_agent"
        ] + [self._handoff("triage-partial", "triage_agent", "policy_agent", {"cycle": "partial"})]
        self.tables["audit_log"].append(
            self._audit(self._next_log(), "triage_agent_evaluated", "triage_agent", {"cycle": "partial"})
        )

    def install_completed_attempt(self) -> None:
        claim_token = json.loads(
            self.latest_event("customer_followup_claimed")["payload_json"]
        )["claim_token"]
        marker = {"type": "customer_followup", "claim_token": claim_token}
        state = _graph_state(self.case.trace_id)
        response_output = {
            "response_result": {
                **state["response_result"],
                "final_outcome": "approved",
                "workflow_status": "completed",
            },
            "response_handoff": "end",
        }
        self.tables["agent_handoffs"].extend([
            self._handoff("triage-1", "triage_agent", "policy_agent", {"triage_output": {"cycle": "followup"}}, claim_token=claim_token),
            self._handoff("policy-1", "policy_agent", "refund_agent", {"decision": {"type": "approve"}}, claim_token=claim_token),
            self._handoff("refund-1", "refund_agent", "response_agent", {"refund_result": state["refund_result"]}, claim_token=claim_token),
            self._handoff("response-1", "response_agent", "end", response_output, claim_token=claim_token),
        ])
        for index, (event_type, agent) in enumerate((
            ("triage_agent_evaluated", "triage_agent"),
            ("policy_agent_evaluated", "policy_agent"),
            ("refund_issued", "refund_agent"),
            ("response_agent_evaluated", "response_agent"),
        )):
            self.tables["audit_log"].append(self._audit(
                self._next_log(),
                event_type,
                agent,
                {"_continuation": marker, "cycle": "followup", "index": index},
            ))
        self.tables["governance_events"].extend([
            self._governance("gov-triage-1", "triage_agent", claim_token=claim_token),
            self._governance("gov-response-1", "response_agent", claim_token=claim_token),
        ])
        self.tables["refund_transactions"] = [{
            "transaction_id": "refund-1",
            "trace_id": self.case.trace_id,
            "approval_id": None,
            "amount": self.follow_up.requested_amount,
            "currency": self.follow_up.currency,
            "status": "issued",
            "external_ref": "demo-refund",
            "created_at": "2026-07-01 10:01:00",
            "updated_at": "2026-07-01 10:01:00",
        }]
        self.workflow.update(status="completed", current_agent="completed")

    def latest_event(self, event_type):
        rows = [row for row in self.tables["audit_log"] if row["event_type"] == event_type]
        return sorted(rows, key=lambda row: row["log_id"])[-1] if rows else None

    def _next_log(self):
        value = self.next_log_id
        self.next_log_id += 1
        return value


class _MemoryCursor:
    def __init__(self, database: _MemoryDatabase) -> None:
        self.db = database
        self.rows = []
        self.row = None
        self.rowcount = 0
        self.lastrowid = 0

    def execute(self, statement: str, params: tuple | None = None) -> None:
        import re

        sql = " ".join(statement.split())
        self.db.statements.append((sql, params))
        self.rows, self.row, self.rowcount = [], None, 0
        if sql.startswith("SELECT trace_id FROM workflow_runs ORDER BY"):
            self.rows = [{"trace_id": value} for value in DEMO_IDS]
        elif sql.startswith("SELECT trace_id, status, current_agent FROM workflow_runs"):
            self.row = (
                {
                    "trace_id": params[0],
                    "status": self.db.workflow["status"],
                    "current_agent": self.db.workflow["current_agent"],
                }
                if params[0] == self.db.case.trace_id
                else None
            )
        elif sql.startswith("SELECT payload_json FROM audit_log"):
            event = (
                "customer_followup_failed"
                if "customer_followup_failed" in sql
                else "customer_followup_claimed"
            )
            found = self.db.latest_event(event)
            self.row = {"payload_json": found["payload_json"]} if found else None
        elif sql.startswith("SELECT log_id FROM audit_log"):
            found = self.db.latest_event("customer_followup_completed")
            self.row = {"log_id": found["log_id"]} if found else None
        elif "FROM workflow_runs workflow JOIN tickets" in sql:
            self.row = dict(self.db.workflow)
        elif sql.startswith("SELECT log_id, payload_json, created_at, TIMESTAMPDIFF"):
            event = params[1]
            found = self.db.latest_event(event)
            if found:
                self.row = {**found, "age_seconds": self.db.claim_ages.get(found["log_id"], 0)}
        elif sql.startswith("SELECT log_id, payload_json, created_at FROM audit_log"):
            event = params[1]
            self.rows = [dict(row) for row in self.db.tables["audit_log"] if row["event_type"] == event]
        elif sql.startswith("SELECT handoff_id FROM agent_handoffs"):
            self.rows = [
                {"handoff_id": row["handoff_id"]}
                for row in self.db.tables["agent_handoffs"]
                if row["from_agent"] == "customer"
            ]
        elif match := re.match(r"SELECT (.+) FROM (\w+) WHERE trace_id = %s ORDER BY", sql):
            table = match.group(2)
            self.rows = [dict(row) for row in self.db.tables[table] if row["trace_id"] == params[0]]
        elif sql.startswith("SELECT customer_id FROM customers"):
            self.rows = [{"customer_id": f"customer-{value}"} for value in DEMO_IDS]
        elif sql.startswith("SELECT order_id FROM orders"):
            self.rows = [{"order_id": f"order-{value}"} for value in DEMO_IDS]
        elif sql.startswith("SELECT ticket_id FROM tickets"):
            self.rows = [{"ticket_id": f"ticket-{value}"} for value in DEMO_IDS]
        elif sql.startswith("SELECT from_agent, to_agent, output_json FROM agent_handoffs"):
            self.rows = [dict(row) for row in self.db.tables["agent_handoffs"]]
        elif sql.startswith("SELECT status, amount, currency FROM refund_transactions"):
            self.rows = [dict(row) for row in self.db.tables["refund_transactions"]]
        elif sql.startswith("DELETE FROM"):
            table = sql.split()[2]
            if table == "agent_handoffs":
                self.db.tables[table] = [row for row in self.db.tables[table] if row["from_agent"] == "customer"]
            elif table == "audit_log":
                keep = {"customer_followup_received", "customer_followup_claimed", "customer_followup_failed", "customer_followup_completed"}
                self.db.tables[table] = [row for row in self.db.tables[table] if row["event_type"] in keep]
            else:
                self.db.tables[table] = []
        elif match := re.match(r"INSERT INTO (\w+) \(([^)]+)\) VALUES", sql):
            table = match.group(1)
            columns = [value.strip() for value in match.group(2).split(",")]
            if table == "audit_log" and columns == ["trace_id", "event_type", "agent", "payload_json"]:
                if "'customer'" in sql or "'workflow'" in sql:
                    literal_agent = "customer" if "'customer'" in sql else "workflow"
                    row = {
                        "trace_id": params[0],
                        "event_type": params[1],
                        "agent": literal_agent,
                        "payload_json": params[2],
                    }
                else:
                    row = dict(zip(columns, params, strict=True))
                row.update(log_id=self.db._next_log(), created_at="2026-07-01 10:02:00")
                self.lastrowid = row["log_id"]
            elif table == "agent_handoffs" and len(params) == 5:
                row = dict(zip(columns[:3] + columns[5:7], params, strict=True))
                row.update(
                    from_agent="customer", to_agent="triage_agent",
                    input_tokens=0, output_tokens=0, created_at="2026-07-01 10:02:00",
                )
            else:
                row = dict(zip(columns, params, strict=True))
            self.db.tables[table].append(row)
            self.rowcount = 1
        elif sql.startswith("UPDATE workflow_runs SET status = 'running'"):
            if self.db.workflow["status"] == "waiting_user" and self.db.workflow["current_agent"] == "triage_agent":
                self.db.workflow.update(status="running", current_agent="triage_agent")
                self.rowcount = 1
        elif sql.startswith("UPDATE workflow_runs SET status = 'waiting_user'"):
            changed = (
                self.db.workflow["status"] != "waiting_user"
                or self.db.workflow["current_agent"] != "triage_agent"
            )
            self.db.workflow.update(status="waiting_user", current_agent="triage_agent")
            if "policy_version = %s" in sql:
                changed = changed or self.db.workflow.get("policy_version") != params[0]
                self.db.workflow["policy_version"] = params[0]
            # mysql.connector reports affected rows, not matched rows. A
            # repeated same-value update in the same timestamp second is 0.
            self.rowcount = int(changed)
        elif sql.startswith("UPDATE audit_log SET created_at = CURRENT_TIMESTAMP"):
            # Lease heartbeat. A same-second refresh may legitimately affect 0
            # rows, and production code intentionally does not reject that.
            claim = self.db.latest_event("customer_followup_claimed")
            if claim is not None:
                self.db.claim_ages[claim["log_id"]] = 0
            self.rowcount = 0
        elif sql.startswith("UPDATE workflow_runs SET status = 'failed'"):
            self.db.workflow.update(status="failed", current_agent="triage_agent")
            self.rowcount = 1
        elif sql.startswith("UPDATE workflow_runs SET status = %s"):
            self.db.workflow.update(status=params[0], current_agent=params[1])
            self.rowcount = 1
        else:
            raise AssertionError(f"Unhandled fake SQL: {sql}")

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.row

    def close(self):
        pass


class _MemoryConnection:
    def __init__(self, database):
        self.database = database

    def start_transaction(self):
        pass

    def cursor(self, *, dictionary=False):
        return _MemoryCursor(self.database)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _MemoryRepository:
    database_name = "final"

    def __init__(self, database):
        self.database = database

    def _connect(self):
        return _MemoryConnection(self.database)


def _store_fixture():
    database = _MemoryDatabase()
    return database, CustomerFollowupStore(_MemoryRepository(database), lease_seconds=30)


def test_store_first_claim_archives_integrity_checked_initial_history() -> None:
    database, store = _store_fixture()
    initial_handoffs = deepcopy(database.tables["agent_handoffs"])

    claim = store.claim(database.case)

    assert claim.claim_token
    assert claim.assistant_response == "Please tell us what was wrong with the item."
    assert database.workflow["status"] == "running"
    receipt = database.latest_event("customer_followup_received")
    payload = json.loads(receipt["payload_json"])
    assert len(payload["history_sha256"]) == 64
    assert payload["history"]["tables"]["agent_handoffs"] == initial_handoffs
    assert payload["history"]["assistant_request_info_response"] == claim.assistant_response
    assert not any(
        sql.startswith(f"INSERT INTO {table}")
        for sql, _ in database.statements
        for table in ("customers", "orders", "tickets", "workflow_runs")
    )


def test_fresh_concurrent_claim_is_rejected_without_history_mutation() -> None:
    database, store = _store_fixture()
    first = store.claim(database.case)
    before = deepcopy(database.tables)

    with pytest.raises(CustomerFollowupConflictError, match="already in progress"):
        store.claim(database.case)

    assert database.tables == before
    assert database.latest_event("customer_followup_claimed")
    assert first.claim_token


def test_stale_claim_restores_partial_attempt_then_reclaims_with_new_fence() -> None:
    database, store = _store_fixture()
    first = store.claim(database.case)
    database.install_partial_attempt()
    claim_row = database.latest_event("customer_followup_claimed")
    database.claim_ages[claim_row["log_id"]] = 31

    second = store.claim(database.case)

    assert second.claim_token != first.claim_token
    stage_handoffs = [row for row in database.tables["agent_handoffs"] if row["from_agent"] != "customer"]
    assert {row["handoff_id"] for row in stage_handoffs} == {"triage-0", "policy-0", "response-0"}
    assert not any("partial" in row["output_json"] for row in stage_handoffs)
    assert database.workflow["status"] == "running"


def test_stale_completed_invalid_proof_restores_and_reclaims() -> None:
    database, store = _store_fixture()
    first = store.claim(database.case)
    database.install_completed_attempt()
    initial_policy = next(
        row
        for row in database.tables["agent_handoffs"]
        if row["handoff_id"] == "policy-0"
    )
    initial_policy["to_agent"] = "refund_agent"

    with pytest.raises(CustomerFollowupConflictError, match="active lease"):
        store.claim(database.case)

    claim_row = database.latest_event("customer_followup_claimed")
    database.claim_ages[claim_row["log_id"]] = 31
    second = store.claim(database.case)

    assert second.claim_token != first.claim_token
    assert database.workflow["status"] == "running"
    assert database.tables["refund_transactions"] == []
    assert {
        row["handoff_id"] for row in database.tables["agent_handoffs"]
    } == {
        "triage-0",
        "policy-0",
        "response-0",
        str(uuid.uuid5(uuid.NAMESPACE_URL, "idox-handoff:demo10:customer_followup")),
    }


def test_store_heartbeat_resets_claim_age_and_stops_at_terminal_workflow() -> None:
    database, store = _store_fixture()
    claim = store.claim(database.case)
    claim_row = database.latest_event("customer_followup_claimed")
    database.claim_ages[claim_row["log_id"]] = 29

    assert store.heartbeat(database.case, claim.claim_token) is True
    assert database.claim_ages[claim_row["log_id"]] == 0

    database.workflow.update(status="completed", current_agent="completed")
    assert store.heartbeat(database.case, claim.claim_token) is False


def test_handled_failure_restores_history_and_allows_immediate_retry() -> None:
    database, store = _store_fixture()
    first = store.claim(database.case)
    database.install_partial_attempt()

    store.fail(database.case, RuntimeError("boom"), first.claim_token)
    assert database.workflow["status"] == "waiting_user"
    assert database.workflow["current_agent"] == "triage_agent"
    failure = json.loads(database.latest_event("customer_followup_failed")["payload_json"])
    assert failure["retryable"] is True
    assert failure["restored_workflow_status"] == "waiting_user"
    assert {row["handoff_id"] for row in database.tables["agent_handoffs"] if row["from_agent"] != "customer"} == {
        "triage-0", "policy-0", "response-0"
    }

    second = store.claim(database.case)
    assert second.claim_token != first.claim_token
    assert database.workflow["status"] == "running"


def test_service_restores_completed_graph_when_terminal_proof_fails() -> None:
    """Model the live failure: all stages committed but Policy mutated history."""

    database, store = _store_fixture()

    class CompletionProofFailureGraph:
        def invoke(self, _value: dict) -> dict:
            database.install_completed_attempt()
            initial_policy = next(
                row
                for row in database.tables["agent_handoffs"]
                if row["handoff_id"] == "policy-0"
            )
            initial_policy["to_agent"] = "refund_agent"
            return _graph_state()

    with pytest.raises(
        CustomerFollowupExecutionError,
        match="customer follow-up continuation failed",
    ):
        CustomerFollowupService(store, graph=CompletionProofFailureGraph()).run(
            database.case
        )

    assert database.workflow["status"] == "waiting_user"
    assert database.workflow["current_agent"] == "triage_agent"
    failure = json.loads(
        database.latest_event("customer_followup_failed")["payload_json"]
    )
    assert failure["error_type"] == "CustomerFollowupStoreError"
    assert failure["history_restored"] is True
    assert database.latest_event("customer_followup_completed") is None
    assert database.tables["refund_transactions"] == []
    assert {
        row["handoff_id"] for row in database.tables["agent_handoffs"]
    } == {
        "triage-0",
        "policy-0",
        "response-0",
        str(uuid.uuid5(uuid.NAMESPACE_URL, "idox-handoff:demo10:customer_followup")),
    }


def test_failed_claim_token_cannot_resurrect_store_or_database_stages() -> None:
    database, store = _store_fixture()
    claim = store.claim(database.case)
    assert claim.claim_token
    store.fail(database.case, RuntimeError("handled"), claim.claim_token)
    snapshot = deepcopy(database.tables)

    with pytest.raises(CustomerFollowupConflictError, match="failed.*revoked"):
        store.complete(
            database.case,
            {"matched_expectations": True},
            claim.claim_token,
        )

    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: _MemoryConnection(database)  # type: ignore[method-assign]
    statement = GovernanceStatement(
        trace_id=database.case.trace_id,
        agent="triage_agent",
        stage="triage_governance",
        status="allow",
        summary="Delayed worker result.",
    )
    delayed_writes = (
        lambda: repository.persist_agent_handoff(
            trace_id=database.case.trace_id,
            ticket_id=database.case.ticket_id,
            from_agent="triage_agent",
            to_agent="policy_agent",
            input_payload={},
            output_payload={},
            audit_event_type="triage_agent_evaluated",
            workflow_status="running",
            current_agent="policy_agent",
            followup_claim_token=claim.claim_token,
        ),
        lambda: repository.save_governance_event_record(
            statement,
            followup_claim_token=claim.claim_token,
        ),
        lambda: repository.persist_result(
            SimpleNamespace(case=SimpleNamespace(trace_id=database.case.trace_id)),
            None,
            None,
            None,
            [],
            None,
            followup_claim_token=claim.claim_token,
        ),
        lambda: repository.persist_refund_result(
            trace_id=database.case.trace_id,
            ticket_id=database.case.ticket_id,
            policy_decision={"decision": "approve"},
            order_lookup_result={"order_id": database.case.order_id},
            refund_result={
                "status": "success",
                "amount": database.follow_up.requested_amount,
                "currency": database.follow_up.currency,
                "order_id": database.case.order_id,
            },
            followup_claim_token=claim.claim_token,
        ),
    )
    for delayed_write in delayed_writes:
        with pytest.raises(CloudDatabaseError, match="failed.*revoked"):
            delayed_write()

    assert database.tables == snapshot
    assert database.workflow["status"] == "waiting_user"
    assert database.workflow["current_agent"] == "triage_agent"


def test_completed_without_marker_is_recovered_graph_free_then_replays() -> None:
    database, store = _store_fixture()
    store.claim(database.case)
    database.install_completed_attempt()

    recovered = store.claim(database.case)
    assert recovered.recovered is True
    assert recovered.completed_result["matched_expectations"] is True
    assert recovered.completed_result["final_outcome"] == "refund_issued"
    assert recovered.completed_result["follow_up"]["recovered_without_graph"] is True
    assert database.latest_event("customer_followup_completed")

    replay = store.claim(database.case)
    assert replay.completed_result == recovered.completed_result
    assert replay.claim_token is None


def test_complete_proves_roots_history_route_refund_and_response_semantics() -> None:
    database, store = _store_fixture()
    claim = store.claim(database.case)
    database.install_completed_attempt()

    result = store.complete(database.case, {
        **_graph_state(),
        "case_id": "demo10",
        "customer_id": "customer-demo10",
        "order_id": "order-demo10",
        "selected_order_id": "order-demo10",
        "request_facts": database.follow_up.request_payload(database.case),
        "route": "refund_agent",
        "policy_decision": "approve",
        "response_content_checks": _graph_state()["response_result"]["content_checks"],
        "expected": database.follow_up.expectations.as_dict(),
        "success": True,
        "matched_expectations": True,
    }, claim.claim_token)

    assert result["persistence"]["exact_root_counts"] == {
        "workflow_runs": 20, "customers": 20, "orders": 20, "tickets": 20,
    }
    assert result["persistence"]["history"]["queryable_in_receipt_audit"] is True
    assert result["persistence"]["history"]["queryable_in_live_tables"] is True
    assert result["persistence"]["history"]["initial_rows_preserved"] is True
    assert result["persistence"]["history"]["initial_handoff_count"] == 3
    assert result["persistence"]["history"]["live_handoff_count"] == 8
    assert result["persistence"]["history"]["continuation_handoff_count"] == 4
    assert result["persistence"]["history"]["continuation_audit_count"] == 4
    assert result["persistence"]["history"]["continuation_governance_count"] == 2
    assert {row["handoff_id"] for row in database.tables["agent_handoffs"]} == {
        "triage-0", "policy-0", "response-0", "triage-1", "policy-1",
        "refund-1", "response-1",
        str(uuid.uuid5(uuid.NAMESPACE_URL, "idox-handoff:demo10:customer_followup")),
    }
    assert result["persistence"]["response_content_checks"]["decision_reflected"] is True


def test_stale_token_cannot_fail_or_complete_newer_attempt() -> None:
    database, store = _store_fixture()
    first = store.claim(database.case)
    store.fail(database.case, RuntimeError("handled"), first.claim_token)
    second = store.claim(database.case)
    snapshot = deepcopy(database.tables)

    with pytest.raises(CustomerFollowupConflictError, match="stale"):
        store.fail(database.case, RuntimeError("old"), first.claim_token)
    with pytest.raises(CustomerFollowupConflictError, match="stale"):
        store.complete(database.case, {"matched_expectations": True}, first.claim_token)

    assert database.tables == snapshot
    assert second.claim_token != first.claim_token


def test_repository_appends_marked_stage_and_governance_rows_and_fences_stale_token() -> None:
    database, store = _store_fixture()
    claim = store.claim(database.case)
    assert claim.claim_token
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: _MemoryConnection(database)  # type: ignore[method-assign]
    initial_handoffs = deepcopy(database.tables["agent_handoffs"])
    statement_index = len(database.statements)

    handoff_id = repository.persist_agent_handoff(
        trace_id=database.case.trace_id,
        ticket_id=database.case.ticket_id,
        from_agent="triage_agent",
        to_agent="policy_agent",
        input_payload={"message": database.follow_up.message},
        output_payload={"triage_handoff": "policy"},
        audit_event_type="triage_agent_evaluated",
        workflow_status="running",
        current_agent="policy_agent",
        followup_claim_token=claim.claim_token,
    )

    assert all(row in database.tables["agent_handoffs"] for row in initial_handoffs)
    resumed = next(row for row in database.tables["agent_handoffs"] if row["handoff_id"] == handoff_id)
    expected_marker = {"type": "customer_followup", "claim_token": claim.claim_token}
    assert json.loads(resumed["input_json"])["_continuation"] == expected_marker
    assert json.loads(resumed["output_json"])["_continuation"] == expected_marker
    assert not any(
        sql.startswith("DELETE FROM agent_handoffs")
        for sql, _ in database.statements[statement_index:]
    )
    stage_audit = database.latest_event("triage_agent_evaluated")
    assert json.loads(stage_audit["payload_json"])["_continuation"] == expected_marker
    assert sum(
        sql.startswith("UPDATE audit_log SET created_at = CURRENT_TIMESTAMP")
        for sql, _ in database.statements[statement_index:]
    ) == 1

    newer_token = "newer-followup-token"
    database.tables["audit_log"].append(database._audit(
        database._next_log(),
        "customer_followup_claimed",
        "workflow",
        {"claim_token": newer_token, "attempt": 2},
    ))
    before = deepcopy(database.tables)
    with pytest.raises(CloudDatabaseError, match="stale customer follow-up claim token"):
        repository.persist_agent_handoff(
            trace_id=database.case.trace_id,
            ticket_id=database.case.ticket_id,
            from_agent="response_agent",
            to_agent="end",
            input_payload={},
            output_payload={},
            audit_event_type="response_agent_evaluated",
            workflow_status="completed",
            current_agent="completed",
            followup_claim_token=claim.claim_token,
        )
    assert database.tables == before

    event_id = repository.save_governance_event_record(
        GovernanceStatement(
            trace_id=database.case.trace_id,
            agent="triage_agent",
            stage="triage_governance",
            status="allow",
            summary="Continuation facts are safe.",
        ),
        followup_claim_token=newer_token,
    )
    assert any(row["event_id"] == "gov-triage" for row in database.tables["governance_events"])
    governance = next(
        row for row in database.tables["governance_events"] if row["event_id"] == event_id
    )
    assert json.loads(governance["flags_json"])["_continuation"] == {
        "type": "customer_followup",
        "claim_token": newer_token,
    }
    assert sum(
        sql.startswith("UPDATE audit_log SET created_at = CURRENT_TIMESTAMP")
        for sql, _ in database.statements[statement_index:]
    ) == 2


def test_governance_context_rejects_cross_trace_write() -> None:
    calls: list[tuple[GovernanceStatement, dict[str, str]]] = []

    class _Backend:
        def save_governance_event_record(self, statement, **kwargs):
            calls.append((statement, kwargs))
            return "event-id"

    repository = DatabaseGovernanceEventRepository(_Backend())
    with followup_fence("demo10", "claim-token"):
        with pytest.raises(RuntimeError, match="crossed its trace fence"):
            repository.save_event(GovernanceStatement(
                trace_id="demo14",
                agent="triage_agent",
                stage="triage_governance",
                status="allow",
                summary="Wrong trace.",
            ))
        assert repository.save_event(GovernanceStatement(
            trace_id="demo10",
            agent="triage_agent",
            stage="triage_governance",
            status="allow",
            summary="Correct trace.",
        )) == "event-id"

    assert len(calls) == 1
    assert calls[0][1] == {"followup_claim_token": "claim-token"}


def _followup_payload(case_id: str = "demo10") -> dict:
    case = load_demo_catalog().get(case_id)
    assert case.follow_up is not None
    return case.follow_up.request_payload(case)


def test_followup_http_boundary_rejects_offline_and_non_allowlisted_cases(monkeypatch) -> None:
    client = TestClient(api.app)
    monkeypatch.setenv("REFUND_MODE", "offline")

    offline = client.post("/api/refund/demo10/follow-up", json=_followup_payload())
    wrong_case = client.post("/api/refund/demo09/follow-up", json=_followup_payload())

    assert offline.status_code == 503
    assert "requires live mode" in offline.json()["detail"]
    assert wrong_case.status_code == 422


def test_followup_http_boundary_rejects_fact_conflict_before_service(monkeypatch) -> None:
    monkeypatch.setenv("REFUND_MODE", "live")
    monkeypatch.setattr(
        api,
        "_create_followup_service",
        lambda _mode: pytest.fail("mismatched fixture facts must not reach the service"),
    )
    payload = _followup_payload()
    payload["requested_amount"] = 1.0

    response = TestClient(api.app).post("/api/refund/demo10/follow-up", json=payload)

    assert response.status_code == 422
    assert "exactly match" in response.json()["detail"]


def test_followup_http_boundary_returns_proven_live_result(monkeypatch) -> None:
    case = load_demo_catalog().get("demo14")
    assert case.follow_up is not None
    result = {
        "success": True,
        "matched_expectations": True,
        "case_id": case.trace_id,
        "trace_id": case.trace_id,
        "ticket_id": case.ticket_id,
        "customer_id": case.customer_id,
        "order_id": case.order_id,
        "message": case.follow_up.message,
        "persistence": {"database": "final", "matched": True},
    }

    class Service:
        def run(self, selected):
            assert selected == case
            return deepcopy(result)

    monkeypatch.setenv("REFUND_MODE", "live")
    monkeypatch.setenv("REFUND_DB", "real")
    monkeypatch.setattr(api, "_create_followup_service", lambda mode: Service())

    response = TestClient(api.app).post(
        "/api/refund/demo14/follow-up",
        json=case.follow_up.request_payload(case),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_boundary"] == {
        "entrypoint": "refund_followup_http_api",
        "database": "final",
        "azure": "real",
        "continuation": "customer_to_triage",
    }
    assert body["selected_case"]["ticket_id"] == "ticket-demo14"
