from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

import agents.policy as policy_package
import agents.response as response_package
import app.review as review_module
from app.review import HumanApprovalService, ReviewContinuationError
from db.database import (
    CloudDatabaseError,
    GCPRepository,
    HumanApprovalConflictError,
    HumanApprovalResolution,
)


def _resolution(
    *,
    decision: str = "approve",
    next_agent: str = "refund_agent",
    continuation_complete: bool = False,
) -> HumanApprovalResolution:
    amount = 79.99 if next_agent == "refund_agent" else None
    status = "rejected" if decision == "deny" else "approved"
    return HumanApprovalResolution(
        approval_id="approval-demo01",
        trace_id="demo01",
        ticket_id="ticket-demo01",
        status=status,
        decision=decision,
        resolved_amount=amount,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
        next_agent=next_agent,
        review_trigger_stage="policy",
        state={
            "trace_id": "demo01",
            "ticket_id": "ticket-demo01",
            "user_id": "customer-demo01",
            "message": "Keyboard arrived cracked.",
            "requested_order_id": "order-demo01",
            "order_lookup_result": {
                "order_id": "order-demo01",
                "amount_paid": 79.99,
                "prior_refund_total": 0,
                "currency": "USD",
            },
            "policy_decision": {
                "decision": decision,
                "refund_amount": amount or 0,
                "reason": "Human decision",
            },
            "human_review": {
                "approval_id": "approval-demo01",
                "status": status,
                "approved_next_agent": next_agent if status == "approved" else None,
                "rejected_next_agent": next_agent if status == "rejected" else None,
            },
            "workflow_status": "completed" if continuation_complete else "running",
        },
        idempotent=continuation_complete,
        continuation_complete=continuation_complete,
        continuation_resumable=not continuation_complete,
    )


class ServiceRepository:
    def __init__(self, resolution: HumanApprovalResolution) -> None:
        self.resolution = resolution
        self.refunds: list[dict[str, Any]] = []
        self.marks: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []

    def resolve_human_approval(self, **_kwargs: Any) -> HumanApprovalResolution:
        return self.resolution

    def persist_refund_result(self, **kwargs: Any) -> tuple[str, str]:
        self.refunds.append(kwargs)
        return "refund-demo01", "handoff-refund-demo01"

    def mark_human_approval_continuation(self, **kwargs: Any) -> bool:
        self.marks.append(kwargs)
        return True

    def record_human_approval_continuation_failure(self, **kwargs: Any) -> None:
        self.failures.append(kwargs)

    def ensure_human_approval(self, **kwargs: Any) -> str:
        self.approvals.append(kwargs)
        return "approval-demo01-response-2"


def _successful_refund(_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "refund_result": {
            "status": "success",
            "refund_id": "RF-ticket-demo01",
            "amount": 79.99,
            "currency": "USD",
            "order_id": "order-demo01",
        },
        "workflow_status": "running",
    }


def _completed_response(_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "response_result": {
            "final_outcome": "approved",
            "workflow_status": "completed",
            "response": {"body": "Your refund was issued."},
        },
        "response_handoff": "end",
        "final_outcome": "approved",
        "workflow_status": "completed",
    }


def test_approved_review_runs_refund_persistence_then_response() -> None:
    repository = ServiceRepository(_resolution())
    calls: list[str] = []

    def refund_runner(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("refund")
        return _successful_refund(state)

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        assert state["refund_persistence_result"]["transaction_id"] == "refund-demo01"
        calls.append("response")
        return _completed_response(state)

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=refund_runner,
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )

    assert calls == ["refund", "response"]
    assert len(repository.refunds) == 1
    assert repository.refunds[0]["policy_decision"]["refund_amount"] == 79.99
    assert outcome.continuation_status == "completed"
    assert outcome.workflow_status == "completed"
    assert repository.marks[0]["current_agent"] == "completed"
    assert repository.failures == []


def test_denied_review_routes_to_response_without_refund() -> None:
    resolution = _resolution(decision="deny", next_agent="response_agent")
    repository = ServiceRepository(resolution)
    seen: list[dict[str, Any]] = []

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        seen.append(state)
        assert state["human_review"]["status"] == "rejected"
        return {
            **_completed_response(state),
            "final_outcome": "denied",
        }

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=lambda _state: pytest.fail("refund must not run"),
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="deny",
        resolved_amount=None,
        reviewer="reviewer@example.com",
        notes="The evidence does not support a refund.",
    )

    assert len(seen) == 1
    assert repository.refunds == []
    assert outcome.status == "rejected"
    assert outcome.workflow_status == "completed"


def test_approved_response_release_preserves_underlying_policy_decision() -> None:
    resolution = _resolution(next_agent="response_agent")
    resolution.state["policy_decision"] = {
        "decision": "deny",
        "refund_amount": 0,
        "reason": "The item is outside policy.",
    }
    repository = ServiceRepository(resolution)

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        assert state["human_review"] == {}
        assert state["policy_decision"]["decision"] == "deny"
        return {
            **_completed_response(state),
            "final_outcome": "denied",
        }

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=None,
        reviewer="reviewer@example.com",
        notes="The response may be released after review.",
    )

    assert outcome.workflow_status == "completed"
    assert repository.refunds == []


