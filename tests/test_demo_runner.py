from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from demo.catalog import DEMO_IDS, load_demo_catalog
from demo.runner import (
    DemoRunError,
    DemoRunner,
    _matches_expectations,
    normalize_graph_state,
    verify_persisted_case,
    verify_seeded_case,
)
from refund_app.simulator import simulate_case


def test_demo07_fixture_uses_partial_refund_human_approval_defaults() -> None:
    case = load_demo_catalog().get("demo07")

    assert case.expectations.policy_decision == "partial_refund"
    assert case.expectations.policy_route == "human_approval"
    assert case.expectations.route == "human_approval"
    assert case.expectations.outcome == "partial_refund"
    assert case.expectations.terminal_state == "pending_human"


class _Repository:
    def __init__(self, database: str = "final") -> None:
        self.database_name = database
        self.connection_config = {"database": database}
        self.failures: list[dict[str, Any]] = []

    def record_workflow_failure(self, **failure: Any) -> None:
        self.failures.append(failure)


class _Graph:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []
        self.evaluation_dates: list[str | None] = []

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        self.inputs.append(state)
        self.evaluation_dates.append(os.getenv("POLICY_EVALUATION_DATE"))
        case = load_demo_catalog().get(state["trace_id"])
        expected = case.expectations
        result: dict[str, Any] = {
            "trace_id": case.trace_id,
            "ticket_id": case.ticket_id,
            "user_id": case.customer_id,
            "message": state["message"],
            "order_resolution_source": (
                "trusted_ui_selection"
                if state.get("requested_order_id")
                else "azure_tool_call"
            ),
            "order_lookup_result": {"order_id": case.order_id},
            "triage_governance_result": {
                "status": "block" if case.trace_id in {"demo12", "demo13"} else "allow"
            },
            "policy_governance_result": (
                {} if case.trace_id in {"demo12", "demo13"} else {"status": "allow"}
            ),
            "policy_decision": {"decision": expected.policy_decision},
            "policy_persistence_result": {"next_agent": expected.route},
            "final_outcome": expected.outcome,
            "workflow_status": (
                "waiting_human" if expected.terminal_state == "pending_human" else expected.terminal_state
            ),
            "response_result": {"response": {"body": f"response for {case.trace_id}"}},
            "response_governance_result": {"status": "allow"},
        }
        if expected.outcome == "refund_issued":
            result["refund_result"] = {"status": "success", "order_id": case.order_id}
            result["refund_result"]["amount"] = case.ticket["requested_amount"]
        return result


def test_offline_batch_runs_exact_twenty_and_restores_evaluation_date(monkeypatch) -> None:
    monkeypatch.setenv("POLICY_EVALUATION_DATE", "previous-value")
    observed: list[str | None] = []

    def execute(case):
        observed.append(os.getenv("POLICY_EVALUATION_DATE"))
        return simulate_case(case)

    result = DemoRunner(offline_executor=execute).run_batch()

    assert result["requested"] == 20
    assert result["successful"] == 20
    assert result["failed"] == 0
    assert result["matched_expectations"] == 20
    assert [item["case_id"] for item in result["cases"]] == list(DEMO_IDS)
    assert set(observed) == {"2026-07-01"}
    assert os.environ["POLICY_EVALUATION_DATE"] == "previous-value"
    assert all(item["timings_ms"]["total"] >= 0 for item in result["cases"])


def test_live_run_reuses_seeded_identity_and_selected_order_id(monkeypatch) -> None:
    monkeypatch.delenv("POLICY_EVALUATION_DATE", raising=False)
    graph = _Graph()
    verified: list[tuple[str, str]] = []

    def verify(repository, catalog, case) -> None:
        assert repository.database_name == "final"
        assert tuple(item.trace_id for item in catalog.cases) == DEMO_IDS
        verified.append((case.trace_id, case.order_id))

    runner = DemoRunner(
        mode="live",
        repository=_Repository(),
        graph=graph,
        seed_verifier=verify,
        result_verifier=lambda *_args: {"matched": True},
    )
    first = runner.run_case("demo18")
    second = runner.run_case("demo18")

    assert first["success"] and first["matched_expectations"]
    assert second["success"] and second["matched_expectations"]
    assert verified == [("demo18", "order-demo18"), ("demo18", "order-demo18")]
    assert [value["trace_id"] for value in graph.inputs] == ["demo18", "demo18"]
    assert graph.inputs[0] == graph.inputs[1]
    assert "order-demo99" in graph.inputs[0]["message"]
    assert graph.inputs[0]["requested_order_id"] == "order-demo18"
    assert graph.inputs[0]["request_context"]["selected_order_id"] == "order-demo18"
    assert first["order_resolution_source"] == "trusted_ui_selection"
    assert graph.evaluation_dates == ["2026-07-01", "2026-07-01"]
    assert "POLICY_EVALUATION_DATE" not in os.environ


