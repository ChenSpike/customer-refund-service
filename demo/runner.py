"""Idempotent offline/live execution for the fixed final demo corpus."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .catalog import (
    DEFAULT_MANIFEST_PATH,
    DEMO_IDS,
    FINAL_DATABASE,
    DemoCase,
    DemoCatalog,
    load_demo_catalog,
)


RunMode = Literal["offline", "live"]
OfflineExecutor = Callable[[DemoCase], dict[str, Any]]
SeedVerifier = Callable[[Any, DemoCatalog, DemoCase], None]
ResultVerifier = Callable[[Any, DemoCatalog, DemoCase, dict[str, Any]], dict[str, Any]]
_ENVIRONMENT_LOCK = threading.RLock()
_TRIAGE_GOVERNANCE_CASES = frozenset({"demo12", "demo13"})


class DemoRunError(RuntimeError):
    """Raised when a live run would escape the seeded final-demo boundary."""


class DemoRunner:
    def __init__(
        self,
        mode: RunMode = "offline",
        *,
        manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
        repository: Any | None = None,
        graph: Any | None = None,
        offline_executor: OfflineExecutor | None = None,
        seed_verifier: SeedVerifier | None = None,
        result_verifier: ResultVerifier | None = None,
        clock: Callable[[], float] = time.perf_counter,
        workers: int = 1,
    ) -> None:
        if mode not in {"offline", "live"}:
            raise DemoRunError("mode must be 'offline' or 'live'")
        if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 4:
            raise DemoRunError("workers must be an integer from 1 through 4")
        self.mode = mode
        self.catalog = load_demo_catalog(manifest_path)
        self.repository = repository
        self.graph = graph
        self.offline_executor = offline_executor or _default_offline_executor
        self.seed_verifier = seed_verifier or verify_seeded_case
        self.result_verifier = result_verifier or verify_persisted_case
        self.clock = clock
        self.workers = workers

    def run_case(self, case_id: str) -> dict[str, Any]:
        with _scoped_environment("POLICY_EVALUATION_DATE", self.catalog.evaluation_date):
            return self._run_case(case_id)

    def _run_case(self, case_id: str) -> dict[str, Any]:
        started = self.clock()
        case: DemoCase | None = None
        repository: Any | None = None
        graph_started = False
        timings: dict[str, float] = {}
        try:
            case = self.catalog.get(case_id)
            if self.mode == "offline":
                workflow_started = self.clock()
                try:
                    actual = self.offline_executor(case)
                finally:
                    timings["workflow"] = _milliseconds(self.clock() - workflow_started)
            else:
                repository = self._live_repository()
                verify_started = self.clock()
                try:
                    self.seed_verifier(repository, self.catalog, case)
                finally:
                    timings["seed_verification"] = _milliseconds(self.clock() - verify_started)
                workflow_started = self.clock()
                try:
                    graph_started = True
                    state = self._live_graph(repository).invoke(case.graph_input())
                finally:
                    timings["workflow"] = _milliseconds(self.clock() - workflow_started)
                actual = normalize_graph_state(case, state)
                actual["persistence"] = self.result_verifier(
                    repository,
                    self.catalog,
                    case,
                    actual,
                )
            timings["total"] = _milliseconds(self.clock() - started)
            matched = _matches_expectations(case, actual)
            return {
                "case_id": case.trace_id,
                "mode": self.mode,
                "success": True,
                "matched_expectations": matched,
                "expected": case.expectations.as_dict(),
                **actual,
                "timings_ms": timings,
            }
        except Exception as error:
            timings["total"] = _milliseconds(self.clock() - started)
            failure_persistence_error = None
            if self.mode == "live" and case is not None and graph_started and repository is not None:
                try:
                    repository.record_workflow_failure(
                        trace_id=case.trace_id,
                        ticket_id=case.ticket_id,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                except Exception as persistence_error:  # keep the original workflow error primary
                    failure_persistence_error = {
                        "type": type(persistence_error).__name__,
                        "message": str(persistence_error),
                    }
            result = {
                "case_id": case.trace_id if case else str(case_id),
                "mode": self.mode,
                "success": False,
                "matched_expectations": False,
                "expected": case.expectations.as_dict() if case else None,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
                "timings_ms": timings,
            }
            if failure_persistence_error is not None:
                result["failure_persistence_error"] = failure_persistence_error
            return result

    def run_batch(self, case_ids: Iterable[str] | None = None) -> dict[str, Any]:
        requested = list(case_ids) if case_ids is not None else list(DEMO_IDS)
        started = self.clock()
        with _scoped_environment("POLICY_EVALUATION_DATE", self.catalog.evaluation_date):
            # Build shared read-only configuration and the stateless compiled
            # graph before worker threads start. Each stage opens its own DB
            # transaction and every persisted artifact is trace-scoped.
            if self.mode == "live":
                self._live_graph(self._live_repository())
            if self.workers == 1 or len(requested) <= 1:
                results = [self._run_case(case_id) for case_id in requested]
            else:
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    results = list(pool.map(self._run_case, requested))
        successful = sum(bool(result["success"]) for result in results)
        matched = sum(bool(result["matched_expectations"]) for result in results)
        return {
            "mode": self.mode,
            "workers": self.workers,
            "requested": len(requested),
            "successful": successful,
            "failed": len(requested) - successful,
            "matched_expectations": matched,
            "elapsed_ms": _milliseconds(self.clock() - started),
            "cases": results,
        }

    def _live_repository(self) -> Any:
        if self.repository is None:
            from db.database import GCPRepository

            self.repository = GCPRepository.from_env()
        database_name = str(
            getattr(self.repository, "database_name", "")
            or getattr(self.repository, "connection_config", {}).get("database", "")
        )
        if database_name != FINAL_DATABASE:
            raise DemoRunError(
                f"Live demos require MYSQL_DATABASE=final; connected target is {database_name!r}"
            )
        return self.repository

    def _live_graph(self, repository: Any) -> Any:
        if self.graph is None:
            from app.graph import build_graph

            self.graph = build_graph(repository=repository)
        return self.graph


def verify_seeded_case(repository: Any, catalog: DemoCatalog, case: DemoCase) -> None:
    """Read-only proof that the live database is exactly the seeded 20-case root set."""

    connection = repository._connect()
    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT trace_id FROM workflow_runs ORDER BY trace_id")
        actual_traces = tuple(_row_value(row, "trace_id", 0) for row in cursor.fetchall())
        expected_traces = tuple(item.trace_id for item in catalog.cases)
        if actual_traces != expected_traces:
            raise DemoRunError(
                "final.workflow_runs must contain exactly demo01 through demo20 before execution"
            )
        cursor.execute(
            """
            SELECT
              workflow.trace_id, workflow.ticket_id,
              ticket.customer_id, ticket.raw_text,
              customer.email, orders.order_id,
              workflow.status, workflow.current_agent, workflow.completed_at,
              (SELECT COUNT(*) FROM agent_handoffs h WHERE h.trace_id = workflow.trace_id) AS handoff_count,
              (SELECT COUNT(*) FROM audit_log a WHERE a.trace_id = workflow.trace_id) AS audit_count,
              (SELECT COUNT(*) FROM governance_events g WHERE g.trace_id = workflow.trace_id) AS governance_count,
              (SELECT COUNT(*) FROM policy_review_events p WHERE p.trace_id = workflow.trace_id) AS policy_review_count,
              (SELECT COUNT(*) FROM human_approvals ha WHERE ha.trace_id = workflow.trace_id) AS approval_count,
              (SELECT COUNT(*) FROM refund_transactions r WHERE r.trace_id = workflow.trace_id) AS refund_count
            FROM workflow_runs workflow
            JOIN tickets ticket ON ticket.ticket_id = workflow.ticket_id
            JOIN customers customer ON customer.customer_id = ticket.customer_id
            JOIN orders orders ON orders.customer_id = customer.customer_id
            WHERE workflow.trace_id = %s AND orders.order_id = %s
            """,
            (case.trace_id, case.order_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise DemoRunError(f"{case.trace_id}: seeded customer/order/ticket/workflow row is missing")
        expected = {
            "trace_id": case.trace_id,
            "ticket_id": case.ticket_id,
            "customer_id": case.customer_id,
            "raw_text": case.message,
            "email": case.customer["email"],
            "order_id": case.order_id,
            "status": "running",
            "current_agent": "triage_agent",
            "completed_at": None,
            "handoff_count": 0,
            "audit_count": 0,
            "governance_count": 0,
            "policy_review_count": 0,
            "approval_count": 0,
            "refund_count": 0,
        }
        actual = {
            key: _row_value(row, key, index)
            for index, key in enumerate(expected)
        }
        if actual != expected:
            raise DemoRunError(
                f"{case.trace_id}: seeded root data is not the clean demo_cases.json baseline"
            )
    finally:
        if cursor is not None and hasattr(cursor, "close"):
            cursor.close()
        connection.close()


def normalize_graph_state(case: DemoCase, state: dict[str, Any]) -> dict[str, Any]:
    response_result = state.get("response_result") or {}
    refund_result = state.get("refund_result") or {}
    triage_governance = state.get("triage_governance_result") or {}
    policy_governance = state.get("policy_governance_result") or {}
    response_governance = state.get("response_governance_result") or {}
    policy_decision = state.get("policy_decision") or {}
    order_lookup = state.get("order_lookup_result") or {}

    route = (
        (state.get("policy_persistence_result") or {}).get("next_agent")
        or (state.get("triage_persistence_result") or {}).get("next_agent")
        or ""
    )
    final_outcome = str(state.get("final_outcome") or "")
    if refund_result.get("status") == "success":
        final_outcome = "refund_issued"
    workflow_status = str(state.get("workflow_status") or "")
    if workflow_status == "waiting_human":
        workflow_status = "pending_human"

    findings = [
        *(triage_governance.get("findings") or []),
        *(policy_governance.get("findings") or []),
    ]
    detail = next(
        (finding.get("detail") for finding in findings if isinstance(finding, dict) and finding.get("detail")),
        None,
    )
    return {
        "trace_id": state.get("trace_id"),
        "ticket_id": state.get("ticket_id"),
        "customer_id": state.get("user_id"),
        "order_id": order_lookup.get("order_id"),
        "selected_order_id": case.selected_order_id,
        "message": case.message,
        "route": route,
        "policy_decision": policy_decision.get("decision"),
        "final_outcome": final_outcome,
        "workflow_status": workflow_status,
        "response_body": (
            response_result.get("response", {}).get("body")
            or response_result.get("body")
            or "(no response generated)"
        ),
        "governance": {
            "triage": triage_governance.get("status"),
            "policy": policy_governance.get("status"),
            "response": response_governance.get("status"),
            "detail": detail,
        },
        "human_review": state.get("human_review"),
        "refund_result": refund_result or None,
    }


def _default_offline_executor(case: DemoCase) -> dict[str, Any]:
    from refund_app.simulator import simulate_case

    return simulate_case(case)


def _matches_expectations(case: DemoCase, actual: dict[str, Any]) -> bool:
    triage_blocked = (actual.get("governance") or {}).get("triage") == "block"
    expected_triage_status = "block" if case.trace_id in _TRIAGE_GOVERNANCE_CASES else "allow"
    expected_policy_status = None if expected_triage_status == "block" else "allow"
    governance_matches = (
        (actual.get("governance") or {}).get("triage") == expected_triage_status
        and (actual.get("governance") or {}).get("policy") == expected_policy_status
        and (actual.get("governance") or {}).get("response") == "allow"
    )
    policy_matches = (
        triage_blocked
        or actual.get("policy_decision") == case.expectations.policy_decision
    )
    identities_match = (
        actual.get("trace_id") == case.trace_id
        and actual.get("ticket_id") == case.ticket_id
        and actual.get("customer_id") == case.customer_id
        and actual.get("order_id") == case.order_id
    )
    refund_matches = True
    if case.expectations.outcome == "refund_issued":
        refund = actual.get("refund_result") or {}
        expected_amount = case.ticket.get("requested_amount")
        refund_matches = (
            refund.get("status") == "success"
            and refund.get("order_id") == case.order_id
            and expected_amount is not None
            and abs(float(refund.get("amount") or 0) - float(expected_amount)) < 0.005
        )
    persistence = actual.get("persistence")
    persistence_matches = persistence is None or persistence.get("matched") is True
    return (
        identities_match
        and policy_matches
        and governance_matches
        and actual.get("route") == case.expectations.route
        and actual.get("final_outcome") == case.expectations.outcome
        and actual.get("workflow_status") == case.expectations.terminal_state
        and refund_matches
        and persistence_matches
    )


def verify_persisted_case(
    repository: Any,
    _catalog: DemoCatalog,
    case: DemoCase,
    actual: dict[str, Any],
) -> dict[str, Any]:
    """Read-only, trace-scoped proof that the graph result reached GCP intact."""

    connection = repository._connect()
    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT ticket_id, status, current_agent FROM workflow_runs WHERE trace_id = %s",
            (case.trace_id,),
        )
        workflow = cursor.fetchone()

        cursor.execute(
            "SELECT from_agent, to_agent FROM agent_handoffs WHERE trace_id = %s",
            (case.trace_id,),
        )
        handoffs = cursor.fetchall()

        cursor.execute(
            "SELECT status, amount, currency, approval_id FROM refund_transactions WHERE trace_id = %s",
            (case.trace_id,),
        )
        refunds = cursor.fetchall()

        cursor.execute(
            "SELECT status, triggering_event_type, approved_next_agent FROM human_approvals WHERE trace_id = %s",
            (case.trace_id,),
        )
        approvals = cursor.fetchall()

        cursor.execute(
            "SELECT agent, interceptor_action FROM governance_events WHERE trace_id = %s",
            (case.trace_id,),
        )
        governance_events = cursor.fetchall()

        cursor.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM audit_log WHERE trace_id = %s) AS audit_count,
              (SELECT COUNT(*) FROM governance_events WHERE trace_id = %s) AS governance_count,
              (SELECT COUNT(*) FROM policy_review_events WHERE trace_id = %s) AS policy_review_count
            """,
            (case.trace_id, case.trace_id, case.trace_id),
        )
        counts = cursor.fetchone() or {}
    finally:
        if cursor is not None and hasattr(cursor, "close"):
            cursor.close()
        connection.close()

    expected_agents = {"triage_agent", "response_agent"}
    triage_blocked = case.trace_id in _TRIAGE_GOVERNANCE_CASES
    if not triage_blocked:
        expected_agents.add("policy_agent")
    if case.expectations.route == "refund_agent":
        expected_agents.add("refund_agent")

    from_agents = {str(_row_value(row, "from_agent", 0)) for row in handoffs}
    handoff_routes = {
        (str(_row_value(row, "from_agent", 0)), str(_row_value(row, "to_agent", 1)))
        for row in handoffs
    }
    expected_route_source = "triage_agent" if triage_blocked else "policy_agent"

    expected_refund_count = 1 if case.expectations.outcome == "refund_issued" else 0
    refund_ok = len(refunds) == expected_refund_count
    if expected_refund_count:
        refund = refunds[0]
        expected_amount = float(case.ticket["requested_amount"])
        refund_ok = refund_ok and (
            _row_value(refund, "status", 0) == "issued"
            and abs(float(_row_value(refund, "amount", 1)) - expected_amount) < 0.005
            and _row_value(refund, "currency", 2) == case.ticket.get("currency", "USD")
            and _row_value(refund, "approval_id", 3) is None
        )

    expected_approval_count = 1 if case.expectations.terminal_state == "pending_human" else 0
    approval_ok = len(approvals) == expected_approval_count
    if expected_approval_count:
        approval = approvals[0]
        expected_trigger = "governance" if triage_blocked else "policy_review"
        expected_next = "policy_agent" if triage_blocked else "refund_agent"
        approval_ok = approval_ok and (
            _row_value(approval, "status", 0) == "pending"
            and _row_value(approval, "triggering_event_type", 1) == expected_trigger
            and _row_value(approval, "approved_next_agent", 2) == expected_next
        )

    expected_policy_reviews = (
        1
        if not triage_blocked and case.expectations.policy_decision == "manual_review"
        else 0
    )
    expected_handoffs = len(expected_agents)
    expected_governance_actions = {
        ("triage_agent", "block" if triage_blocked else "allow"),
        ("response_agent", "allow"),
    }
    observed_governance_actions = {
        (
            str(_row_value(row, "agent", 0)),
            str(_row_value(row, "interceptor_action", 1)),
        )
        for row in governance_events
    }
    checks = {
        "workflow": bool(workflow)
        and _row_value(workflow, "ticket_id", 0) == case.ticket_id
        and _row_value(workflow, "status", 1) == case.expectations.terminal_state,
        "handoff_agents": from_agents == expected_agents,
        "route_handoff": (expected_route_source, case.expectations.route) in handoff_routes,
        "refund": refund_ok,
        "approval": approval_ok,
        "audit_count": int(_row_value(counts, "audit_count", 0) or 0) == expected_handoffs,
        "governance_count": int(_row_value(counts, "governance_count", 1) or 0) == 2,
        "governance_actions": observed_governance_actions == expected_governance_actions,
        "policy_review_count": int(_row_value(counts, "policy_review_count", 2) or 0)
        == expected_policy_reviews,
        "normalized_state": actual.get("workflow_status") == case.expectations.terminal_state,
    }
    return {
        "matched": all(checks.values()),
        "checks": checks,
        "observed": {
            "workflow_status": _row_value(workflow, "status", 1) if workflow else None,
            "current_agent": _row_value(workflow, "current_agent", 2) if workflow else None,
            "handoff_agents": sorted(from_agents),
            "refund_count": len(refunds),
            "approval_count": len(approvals),
            "audit_count": int(_row_value(counts, "audit_count", 0) or 0),
            "governance_count": int(_row_value(counts, "governance_count", 1) or 0),
            "governance_actions": sorted(observed_governance_actions),
            "policy_review_count": int(_row_value(counts, "policy_review_count", 2) or 0),
        },
    }


def _row_value(row: Any, name: str, index: int) -> Any:
    return row.get(name) if isinstance(row, dict) else row[index]


def _milliseconds(seconds: float) -> float:
    return round(max(0.0, seconds) * 1000, 3)


@contextmanager
def _scoped_environment(name: str, value: str):
    # Environment variables are process-global.  Serialize the scoped policy
    # date so concurrent API requests cannot restore one another's temporary
    # value out of order.
    with _ENVIRONMENT_LOCK:
        previous = os.environ.get(name)
        os.environ[name] = value
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