def test_triage_approval_continues_policy_then_refund_and_response() -> None:
    repository = ServiceRepository(_resolution(next_agent="policy_agent"))
    calls: list[str] = []

    def policy_runner(_state: dict[str, Any]) -> dict[str, Any]:
        calls.append("policy")
        return {
            "policy_decision": {
                "decision": "approve",
                "refund_amount": 79.99,
                "reason": "Damaged item is eligible.",
            },
            "policy_persistence_result": {"next_agent": "refund_agent"},
            "workflow_status": "running",
        }

    def refund_runner(state: dict[str, Any]) -> dict[str, Any]:
        assert state["policy_decision"]["decision"] == "approve"
        calls.append("refund")
        return _successful_refund(state)

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        assert state["refund_persistence_result"]["transaction_id"] == "refund-demo01"
        calls.append("response")
        return _completed_response(state)

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        policy_runner=policy_runner,
        refund_runner=refund_runner,
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=None,
        reviewer="security-reviewer@example.com",
        notes="The triage governance concern was reviewed and cleared.",
    )

    assert calls == ["policy", "refund", "response"]
    assert outcome.workflow_status == "completed"
    assert len(repository.refunds) == 1


def test_triage_approval_continues_policy_then_response_only() -> None:
    repository = ServiceRepository(_resolution(next_agent="policy_agent"))
    calls: list[str] = []

    def policy_runner(_state: dict[str, Any]) -> dict[str, Any]:
        calls.append("policy")
        return {
            "policy_decision": {
                "decision": "deny",
                "refund_amount": 0,
                "reason": "Policy does not allow this refund.",
            },
            "policy_persistence_result": {"next_agent": "response_agent"},
            "workflow_status": "running",
        }

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        assert state["policy_decision"]["decision"] == "deny"
        assert state["human_review"] == {}
        calls.append("response")
        return {
            **_completed_response(state),
            "final_outcome": "denied",
        }

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        policy_runner=policy_runner,
        refund_runner=lambda _state: pytest.fail("refund must not run"),
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=None,
        reviewer="security-reviewer@example.com",
        notes="The triage governance concern was reviewed and cleared.",
    )

    assert calls == ["policy", "response"]
    assert repository.refunds == []
    assert outcome.workflow_status == "completed"


def test_triage_approval_continues_policy_to_new_pending_review_without_recursion() -> None:
    repository = ServiceRepository(_resolution(next_agent="policy_agent"))
    policy_calls = 0

    def policy_runner(_state: dict[str, Any]) -> dict[str, Any]:
        nonlocal policy_calls
        policy_calls += 1
        return {
            "policy_decision": {
                "decision": "manual_review",
                "refund_amount": 0,
                "reason": "The remaining amount needs manager approval.",
            },
            "policy_persistence_result": {"next_agent": "human_approval"},
            "workflow_status": "waiting_human",
        }

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        policy_runner=policy_runner,
        refund_runner=lambda _state: pytest.fail("refund must not run"),
        response_runner=lambda _state: pytest.fail("response must not run"),
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=None,
        reviewer="security-reviewer@example.com",
        notes="The triage governance concern was reviewed and cleared.",
    )

    assert policy_calls == 1
    assert len(repository.approvals) == 1
    assert repository.approvals[0]["stage"] == "policy"
    assert repository.approvals[0]["reason"] == "The remaining amount needs manager approval."
    assert outcome.continuation_status == "pending_human"
    assert outcome.new_approval_id == "approval-demo01-response-2"
    assert repository.marks[0]["workflow_status"] == "pending_human"


def test_response_governance_escalation_creates_pending_review_and_stops() -> None:
    repository = ServiceRepository(_resolution(decision="deny", next_agent="response_agent"))
    response_calls = 0

    def blocked_response(_state: dict[str, Any]) -> dict[str, Any]:
        nonlocal response_calls
        response_calls += 1
        return {
            "response_result": {"workflow_status": "waiting_human"},
            "response_governance_result": {"status": "block"},
            "response_handoff": "human_review",
            "review_trigger_reason": "governance_block",
            "workflow_status": "waiting_human",
        }

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        response_runner=blocked_response,
    ).resolve(
        "demo01",
        decision="deny",
        resolved_amount=None,
        reviewer="reviewer@example.com",
        notes="Deny this request.",
    )

    assert response_calls == 1
    assert len(repository.approvals) == 1
    assert repository.approvals[0]["stage"] == "response"
    assert outcome.continuation_status == "pending_human"
    assert outcome.workflow_status == "pending_human"
    assert outcome.new_approval_id == "approval-demo01-response-2"
    assert repository.marks[0]["workflow_status"] == "pending_human"