def test_live_run_without_fixture_override_requires_azure_order_resolution() -> None:
    graph = _Graph()
    runner = DemoRunner(
        mode="live",
        repository=_Repository(),
        graph=graph,
        seed_verifier=lambda *_args: None,
        result_verifier=lambda *_args: {"matched": True},
    )

    result = runner.run_case("demo01")

    assert result["success"] and result["matched_expectations"]
    assert "requested_order_id" not in graph.inputs[0]
    assert "selected_order_id" not in graph.inputs[0]["request_context"]
    assert result["selected_order_id"] is None
    assert result["order_resolution_source"] == "azure_tool_call"


def test_batch_isolates_case_failure_and_continues() -> None:
    visited: list[str] = []

    def execute(case):
        visited.append(case.trace_id)
        if case.trace_id == "demo02":
            raise TimeoutError("simulated per-case timeout")
        return simulate_case(case)

    result = DemoRunner(offline_executor=execute).run_batch(["demo01", "demo02", "demo03"])

    assert visited == ["demo01", "demo02", "demo03"]
    assert result["successful"] == 2
    assert result["failed"] == 1
    assert result["matched_expectations"] == 2
    assert result["cases"][1]["error"] == {
        "type": "TimeoutError",
        "message": "simulated per-case timeout",
    }
    assert result["cases"][1]["timings_ms"]["workflow"] >= 0


def test_bounded_batch_workers_run_concurrently_and_preserve_case_order(monkeypatch) -> None:
    monkeypatch.setenv("POLICY_EVALUATION_DATE", "original")
    barrier = threading.Barrier(2)
    observed: list[tuple[str, str | None]] = []

    def execute(case):
        observed.append((case.trace_id, os.getenv("POLICY_EVALUATION_DATE")))
        barrier.wait(timeout=1)
        return simulate_case(case)

    result = DemoRunner(offline_executor=execute, workers=2).run_batch(
        ["demo01", "demo02"]
    )

    assert [item["case_id"] for item in result["cases"]] == ["demo01", "demo02"]
    assert result["workers"] == 2
    assert result["matched_expectations"] == 2
    assert set(observed) == {
        ("demo01", "2026-07-01"),
        ("demo02", "2026-07-01"),
    }
    assert os.environ["POLICY_EVALUATION_DATE"] == "original"


@pytest.mark.parametrize("workers", [0, 5, True, 1.5])
def test_runner_rejects_unsafe_worker_counts(workers) -> None:
    with pytest.raises(DemoRunError, match="workers"):
        DemoRunner(workers=workers)


def test_scoped_evaluation_date_is_safe_across_concurrent_requests(monkeypatch) -> None:
    monkeypatch.setenv("POLICY_EVALUATION_DATE", "original")
    first_entered = threading.Event()
    release_first = threading.Event()
    second_called = threading.Event()
    second_entered = threading.Event()

    def execute(case):
        assert os.getenv("POLICY_EVALUATION_DATE") == "2026-07-01"
        if case.trace_id == "demo01":
            first_entered.set()
            assert release_first.wait(timeout=1)
        else:
            second_entered.set()
        return simulate_case(case)

    runner = DemoRunner(offline_executor=execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runner.run_case, "demo01")
        assert first_entered.wait(timeout=1)

        def run_second():
            second_called.set()
            return runner.run_case("demo02")

        second = pool.submit(run_second)
        assert second_called.wait(timeout=1)
        assert not second_entered.wait(timeout=0.05)
        release_first.set()
        assert first.result(timeout=1)["success"]
        assert second.result(timeout=1)["success"]

    assert second_entered.is_set()
    assert os.environ["POLICY_EVALUATION_DATE"] == "original"