def test_identical_completed_resolution_does_not_run_agents_again() -> None:
    repository = ServiceRepository(_resolution(continuation_complete=True))
    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=lambda _state: pytest.fail("refund must not rerun"),
        response_runner=lambda _state: pytest.fail("response must not rerun"),
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )

    assert outcome.continuation_status == "already_completed"
    assert outcome.idempotent is True
    assert repository.marks == []


def test_fresh_duplicate_claim_reports_in_progress_without_duplicate_side_effects() -> None:
    resolution = replace(
        _resolution(),
        idempotent=True,
        continuation_resumable=False,
    )
    repository = ServiceRepository(resolution)
    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=lambda _state: pytest.fail("fresh claim must not rerun refund"),
        response_runner=lambda _state: pytest.fail("fresh claim must not rerun response"),
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )

    assert outcome.continuation_status == "in_progress"
    assert repository.refunds == []
    assert repository.marks == []


def test_missing_marker_after_terminal_commit_is_repaired_without_rerunning_refund() -> None:
    resolution = replace(
        _resolution(),
        idempotent=True,
        continuation_resumable=True,
        state={**_resolution().state, "workflow_status": "completed", "current_stage": "completed"},
    )
    repository = ServiceRepository(resolution)
    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=lambda _state: pytest.fail("committed refund must not rerun"),
        response_runner=lambda _state: pytest.fail("committed response must not rerun"),
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )

    assert outcome.continuation_status == "recovered"
    assert repository.refunds == []
    assert repository.marks[0]["workflow_status"] == "completed"
    assert repository.marks[0]["summary"]["recovered_terminal_marker"] is True


def test_stale_claim_resumes_after_process_death_and_finishes_idempotently() -> None:
    resolution = replace(
        _resolution(),
        idempotent=True,
        continuation_resumable=True,
    )
    repository = ServiceRepository(resolution)
    calls: list[str] = []

    def refund_runner(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("refund")
        return _successful_refund(state)

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("response")
        return _completed_response(state)

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=refund_runner,
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )

    assert calls == ["refund", "response"]
    assert outcome.continuation_status == "completed"
    assert len(repository.refunds) == 1
    assert len(repository.marks) == 1


def test_continuation_failure_is_persisted_and_fails_closed() -> None:
    repository = ServiceRepository(_resolution(decision="deny", next_agent="response_agent"))

    def broken_response(_state: dict[str, Any]) -> dict[str, Any]:
        raise TimeoutError("response timed out")

    service = HumanApprovalService(repository, response_runner=broken_response)  # type: ignore[arg-type]
    with pytest.raises(ReviewContinuationError, match="demo01"):
        service.resolve(
            "demo01",
            decision="deny",
            resolved_amount=None,
            reviewer="reviewer@example.com",
            notes="Deny this request.",
        )
    assert len(repository.failures) == 1
    assert isinstance(repository.failures[0]["error"], TimeoutError)


def test_default_policy_and_response_subgraphs_share_one_lazy_azure_client(monkeypatch) -> None:
    repository = ServiceRepository(_resolution(next_agent="policy_agent"))
    azure = object()
    captured: dict[str, Any] = {}

    class Graph:
        def __init__(self, name: str) -> None:
            self.name = name

        def invoke(self, _state: dict[str, Any]) -> dict[str, Any]:
            return {"runner": self.name}

    def build_policy(client: Any, *, store: Any) -> Graph:
        captured["policy_client"] = client
        captured["policy_store"] = store
        return Graph("policy")

    def build_response(*, client: Any, store: Any, event_writer: Any) -> Graph:
        captured["response_client"] = client
        captured["response_store"] = store
        captured["event_writer"] = event_writer
        return Graph("response")

    monkeypatch.setattr(review_module.AzureJsonClient, "from_env", lambda: azure)
    monkeypatch.setattr(policy_package, "build_policy_agent_graph", build_policy)
    monkeypatch.setattr(response_package, "build_response_agent_graph", build_response)
    service = HumanApprovalService(repository)  # type: ignore[arg-type]

    assert service._run_default_policy({}) == {"runner": "policy"}
    assert service._run_default_response({}) == {"runner": "response"}
    assert captured["policy_client"] is azure
    assert captured["response_client"] is azure


class DatabaseStore:
    def __init__(self) -> None:
        self.approval = {
            "approval_id": "approval-demo07",
            "trace_id": "demo07",
            "triggering_event_id": "policy-review-demo07",
            "triggering_event_type": "policy_review",
            "reason": "Manager review required.",
            "amount_requested": Decimal("299.99"),
            "resolved_amount": None,
            "status": "pending",
            "decision": None,
            "approved_next_agent": "refund_agent",
            "rejected_next_agent": "response_agent",
            "reviewer": None,
            "notes": '{"review_source":"policy_review"}',
            "resolved_at": None,
            "ticket_id": "ticket-demo07",
            "workflow_status": "pending_human",
            "current_agent": "human_approval",
            "policy_version": "v1.0",
            "customer_id": "customer-demo07",
            "raw_text": "Monitor arrived broken.",
            "sanitized_text": "Monitor arrived broken.",
            "refund_reason": "damaged",
            "requested_amount": Decimal("299.99"),
            "ticket_currency": "USD",
            "customer_email": "sara@example.com",
            "customer_name": "Sara",
            "governance_agent": None,
        }
        self.workflow = {"status": "pending_human", "current_agent": "human_approval"}
        self.handoffs = [
            {
                "handoff_id": "policy-demo07",
                "from_agent": "policy_agent",
                "to_agent": "human_approval",
                "input_json": "{}",
                "output_json": json.dumps(
                    {
                        "decision": {
                            "type": "manual_review",
                            "refund_amount": 299.99,
                            "reason": "Manager review required.",
                        }
                    }
                ),
            }
        ]
        self.orders = [
            {
                "order_id": "order-demo07",
                "order_customer_id": "customer-demo07",
                "product_type": "monitor",
                "purchase_date": "2026-06-12",
                "item_status": "damaged",
                "amount_paid": Decimal("299.99"),
                "prior_refund_total": Decimal("100.00"),
                "currency": "USD",
                "contact_customer_id": "customer-demo07",
                "contact_email": "sara@example.com",
                "contact_name": "Sara",
            }
        ]
        self.audit: list[dict[str, Any]] = []
        self.claim_age_seconds = 0
        self.next_log_id = 1
        self.approval_handoffs: list[tuple[Any, ...]] = []
        self.queries: list[str] = []


class DatabaseCursor:
    def __init__(self, store: DatabaseStore, *, dictionary: bool) -> None:
        self.store = store
        self.dictionary = dictionary
        self.rows: list[Any] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        compact = " ".join(sql.split())
        self.store.queries.append(compact)
        self.rowcount = 0
        if "FROM human_approvals approvals" in compact:
            row = dict(self.store.approval)
            row["workflow_status"] = self.store.workflow["status"]
            row["current_agent"] = self.store.workflow["current_agent"]
            self.rows = [row]
        elif "FROM agent_handoffs" in compact and compact.startswith("SELECT"):
            self.rows = [dict(row) for row in self.store.handoffs]
        elif "FROM orders" in compact and compact.startswith("SELECT"):
            self.rows = [dict(row) for row in self.store.orders]
        elif compact.startswith("UPDATE human_approvals"):
            if self.store.approval["status"] != "pending":
                self.rowcount = 0
            else:
                (
                    self.store.approval["status"],
                    self.store.approval["decision"],
                    self.store.approval["resolved_amount"],
                    self.store.approval["reviewer"],
                    self.store.approval["notes"],
                    _approval_id,
                ) = params
                self.store.approval["resolved_at"] = "now"
                self.rowcount = 1
            self.rows = []
        elif compact.startswith("INSERT INTO agent_handoffs"):
            self.store.approval_handoffs.append(params)
            self.rowcount = 1
            self.rows = []
        elif compact.startswith("INSERT INTO audit_log"):
            event_type = next(
                value
                for value in (
                    "human_approval_continuation_claimed",
                    "human_approval_continued",
                    "human_approval_resolved",
                )
                if value in compact
            )
            self.store.audit.append(
                {
                    "log_id": self.store.next_log_id,
                    "event_type": event_type,
                    "payload_json": params[1],
                }
            )
            self.store.next_log_id += 1
            self.rowcount = 1
            self.rows = []
        elif compact.startswith("UPDATE audit_log"):
            payload, log_id, _trace_id = params
            row = next(item for item in self.store.audit if item["log_id"] == log_id)
            row["payload_json"] = payload
            self.store.claim_age_seconds = 0
            self.rowcount = 1
            self.rows = []
        elif compact.startswith("UPDATE workflow_runs"):
            if len(params) == 4:
                self.store.workflow["status"] = params[0]
                self.store.workflow["current_agent"] = params[1]
            self.rowcount = 1
            self.rows = []
        elif "event_type = 'human_approval_continued'" in compact:
            self.rows = [
                {"payload_json": row["payload_json"]}
                for row in self.store.audit
                if row["event_type"] == "human_approval_continued"
            ]
        elif "event_type = 'human_approval_continuation_claimed'" in compact:
            claims = [
                row
                for row in self.store.audit
                if row["event_type"] == "human_approval_continuation_claimed"
            ]
            self.rows = (
                [
                    {
                        "log_id": claims[-1]["log_id"],
                        "payload_json": claims[-1]["payload_json"],
                        "created_at": "now",
                        "age_seconds": self.store.claim_age_seconds,
                    }
                ]
                if claims
                else []
            )
        elif compact.startswith("SELECT approval_id, status FROM human_approvals"):
            self.rows = [
                {
                    "approval_id": self.store.approval["approval_id"],
                    "status": self.store.approval["status"],
                }
            ]
        elif compact.startswith("SELECT COUNT(*) FROM workflow_runs"):
            self.rows = [{"COUNT(*)": 1}] if self.dictionary else [(1,)]
        else:
            raise AssertionError(f"Unexpected SQL: {compact}")

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None