def test_live_runner_rejects_non_final_database_before_graph_invocation() -> None:
    graph = _Graph()
    result = DemoRunner(
        mode="live",
        repository=_Repository("main_db"),
        graph=graph,
        seed_verifier=lambda *_args: None,
    ).run_case("demo01")

    assert result["success"] is False
    assert result["error"]["type"] == "DemoRunError"
    assert "MYSQL_DATABASE=final" in result["error"]["message"]
    assert graph.inputs == []


def test_live_graph_failure_is_persisted_without_masking_original_error() -> None:
    class FailingGraph:
        def invoke(self, _state):
            raise TimeoutError("model deadline exceeded")

    repository = _Repository()
    result = DemoRunner(
        mode="live",
        repository=repository,
        graph=FailingGraph(),
        seed_verifier=lambda *_args: None,
    ).run_case("demo10")

    assert result["success"] is False
    assert result["error"] == {
        "type": "TimeoutError",
        "message": "model deadline exceeded",
    }
    assert repository.failures == [{
        "trace_id": "demo10",
        "ticket_id": "ticket-demo10",
        "error_type": "TimeoutError",
        "error_message": "model deadline exceeded",
    }]


def test_missing_observed_route_and_wrong_policy_decision_cannot_match_manifest() -> None:
    case = load_demo_catalog().get("demo01")
    actual = normalize_graph_state(case, {
        "trace_id": case.trace_id,
        "ticket_id": case.ticket_id,
        "user_id": case.customer_id,
        "order_lookup_result": {"order_id": case.order_id},
        "policy_decision": {"decision": "deny"},
        "final_outcome": "refund_issued",
        "workflow_status": "completed",
        "refund_result": {"status": "success"},
    })

    assert actual["route"] == ""
    assert _matches_expectations(case, actual) is False


def test_normalized_result_preserves_observed_message_and_checks_fixture_identity() -> None:
    case = load_demo_catalog().get("demo01")
    actual = normalize_graph_state(case, {
        "trace_id": case.trace_id,
        "ticket_id": case.ticket_id,
        "user_id": case.customer_id,
        "message": "different graph message",
        "order_resolution_source": "azure_tool_call",
        "order_lookup_result": {"order_id": case.order_id},
        "policy_decision": {"decision": "approve"},
        "policy_persistence_result": {"next_agent": "refund_agent"},
        "final_outcome": "refund_issued",
        "workflow_status": "completed",
        "refund_result": {
            "status": "success",
            "order_id": case.order_id,
            "amount": case.ticket["requested_amount"],
        },
        "triage_governance_result": {"status": "allow"},
        "policy_governance_result": {"status": "allow"},
        "response_governance_result": {"status": "allow"},
    })

    assert actual["message"] == "different graph message"
    assert actual["expected_message"] == case.message
    assert actual["order_resolution_source"] == "azure_tool_call"
    assert _matches_expectations(case, actual) is False


class _Cursor:
    def __init__(self, *, extra_trace: bool = False, row: dict[str, Any] | None = None) -> None:
        self.extra_trace = extra_trace
        self.row = row
        self.queries: list[tuple[str, Any]] = []
        self.closed = False

    def execute(self, query: str, params: Any = None) -> None:
        self.queries.append((query, params))

    def fetchall(self):
        traces = list(DEMO_IDS) + (["demo21"] if self.extra_trace else [])
        return [{"trace_id": trace} for trace in traces]

    def fetchone(self):
        return self.row

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.value = cursor
        self.closed = False

    def cursor(self, **_kwargs):
        return self.value

    def close(self) -> None:
        self.closed = True


def _clean_seed_row(case):
    return {
        "trace_id": case.trace_id,
        "ticket_id": case.ticket_id,
        "policy_version": "v1.0",
        "status": "running",
        "current_agent": "triage_agent",
        "completed_at": None,
        "customer_id": case.customer_id,
        "raw_text": case.message,
        "sanitized_text": case.ticket["sanitized_text"],
        "refund_reason": case.ticket["refund_reason"],
        "requested_amount": case.ticket["requested_amount"],
        "ticket_currency": case.ticket["currency"],
        "ticket_status": "new",
        "injection_flag": int(bool(case.ticket["injection_flag"])),
        "email": case.customer["email"],
        "full_name": case.customer["full_name"],
        "order_id": case.order_id,
        "product_type": case.order["product_type"],
        "purchase_date": case.order["purchase_date"],
        "item_status": case.order["item_status"],
        "amount_paid": case.order["amount_paid"],
        "prior_refund_total": case.order["prior_refund_total"],
        "order_currency": case.order["currency"],
        "handoff_count": 0,
        "audit_count": 0,
        "governance_count": 0,
        "policy_review_count": 0,
        "approval_count": 0,
        "refund_count": 0,
    }