class DatabaseConnection:
    def __init__(self, store: DatabaseStore) -> None:
        self.store = store
        self.started = False
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def start_transaction(self) -> None:
        self.started = True

    def cursor(self, dictionary: bool = False) -> DatabaseCursor:
        return DatabaseCursor(self.store, dictionary=dictionary)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _database_repository(store: DatabaseStore, connections: list[DatabaseConnection]) -> GCPRepository:
    repository = GCPRepository({"database": "final"})

    def connect() -> DatabaseConnection:
        connection = DatabaseConnection(store)
        connections.append(connection)
        return connection

    repository._connect = connect  # type: ignore[method-assign]
    return repository


def test_repository_locks_and_atomically_resolves_pending_approval() -> None:
    store = DatabaseStore()
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)

    result = repository.resolve_human_approval(
        trace_id="demo07",
        decision="partial_refund",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the remaining refundable balance.",
    )

    assert connections[0].started and connections[0].committed
    assert not connections[0].rolled_back
    assert any("FOR UPDATE" in query and "human_approvals approvals" in query for query in store.queries)
    assert store.approval["status"] == "approved"
    assert store.approval["decision"] == "partial_refund"
    assert store.workflow == {"status": "running", "current_agent": "refund_agent"}
    assert len(store.approval_handoffs) == 1
    assert store.approval_handoffs[0][0] == "39751ff1-53c2-5fcf-b120-7eee490770a4"
    assert [row["event_type"] for row in store.audit] == [
        "human_approval_resolved",
        "human_approval_continuation_claimed",
    ]
    assert result.state["requested_order_id"] == "order-demo07"
    assert result.state["policy_decision"]["refund_amount"] == 199.99
    assert result.idempotent is False


def test_repository_allows_triage_approval_to_claim_policy_continuation() -> None:
    store = DatabaseStore()
    store.approval["approved_next_agent"] = "policy_agent"
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)

    result = repository.resolve_human_approval(
        trace_id="demo07",
        decision="approve",
        resolved_amount=None,
        reviewer="security-reviewer@example.com",
        notes="The triage governance concern was reviewed and cleared.",
    )

    assert result.next_agent == "policy_agent"
    assert store.workflow == {"status": "running", "current_agent": "policy_agent"}
    assert store.approval_handoffs[0][3] == "policy_agent"


def test_repository_same_resolution_is_idempotent_but_conflict_is_rejected() -> None:
    store = DatabaseStore()
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)
    request = {
        "trace_id": "demo07",
        "decision": "partial_refund",
        "resolved_amount": Decimal("199.99"),
        "reviewer": "manager@example.com",
        "notes": "Approve the remaining refundable balance.",
    }
    repository.resolve_human_approval(**request)
    repeated = repository.resolve_human_approval(**request)

    assert repeated.idempotent is True
    assert len(store.approval_handoffs) == 1
    assert [row["event_type"] for row in store.audit] == [
        "human_approval_resolved",
        "human_approval_continuation_claimed",
    ]

    with pytest.raises(HumanApprovalConflictError, match="different decision"):
        repository.resolve_human_approval(
            trace_id="demo07",
            decision="deny",
            resolved_amount=None,
            reviewer="manager@example.com",
            notes="Deny instead.",
        )
    assert connections[-1].rolled_back is True


def test_repository_refreshes_only_a_stale_missing_terminal_claim() -> None:
    store = DatabaseStore()
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)
    request = {
        "trace_id": "demo07",
        "decision": "partial_refund",
        "resolved_amount": Decimal("199.99"),
        "reviewer": "manager@example.com",
        "notes": "Approve the remaining refundable balance.",
        "continuation_stale_after_seconds": 30,
    }

    first = repository.resolve_human_approval(**request)
    immediate = repository.resolve_human_approval(**request)
    assert first.continuation_resumable is True
    assert immediate.idempotent is True
    assert immediate.continuation_complete is False
    assert immediate.continuation_resumable is False

    store.claim_age_seconds = 31
    recovered = repository.resolve_human_approval(**request)
    assert recovered.idempotent is True
    assert recovered.continuation_resumable is True
    claims = [
        row for row in store.audit
        if row["event_type"] == "human_approval_continuation_claimed"
    ]
    assert len(claims) == 1
    assert json.loads(claims[0]["payload_json"])["attempt"] == 2


def test_repository_continuation_marker_is_durable_and_idempotent() -> None:
    store = DatabaseStore()
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)
    request = {
        "trace_id": "demo07",
        "decision": "partial_refund",
        "resolved_amount": Decimal("199.99"),
        "reviewer": "manager@example.com",
        "notes": "Approve the remaining refundable balance.",
    }
    first = repository.resolve_human_approval(**request)

    assert repository.mark_human_approval_continuation(
        trace_id="demo07",
        approval_id=first.approval_id,
        workflow_status="completed",
        current_agent="completed",
        summary={"final_outcome": "partial_refund"},
    ) is True
    assert repository.mark_human_approval_continuation(
        trace_id="demo07",
        approval_id=first.approval_id,
        workflow_status="completed",
        current_agent="completed",
        summary={"final_outcome": "partial_refund"},
    ) is False

    repeated = repository.resolve_human_approval(**request)
    assert repeated.idempotent is True
    assert repeated.continuation_complete is True
    assert repeated.state["workflow_status"] == "completed"
    assert len(
        [row for row in store.audit if row["event_type"] == "human_approval_continued"]
    ) == 1


@pytest.mark.parametrize("trace_id", ["demo00", "demo21", "TRACE-POL-001", "demo01-extra"])
def test_repository_rejects_every_trace_outside_exact_demo_allowlist(trace_id: str) -> None:
    repository = GCPRepository({"database": "final"})
    with pytest.raises(ValueError, match="demo01 through demo20"):
        repository.resolve_human_approval(
            trace_id=trace_id,
            decision="deny",
            resolved_amount=None,
            reviewer="manager@example.com",
            notes="Out of scope.",
        )


@pytest.mark.parametrize(
    ("decision", "amount", "reviewer", "notes", "message"),
    [
        ("maybe", None, "manager", "Reviewed", "decision must be"),
        ("deny", None, "", "Reviewed", "reviewer is required"),
        ("deny", None, "manager", "", "notes are required"),
        ("approve", 1.001, "manager", "Reviewed", "two decimal places"),
    ],
)
def test_repository_validates_resolution_fields_before_connecting(
    decision: str,
    amount: float | None,
    reviewer: str,
    notes: str,
    message: str,
) -> None:
    repository = GCPRepository({"database": "final"})
    with pytest.raises(ValueError, match=message):
        repository.resolve_human_approval(
            trace_id="demo01",
            decision=decision,
            resolved_amount=amount,
            reviewer=reviewer,
            notes=notes,
        )


class PolicyApprovalCursor:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount = 1

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self) -> list[tuple[str, str]]:
        return list(self.rows)


class PolicyApprovalConnection:
    def __init__(self, cursor: PolicyApprovalCursor) -> None:
        self.value = cursor
        self.committed = False
        self.rolled_back = False

    def start_transaction(self) -> None:
        return None

    def cursor(self, **_kwargs: Any) -> PolicyApprovalCursor:
        return self.value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


def _policy_approval_output(next_agent: str = "human_approval") -> Any:
    return SimpleNamespace(
        case=SimpleNamespace(trace_id="demo07"),
        handoff=SimpleNamespace(next_agent=next_agent, reason="Manager review required."),
        decision=SimpleNamespace(type="manual_review", reason="Evidence conflicts."),
        customer_request=SimpleNamespace(requested_amount=199.99),
        response_guidance=SimpleNamespace(customer_safe_summary="A manager will review this."),
        governance=SimpleNamespace(flags=[]),
    )


def test_policy_persistence_rejects_resolved_history_before_rewriting_dependencies() -> None:
    cursor = PolicyApprovalCursor([("POL-APP-existing", "approved")])
    connection = PolicyApprovalConnection(cursor)
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: connection  # type: ignore[method-assign]

    with pytest.raises(HumanApprovalConflictError, match="resolved Policy approval history"):
        repository.persist_result(
            SimpleNamespace(case=SimpleNamespace(trace_id="demo07")),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            [],
            SimpleNamespace(),
        )

    assert connection.rolled_back and not connection.committed
    assert len(cursor.executed) == 1
    assert cursor.executed[0][0].startswith("SELECT approval_id, status")


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_policy_rerun_never_resets_or_deletes_resolved_policy_approval(status: str) -> None:
    cursor = PolicyApprovalCursor([("POL-APP-existing", status)])
    repository = GCPRepository({"database": "final"})

    with pytest.raises(HumanApprovalConflictError, match="resolved Policy approval history"):
        repository._persist_human_approval(  # type: ignore[attr-defined]
            cursor,
            _policy_approval_output(next_agent="response_agent"),
            None,
            [],
        )

    mutation_sql = [
        sql for sql, _params in cursor.executed
        if sql.startswith("DELETE") or sql.startswith("INSERT") or sql.startswith("UPDATE")
    ]
    assert mutation_sql == []


def test_pending_policy_approval_refresh_clears_every_resolution_field() -> None:
    cursor = PolicyApprovalCursor([("POL-APP-existing", "pending")])
    repository = GCPRepository({"database": "final"})

    repository._persist_human_approval(  # type: ignore[attr-defined]
        cursor,
        _policy_approval_output(),
        "POL-REV-demo07",
        [],
    )

    insert_sql, params = next(
        (sql, params) for sql, params in cursor.executed
        if sql.startswith("INSERT INTO human_approvals")
    )
    assert params[0] == "POL-APP-existing"
    assert "decision = NULL" in insert_sql
    assert "resolved_amount = NULL" in insert_sql
    assert "reviewer = NULL" in insert_sql
    assert "resolved_at = NULL" in insert_sql