def test_seed_verifier_is_read_only_and_checks_exact_root_scope() -> None:
    case = load_demo_catalog().get("demo04")
    row = _clean_seed_row(case)
    cursor = _Cursor(row=row)
    connection = _Connection(cursor)
    repository = _Repository()
    repository._connect = lambda: connection

    verify_seeded_case(repository, load_demo_catalog(), case)

    assert len(cursor.queries) == 2
    assert all(query.lstrip().upper().startswith("SELECT") for query, _params in cursor.queries)
    assert cursor.queries[1][1] == ("demo04", "order-demo04")
    assert cursor.closed and connection.closed


def test_seed_verifier_rejects_dirty_trace_before_graph_invocation() -> None:
    case = load_demo_catalog().get("demo04")
    row = _clean_seed_row(case)
    row.update({
        "status": "completed",
        "current_agent": "completed",
        "completed_at": "2026-08-14 12:00:00",
        "handoff_count": 4,
        "audit_count": 4,
        "governance_count": 2,
        "refund_count": 1,
    })
    cursor = _Cursor(row=row)
    connection = _Connection(cursor)
    repository = _Repository()
    repository._connect = lambda: connection

    with pytest.raises(DemoRunError, match="clean demo_cases.json baseline"):
        verify_seeded_case(repository, load_demo_catalog(), case)


def test_seed_verifier_rejects_mutated_order_fact_before_graph_invocation() -> None:
    case = load_demo_catalog().get("demo04")
    row = _clean_seed_row(case)
    row["amount_paid"] = 999.99
    cursor = _Cursor(row=row)
    connection = _Connection(cursor)
    repository = _Repository()
    repository._connect = lambda: connection

    with pytest.raises(DemoRunError, match="clean demo_cases.json baseline"):
        verify_seeded_case(repository, load_demo_catalog(), case)


class _PersistedCursor:
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.query = ""

    def execute(self, query: str, _params: Any = None) -> None:
        self.query = query

    def fetchone(self):
        if "FROM workflow_runs" in self.query:
            return {
                "ticket_id": f"ticket-{self.case_id}",
                "status": "completed",
                "current_agent": "completed",
            }
        if "audit_count" in self.query:
            return {"audit_count": 4, "governance_count": 2, "policy_review_count": 0}
        return None

    def fetchall(self):
        if "FROM agent_handoffs" in self.query:
            return [
                {"from_agent": "triage_agent", "to_agent": "policy_agent"},
                {"from_agent": "policy_agent", "to_agent": "refund_agent"},
                {"from_agent": "refund_agent", "to_agent": "response_agent"},
                {"from_agent": "response_agent", "to_agent": "end"},
            ]
        if "FROM refund_transactions" in self.query:
            return [{"status": "issued", "amount": 79.99, "currency": "USD", "approval_id": None}]
        if "FROM human_approvals" in self.query:
            return []
        if "FROM governance_events" in self.query:
            return [
                {"agent": "triage_agent", "interceptor_action": "allow"},
                {"agent": "response_agent", "interceptor_action": "allow"},
            ]
        return []

    def close(self) -> None:
        pass


def test_persisted_result_verifier_checks_trace_artifacts_not_only_graph_state() -> None:
    case = load_demo_catalog().get("demo01")
    cursor = _PersistedCursor(case.trace_id)
    connection = _Connection(cursor)
    repository = _Repository()
    repository._connect = lambda: connection

    evidence = verify_persisted_case(
        repository,
        load_demo_catalog(),
        case,
        simulate_case(case),
    )

    assert evidence["matched"] is True
    assert all(evidence["checks"].values())


def test_seed_verifier_rejects_any_extra_root_trace() -> None:
    cursor = _Cursor(extra_trace=True)
    connection = _Connection(cursor)
    repository = _Repository()
    repository._connect = lambda: connection

    with pytest.raises(DemoRunError, match="exactly demo01 through demo20"):
        verify_seeded_case(repository, load_demo_catalog(), load_demo_catalog().get("demo01"))
    assert len(cursor.queries) == 1
    assert cursor.closed and connection.closed