def test_non_review_policy_rerun_deletes_only_pending_policy_approval() -> None:
    cursor = PolicyApprovalCursor([("POL-APP-existing", "pending")])
    repository = GCPRepository({"database": "final"})

    repository._persist_human_approval(  # type: ignore[attr-defined]
        cursor,
        _policy_approval_output(next_agent="response_agent"),
        None,
        [],
    )

    delete_sql = next(sql for sql, _params in cursor.executed if sql.startswith("DELETE"))
    assert "status = 'pending'" in delete_sql


class EnsureApprovalCursor:
    def __init__(self, existing_approval_id: str | None = None) -> None:
        self.rows: list[Any] = []
        self.rowcount = 1
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.insert_params: tuple[Any, ...] | None = None
        self.existing_approval_id = existing_approval_id
        self.events = {
            "triage_agent": "governance-triage",
            "policy_agent": "governance-policy",
            "response_agent": "governance-response",
        }

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        compact = " ".join(sql.split())
        self.executed.append((compact, params))
        self.rowcount = 1
        if compact.startswith("SELECT approval_id FROM human_approvals"):
            self.rows = (
                [(self.existing_approval_id,)] if self.existing_approval_id else []
            )
        elif compact.startswith("SELECT event_id FROM governance_events"):
            self.rows = [(self.events[params[1]],)]
        elif compact.startswith("SELECT tickets.requested_amount"):
            self.rows = [(Decimal("49.99"),)]
        elif compact.startswith("SELECT COUNT(*) FROM human_approvals"):
            self.rows = [(0,)]
        elif compact.startswith("INSERT INTO human_approvals"):
            self.insert_params = params
            self.rows = []
        elif compact.startswith("UPDATE workflow_runs"):
            self.rows = []
        else:
            raise AssertionError(f"Unexpected SQL: {compact}")

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None


class EnsureApprovalConnection:
    def __init__(self, cursor: EnsureApprovalCursor) -> None:
        self.value = cursor
        self.committed = False
        self.rolled_back = False

    def start_transaction(self) -> None:
        return None

    def cursor(self, **_kwargs: Any) -> EnsureApprovalCursor:
        return self.value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("stage", "expected_agent", "expected_event"),
    [
        ("triage", "triage_agent", "governance-triage"),
        ("response", "response_agent", "governance-response"),
    ],
)
def test_ensure_human_approval_binds_governance_trigger_to_requested_stage(
    stage: str,
    expected_agent: str,
    expected_event: str,
) -> None:
    cursor = EnsureApprovalCursor()
    connection = EnsureApprovalConnection(cursor)
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: connection  # type: ignore[method-assign]

    approval_id = repository.ensure_human_approval(
        trace_id="demo18",
        reason="governance_block",
        stage=stage,
        policy_decision={},
    )

    governance_query, governance_params = next(
        (sql, params) for sql, params in cursor.executed
        if sql.startswith("SELECT event_id FROM governance_events")
    )
    assert governance_params == ("demo18", expected_agent)
    assert "ORDER BY created_at DESC, event_id DESC" in governance_query
    assert cursor.insert_params is not None
    assert cursor.insert_params[2:4] == (expected_event, "governance")
    assert approval_id
    assert connection.committed and not connection.rolled_back


def test_ensure_policy_approval_returns_existing_pending_row_before_trigger_lookup() -> None:
    cursor = EnsureApprovalCursor(existing_approval_id="POL-APP-existing")
    connection = EnsureApprovalConnection(cursor)
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: connection  # type: ignore[method-assign]

    approval_id = repository.ensure_human_approval(
        trace_id="demo13",
        reason="manual_review",
        stage="policy",
        policy_decision={"decision": "manual_review"},
    )

    assert approval_id == "POL-APP-existing"
    assert len(cursor.executed) == 1
    assert cursor.executed[0][0].startswith("SELECT approval_id FROM human_approvals")
    assert connection.committed and not connection.rolled_back


class GenericHandoffCursor:
    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.rowcount = 1
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        compact = " ".join(sql.split())
        self.executed.append((compact, params))
        self.rowcount = 1
        if compact.startswith("SELECT handoff_id FROM agent_handoffs"):
            self.rows = []
        else:
            self.rows = []

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None


class GenericHandoffConnection:
    def __init__(self, cursor: GenericHandoffCursor) -> None:
        self.value = cursor
        self.committed = False
        self.rolled_back = False

    def start_transaction(self) -> None:
        return None

    def cursor(self, **_kwargs: Any) -> GenericHandoffCursor:
        return self.value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("workflow_status", "current_agent"),
    [("completed", "completed"), ("running", "response_agent")],
)
def test_generic_handoff_sets_completed_at_only_for_completed_workflow(
    workflow_status: str,
    current_agent: str,
) -> None:
    cursor = GenericHandoffCursor()
    connection = GenericHandoffConnection(cursor)
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: connection  # type: ignore[method-assign]

    repository.persist_agent_handoff(
        trace_id="demo01",
        ticket_id="ticket-demo01",
        from_agent="response_agent",
        to_agent="end",
        input_payload={},
        output_payload={},
        audit_event_type="response_agent_evaluated",
        workflow_status=workflow_status,
        current_agent=current_agent,
    )

    update_sql, update_params = next(
        (sql, params) for sql, params in cursor.executed
        if sql.startswith("UPDATE workflow_runs")
    )
    assert "completed_at = CASE WHEN %s = 'completed' THEN CURRENT_TIMESTAMP ELSE NULL END" in update_sql
    assert update_params == (
        workflow_status,
        current_agent,
        workflow_status,
        "demo01",
    )
    assert connection.committed and not connection.rolled_back


class WorkflowFailureStore:
    def __init__(self) -> None:
        self.ticket_id = "ticket-demo10"
        self.audit: list[dict[str, Any]] = []
        self.workflow = {"status": "running", "current_agent": "policy_agent"}
        self.queries: list[str] = []


class WorkflowFailureCursor:
    def __init__(self, store: WorkflowFailureStore) -> None:
        self.store = store
        self.rows: list[Any] = []
        self.rowcount = 1
        self.lastrowid = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        compact = " ".join(sql.split())
        self.store.queries.append(compact)
        self.rowcount = 1
        if compact.startswith("SELECT ticket_id FROM workflow_runs"):
            self.rows = [(self.store.ticket_id,)]
        elif compact.startswith("SELECT log_id FROM audit_log"):
            self.rows = [(row["log_id"],) for row in self.store.audit]
        elif compact.startswith("INSERT INTO audit_log"):
            self.lastrowid = 701
            self.store.audit.append({"log_id": 701, "payload_json": params[1]})
            self.rows = []
        elif compact.startswith("UPDATE audit_log"):
            payload, log_id, _trace_id = params
            row = next(item for item in self.store.audit if item["log_id"] == log_id)
            row["payload_json"] = payload
            self.rows = []
        elif compact.startswith("UPDATE workflow_runs"):
            self.store.workflow = {"status": "failed", "current_agent": "failed"}
            self.rows = []
        else:
            raise AssertionError(f"Unexpected SQL: {compact}")

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None


class WorkflowFailureConnection:
    def __init__(self, store: WorkflowFailureStore) -> None:
        self.value = WorkflowFailureCursor(store)
        self.committed = False
        self.rolled_back = False

    def start_transaction(self) -> None:
        return None

    def cursor(self, **_kwargs: Any) -> WorkflowFailureCursor:
        return self.value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


def test_record_workflow_failure_is_atomic_and_upserts_one_trace_audit() -> None:
    store = WorkflowFailureStore()
    connections: list[WorkflowFailureConnection] = []
    repository = GCPRepository({"database": "final"})

    def connect() -> WorkflowFailureConnection:
        connection = WorkflowFailureConnection(store)
        connections.append(connection)
        return connection

    repository._connect = connect  # type: ignore[method-assign]
    first = repository.record_workflow_failure(
        trace_id="demo10",
        ticket_id="ticket-demo10",
        error_type="TimeoutError",
        error_message="model deadline exceeded",
    )
    second = repository.record_workflow_failure(
        trace_id="demo10",
        ticket_id="ticket-demo10",
        error_type="RuntimeError",
        error_message="retry also failed",
    )

    assert first == second == 701
    assert len(store.audit) == 1
    assert json.loads(store.audit[0]["payload_json"]) == {
        "ticket_id": "ticket-demo10",
        "error_type": "RuntimeError",
        "error_message": "retry also failed",
    }
    assert store.workflow == {"status": "failed", "current_agent": "failed"}
    workflow_update = next(
        query for query in store.queries
        if query.startswith("UPDATE workflow_runs")
    )
    assert "completed_at = CURRENT_TIMESTAMP" in workflow_update
    assert all(connection.committed and not connection.rolled_back for connection in connections)


def test_record_workflow_failure_rolls_back_on_ticket_mismatch() -> None:
    store = WorkflowFailureStore()
    connection = WorkflowFailureConnection(store)
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: connection  # type: ignore[method-assign]

    with pytest.raises(CloudDatabaseError, match="ticket_id does not match"):
        repository.record_workflow_failure(
            trace_id="demo10",
            ticket_id="ticket-demo11",
            error_type="TimeoutError",
            error_message="model deadline exceeded",
        )

    assert connection.rolled_back and not connection.committed
    assert store.audit == []
    assert store.workflow == {"status": "running", "current_agent": "policy_agent"}
