"""Cloud-free contract tests for the strict HTTP acceptance harness."""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from demo.catalog import DEMO_IDS, DemoCase, load_demo_catalog
from demo.http_acceptance import (
    ApprovalPlan,
    HttpAcceptanceConfig,
    HttpAcceptanceError,
    run_http_acceptance,
)


class MockFinalSystem:
    """Stateful HTTP-only double; it never imports either ASGI application."""

    def __init__(
        self,
        *,
        refund_mode: str = "live",
        refund_database: str = "final",
        ready_for_full_run: bool = True,
        reverse_catalog: bool = False,
        false_match: str | None = None,
        followup_dashboard_status: str = "followup_approved",
    ) -> None:
        self.catalog = load_demo_catalog()
        self.refund_mode = refund_mode
        self.refund_database = refund_database
        self.ready_for_full_run = ready_for_full_run
        self.reverse_catalog = reverse_catalog
        self.false_match = false_match
        self.followup_dashboard_status = followup_dashboard_status
        self.executed: set[str] = set()
        self.followup_completed: set[str] = set()
        self.calls: list[tuple[str, str, str]] = []
        self.approval_payload: dict[str, Any] | None = None
        self.approval_resolved = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        self.calls.append((host, request.method, path))
        if host == "refund.test":
            return self._refund(request, path)
        if host == "dashboard.test":
            return self._dashboard(request, path)
        return httpx.Response(404, json={"detail": "unknown mock host"})

    def _refund(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "GET" and path == "/api/health":
            clean = self.ready_for_full_run and not self.executed
            case_states: dict[str, dict[str, str]] = {}
            for case in self.catalog.cases:
                if case.trace_id not in self.executed:
                    status = "running"
                    current_agent = "triage_agent"
                elif case.trace_id in self.followup_completed:
                    status = "completed"
                    current_agent = "completed"
                else:
                    status = case.expectations.terminal_state
                    current_agent = {
                        "completed": "completed",
                        "pending_human": "human_approval",
                        "waiting_user": "triage_agent",
                    }[status]
                case_states[case.trace_id] = {
                    "workflow_status": status,
                    "current_agent": current_agent,
                    "followup_status": (
                        "completed"
                        if case.trace_id in self.followup_completed
                        else "not_started"
                        if case.trace_id in {"demo10", "demo14"}
                        else "not_applicable"
                    ),
                    "followup_retry_after_seconds": None,
                }
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "mode": self.refund_mode,
                    "database": self.refund_database,
                    "database_status": "ok" if self.refund_mode == "live" else "not_checked",
                    "exact_demo_roots": True,
                    "canonical_root_data": True,
                    "canonical_root_fingerprint": "a" * 64,
                    "clean_case_count": 20 if clean else 0,
                    "dirty_case_count": 0 if clean else len(self.executed),
                    "clean_case_ids": (
                        list(DEMO_IDS) if clean else []
                    ),
                    "case_states": case_states,
                    "ready_for_full_run": clean,
                },
            )
        if request.method == "GET" and path == "/api/cases":
            cases = [case.public_summary() for case in self.catalog.cases]
            if self.reverse_catalog:
                cases.reverse()
            return httpx.Response(
                200,
                json={
                    "database": "final",
                    "evaluation_date": self.catalog.evaluation_date,
                    "cases": cases,
                },
            )
        if request.method == "POST" and path == "/api/refund":
            payload = json.loads(request.content)
            case = self.catalog.get(payload["case_id"])
            self.executed.add(case.trace_id)
            return httpx.Response(200, json=self._refund_result(case))
        if request.method == "POST" and path in {
            "/api/refund/demo10/follow-up",
            "/api/refund/demo14/follow-up",
        }:
            case = self.catalog.get(path.split("/")[3])
            if case.trace_id not in self.executed or case.follow_up is None:
                return httpx.Response(409, json={"detail": "case is not waiting"})
            payload = json.loads(request.content)
            if payload != case.follow_up.request_payload(case):
                return httpx.Response(422, json={"detail": "facts differ"})
            replay = case.trace_id in self.followup_completed
            self.followup_completed.add(case.trace_id)
            return httpx.Response(200, json=self._followup_result(case, replay=replay))
        return httpx.Response(404, json={"detail": "unknown refund route"})

    def _dashboard(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "GET" and path == "/api/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "api": {"status": "ok"},
                    "config": {"status": "ok", "database": "final"},
                    "database": {"status": "ok", "database": "final"},
                },
            )
        if request.method == "GET" and path.startswith("/api/cases/"):
            case_id = path.rsplit("/", 1)[-1]
            if case_id not in self.executed:
                return httpx.Response(404, json={"detail": "not executed"})
            return httpx.Response(200, json=self._dashboard_detail(self.catalog.get(case_id)))
        if request.method == "GET" and path == "/api/cases":
            return httpx.Response(
                200,
                json=[self._dashboard_summary(case) for case in self.catalog.cases],
            )
        if request.method == "GET" and path == "/api/console-metrics":
            return httpx.Response(200, json=self._metrics())
        if request.method == "GET" and path == "/api/audit-log/query":
            rows = [
                {"trace_id": case.trace_id, "event_type": f"event-{index}"}
                for case in self.catalog.cases
                for index in range(self._audit_count(case))
            ]
            rows.extend(
                {"trace_id": trace_id, "event_type": event_type}
                for trace_id in sorted(self.followup_completed)
                for event_type in (
                    "customer_followup_received",
                    "customer_followup_completed",
                )
            )
            return httpx.Response(200, json=rows)
        if request.method == "GET" and path == "/api/governance-events":
            rows = [
                {"trace_id": case.trace_id, "event_id": f"{case.trace_id}-gov-{index}"}
                for case in self.catalog.cases
                for index in range(2)
            ]
            rows.extend(
                {"trace_id": trace_id, "event_id": f"{trace_id}-followup-governance"}
                for trace_id in sorted(self.followup_completed)
            )
            return httpx.Response(200, json=rows)
        if request.method == "GET" and path == "/api/approvals/pending":
            return httpx.Response(200, json=self._pending())
        if request.method == "POST" and path == "/api/approvals/demo07/resolve":
            return self._resolve_approval(request)
        return httpx.Response(404, json={"detail": "unknown dashboard route"})

    def _refund_result(self, case: DemoCase) -> dict[str, Any]:
        expected = case.expectations
        refund = None
        if expected.outcome == "refund_issued":
            refund = {
                "status": "success",
                "refund_id": f"RF-{case.ticket_id}",
                "order_id": case.order_id,
                "amount": case.ticket["requested_amount"],
                "currency": case.ticket["currency"],
            }
        review = None
        if expected.terminal_state == "pending_human":
            review = {
                "approval_id": f"approval-{case.trace_id}",
                "status": "pending",
                "stage": "triage" if case.trace_id in {"demo12", "demo13"} else "policy",
            }
        return {
            "case_id": case.trace_id,
            "trace_id": case.trace_id,
            "ticket_id": case.ticket_id,
            "customer_id": case.customer_id,
            "order_id": case.order_id,
            "selected_order_id": case.selected_order_id,
            "order_resolution_source": (
                "trusted_ui_selection" if case.selected_order_id else "azure_tool_call"
            ),
            "message": case.message,
            "response_body": f"Customer-safe response for {case.trace_id}",
            "response_content_checks": {
                "decision_reflected": True,
                "missing_info_requested": True,
                "safe_summary_reflected": True,
                "outcome_anchor_reflected": True,
                "pii_fields_detected": [],
                "forbidden_phrases": [],
            },
            "route": expected.route,
            "policy_decision": (
                None if case.trace_id in {"demo12", "demo13"} else expected.policy_decision
            ),
            "final_outcome": expected.outcome,
            "workflow_status": expected.terminal_state,
            "success": True,
            "matched_expectations": case.trace_id != self.false_match,
            "mode": {"workflow": "live", "db": "real"},
            "execution_boundary": {
                "entrypoint": "refund_http_api",
                "database": "final",
                "azure": "real",
            },
            "selected_case": {
                "case_id": case.trace_id,
                "trace_id": case.trace_id,
                "ticket_id": case.ticket_id,
                "customer_id": case.customer_id,
                "order_id": case.order_id,
                "selected_order_id": case.selected_order_id,
                "message": case.message,
            },
            "governance": {
                "triage": "block" if case.trace_id in {"demo12", "demo13"} else "allow",
                "policy": None if case.trace_id in {"demo12", "demo13"} else "allow",
                "response": "allow",
            },
            "human_review": review,
            "refund_result": refund,
            "persistence": {
                "matched": True,
                "checks": {"workflow": True, "handoffs": True, "governance": True},
                "observed": {
                    "audit_count": self._audit_count(case),
                    "governance_count": 2,
                },
            },
        }

    def _followup_result(self, case: DemoCase, *, replay: bool) -> dict[str, Any]:
        follow_up = case.follow_up
        assert follow_up is not None
        response_checks = {
            "decision_reflected": True,
            "missing_info_requested": True,
            "safe_summary_reflected": True,
            "outcome_anchor_reflected": True,
            "pii_fields_detected": [],
            "forbidden_phrases": [],
        }
        return {
            "case_id": case.trace_id,
            "trace_id": case.trace_id,
            "ticket_id": case.ticket_id,
            "customer_id": case.customer_id,
            "order_id": case.order_id,
            "selected_order_id": case.order_id,
            "message": follow_up.message,
            "request_facts": follow_up.request_payload(case),
            "order_resolution_source": "trusted_ui_selection",
            "route": follow_up.expectations.route,
            "policy_decision": follow_up.expectations.policy_decision,
            "final_outcome": follow_up.expectations.outcome,
            "workflow_status": follow_up.expectations.terminal_state,
            "response_body": f"Customer-safe follow-up response for {case.trace_id}",
            "response_content_checks": response_checks,
            "governance": {"triage": "allow", "policy": "allow", "response": "allow"},
            "refund_result": {
                "status": "success",
                "refund_id": f"RF-{case.ticket_id}",
                "order_id": case.order_id,
                "amount": follow_up.requested_amount,
                "currency": follow_up.currency,
            },
            "success": True,
            "matched_expectations": True,
            "persistence": {
                "database": "final",
                "matched": True,
                "exact_root_counts": {
                    "workflow_runs": 20,
                    "customers": 20,
                    "orders": 20,
                    "tickets": 20,
                },
                "immutable_receipt_log_id": 900 + int(case.trace_id[-2:]),
                "history": {
                    "snapshot_sha256": "b" * 64,
                    "initial_handoff_count": 3,
                    "initial_audit_count": 3,
                    "initial_governance_count": 2,
                    "live_handoff_count": 8,
                    "live_audit_count": 9,
                    "live_governance_count": 4,
                    "continuation_handoff_count": 4,
                    "continuation_audit_count": 4,
                    "continuation_governance_count": 2,
                    "assistant_request_info_response": (
                        f"Canonical request-info response for {case.trace_id}"
                    ),
                    "queryable_in_receipt_audit": True,
                    "queryable_in_live_tables": True,
                    "initial_rows_preserved": True,
                },
                "required_routes": [
                    "customer->triage_agent",
                    "policy_agent->refund_agent",
                    "refund_agent->response_agent",
                    "response_agent->end",
                    "triage_agent->policy_agent",
                ],
                "refund": {
                    "status": "issued",
                    "amount": follow_up.requested_amount,
                    "currency": follow_up.currency,
                },
                "ticket_raw_text_preserved": True,
            },
            "follow_up": {
                "idempotent": replay,
                "status": "already_completed" if replay else "completed",
                "receipt_log_id": 900 + int(case.trace_id[-2:]),
            },
            "selected_case": {
                "case_id": case.trace_id,
                "trace_id": case.trace_id,
                "ticket_id": case.ticket_id,
                "customer_id": case.customer_id,
                "order_id": case.order_id,
                "message": follow_up.message,
            },
            "mode": {"workflow": "live", "db": "real"},
            "execution_boundary": {
                "entrypoint": "refund_followup_http_api",
                "database": "final",
                "azure": "real",
                "continuation": "customer_to_triage",
            },
        }

    def _dashboard_summary(self, case: DemoCase) -> dict[str, Any]:
        detail = self._dashboard_detail(case)
        return {
            key: detail[key]
            for key in (
                "id",
                "traceId",
                "workflowStatus",
                "currentAgent",
                "status",
                "finalOutcome",
            )
        }

    def _dashboard_detail(self, case: DemoCase) -> dict[str, Any]:
        expected = case.expectations
        status = {
            "refund_issued": "auto_approved",
            "denied": "rejected",
            "need_info": "needs_info",
            "manual_review": "manual_review",
        }[expected.outcome]
        if case.trace_id in {"demo12", "demo13"}:
            status = "quarantined"
        workflow_status = expected.terminal_state
        current_agent = {
            "completed": "completed",
            "pending_human": "human_approval",
            "waiting_user": "triage_agent",
        }[workflow_status]
        final_outcome = "approved" if expected.outcome == "refund_issued" else expected.outcome
        approvals: list[dict[str, Any]] = []
        pending_id = None
        if expected.terminal_state == "pending_human":
            pending_id = f"approval-{case.trace_id}"
            approvals = [
                {
                    "approval_id": pending_id,
                    "trace_id": case.trace_id,
                    "status": "pending",
                    "approved_next_agent": "refund_agent",
                    "amount_requested": case.ticket.get("requested_amount"),
                }
            ]
        refund = None
        if expected.outcome == "refund_issued":
            refund = {
                "transactionId": f"RF-{case.ticket_id}",
                "status": "issued",
                "amount": case.ticket["requested_amount"],
            }
        response_body = f"Customer-safe response for {case.trace_id}"
        if case.trace_id in self.followup_completed:
            assert case.follow_up is not None
            workflow_status = "completed"
            current_agent = "completed"
            status = self.followup_dashboard_status
            final_outcome = "approved"
            approvals = []
            pending_id = None
            response_body = f"Customer-safe follow-up response for {case.trace_id}"
            refund = {
                "transactionId": f"RF-{case.ticket_id}",
                "status": "issued",
                "amount": case.follow_up.requested_amount,
            }
        if case.trace_id == "demo07" and self.approval_resolved:
            decision = str(self.approval_payload["decision"])
            workflow_status = "completed"
            current_agent = "completed"
            pending_id = None
            status = "rejected" if decision == "deny" else "human_approved"
            final_outcome = "denied" if decision == "deny" else decision
            approvals = [
                {
                    "approval_id": "approval-demo07",
                    "trace_id": "demo07",
                    "status": "rejected" if decision == "deny" else "approved",
                    "approved_next_agent": "refund_agent",
                }
            ]
            if decision != "deny":
                refund = {
                    "transactionId": "RF-ticket-demo07",
                    "status": "issued",
                    "amount": self.approval_payload["resolved_amount"],
                }
        return {
            "id": case.ticket_id,
            "traceId": case.trace_id,
            "workflowStatus": workflow_status,
            "currentAgent": current_agent,
            "status": status,
            "finalOutcome": final_outcome,
            "customerResponse": {
                "body": response_body,
                "contentChecks": {
                    "decision_reflected": True,
                    "missing_info_requested": True,
                    "safe_summary_reflected": True,
                    "outcome_anchor_reflected": True,
                    "pii_fields_detected": [],
                    "forbidden_phrases": [],
                },
            },
            "order": {"orderId": case.order_id},
            "approvals": approvals,
            "pendingApprovalId": pending_id,
            "refund": refund,
        }

    def _pending(self) -> list[dict[str, Any]]:
        return [
            {
                "approval_id": f"approval-{case.trace_id}",
                "trace_id": case.trace_id,
                "status": "pending",
                "approved_next_agent": "refund_agent",
                "amount_requested": case.ticket.get("requested_amount"),
            }
            for case in self.catalog.cases
            if case.expectations.terminal_state == "pending_human"
            and not (case.trace_id == "demo07" and self.approval_resolved)
        ]

    def _metrics(self) -> dict[str, Any]:
        audit_total = (
            sum(self._audit_count(case) for case in self.catalog.cases)
            + 2 * len(self.followup_completed)
        )
        governance_total = 40 + len(self.followup_completed)
        status_counts = Counter(
            self._dashboard_summary(case)["status"] for case in self.catalog.cases
        )
        return {
            "primaryStats": [],
            "secondaryStats": [
                {"label": "Total Cases", "value": "20"},
                {"label": "Pending Approvals", "value": str(len(self._pending()))},
                {"label": "Persisted Governance Events", "value": str(governance_total)},
                {"label": "Audit Events", "value": str(audit_total)},
            ],
            "statusBreakdown": [
                {"status": status, "count": count}
                for status, count in status_counts.items()
            ],
        }

    def _resolve_approval(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("approval_id") != "approval-demo07":
            return httpx.Response(404, json={"detail": "approval not found"})
        if self.approval_payload is not None and payload != self.approval_payload:
            return httpx.Response(409, json={"detail": "conflicting approval"})
        idempotent = self.approval_payload is not None
        self.approval_payload = payload
        self.approval_resolved = True
        decision = payload["decision"]
        refund_result = (
            {}
            if decision == "deny"
            else {
                "status": "success",
                "refund_id": "RF-ticket-demo07",
                "amount": payload["resolved_amount"],
            }
        )
        return httpx.Response(
            200,
            json={
                "approval_id": "approval-demo07",
                "trace_id": "demo07",
                "decision": decision,
                "status": "rejected" if decision == "deny" else "approved",
                "resolved_amount": payload["resolved_amount"],
                "next_agent": "response_agent" if decision == "deny" else "refund_agent",
                "continuation_status": "already_completed" if idempotent else "completed",
                "workflow_status": "completed",
                "current_agent": "completed",
                "idempotent": idempotent,
                "new_approval_id": None,
                "refund_result": refund_result,
                "response_result": {
                    "final_outcome": "denied" if decision == "deny" else decision
                },
            },
        )

    @staticmethod
    def _audit_count(case: DemoCase) -> int:
        if case.expectations.route == "refund_agent":
            return 4
        if case.trace_id in {"demo12", "demo13"}:
            return 2
        return 3


def _clients(system: MockFinalSystem) -> tuple[httpx.Client, httpx.Client]:
    transport = httpx.MockTransport(system.handler)
    return (
        httpx.Client(base_url="http://refund.test", transport=transport),
        httpx.Client(base_url="http://dashboard.test", transport=transport),
    )


def _config(output: Path, *, approval: ApprovalPlan | None = None) -> HttpAcceptanceConfig:
    return HttpAcceptanceConfig(
        refund_base_url="http://refund.test",
        dashboard_base_url="http://dashboard.test",
        output_path=output,
        approval=approval,
    )


def test_offline_mock_transport_proves_all_twenty_http_contracts(tmp_path: Path) -> None:
    system = MockFinalSystem()
    refund, dashboard = _clients(system)
    output = tmp_path / "http-acceptance.json"

    report = run_http_acceptance(
        _config(output),
        refund_client=refund,
        dashboard_client=dashboard,
    )

    assert report["status"] == "passed"
    assert report["transport"] == "injected_test_transport"
    assert report["summary"] == {
        "requested": 20,
        "successful": 20,
        "matched_expectations": 20,
        "dashboard_observed": 20,
        "followups_exercised": 2,
        "followups_matched": 2,
        "approval_exercised": False,
    }
    assert [entry["case_id"] for entry in report["cases"]] == list(DEMO_IDS)
    assert system.executed == set(DEMO_IDS)
    initial_statuses = {
        entry["case_id"]: entry["dashboard"]["body"]["status"]
        for entry in report["cases"]
    }
    assert initial_statuses["demo01"] == "auto_approved"
    assert initial_statuses["demo10"] == "needs_info"
    assert initial_statuses["demo14"] == "needs_info"
    followup_statuses = {
        entry["case_id"]: entry["dashboard_after"]["body"]["status"]
        for entry in report["followups"]
    }
    assert followup_statuses == {
        "demo10": "followup_approved",
        "demo14": "followup_approved",
    }
    post_followup_statuses = {
        row["traceId"]: row["status"]
        for row in report["post_followup"]["cases"]["body"]
    }
    assert post_followup_statuses["demo10"] == "followup_approved"
    assert post_followup_statuses["demo14"] == "followup_approved"
    metric_labels = {
        row["label"]
        for row in report["post_followup"]["metrics"]["body"]["secondaryStats"]
    }
    assert "Persisted Governance Events" in metric_labels
    assert "Governance Checks" not in metric_labels
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["status"] == "passed"
    assert persisted["source"]["fixture_sha256"] == report["source"]["fixture_sha256"]
    assert Counter(path for _, method, path in system.calls if method == "POST") == {
        "/api/refund": 20,
        "/api/refund/demo10/follow-up": 2,
        "/api/refund/demo14/follow-up": 2,
    }


def test_offline_mock_transport_rejects_legacy_followup_status(tmp_path: Path) -> None:
    system = MockFinalSystem(followup_dashboard_status="auto_approved")
    refund, dashboard = _clients(system)

    with pytest.raises(
        HttpAcceptanceError,
        match="demo10 post-follow-up dashboard: status differs",
    ):
        run_http_acceptance(
            _config(tmp_path / "stale-followup-status.json"),
            refund_client=refund,
            dashboard_client=dashboard,
        )


def test_offline_mock_transport_rejects_offline_refund_health(tmp_path: Path) -> None:
    system = MockFinalSystem(refund_mode="offline")
    refund, dashboard = _clients(system)
    output = tmp_path / "failed.json"

    with pytest.raises(HttpAcceptanceError, match="not in live mode"):
        run_http_acceptance(
            _config(output),
            refund_client=refund,
            dashboard_client=dashboard,
        )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["preflight"]["refund_health"]["body"]["mode"] == "offline"
    assert not any(method == "POST" for _, method, _ in system.calls)


@pytest.mark.parametrize(
    ("system", "message"),
    [
        (MockFinalSystem(refund_database="main_db"), "Refund service is not using final"),
        (MockFinalSystem(ready_for_full_run=False), "does not have 20 clean cases"),
    ],
)
def test_offline_mock_transport_rejects_unsafe_or_dirty_live_health(
    tmp_path: Path,
    system: MockFinalSystem,
    message: str,
) -> None:
    refund, dashboard = _clients(system)

    with pytest.raises(HttpAcceptanceError, match=message):
        run_http_acceptance(
            _config(tmp_path / "unsafe.json"),
            refund_client=refund,
            dashboard_client=dashboard,
        )

    assert not any(method == "POST" for _, method, _ in system.calls)


def test_offline_mock_transport_requires_the_exact_ordered_catalog(tmp_path: Path) -> None:
    system = MockFinalSystem(reverse_catalog=True)
    refund, dashboard = _clients(system)

    with pytest.raises(HttpAcceptanceError, match="exact canonical demo01-demo20 fixture"):
        run_http_acceptance(
            _config(tmp_path / "wrong-catalog.json"),
            refund_client=refund,
            dashboard_client=dashboard,
        )

    assert not any(method == "POST" for _, method, _ in system.calls)


def test_offline_mock_transport_rejects_http_200_without_a_true_match(tmp_path: Path) -> None:
    system = MockFinalSystem(false_match="demo03")
    refund, dashboard = _clients(system)
    output = tmp_path / "failed-match.json"

    with pytest.raises(HttpAcceptanceError, match="demo03: matched_expectations is not true"):
        run_http_acceptance(
            _config(output),
            refund_client=refund,
            dashboard_client=dashboard,
        )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert [entry["case_id"] for entry in report["cases"]] == ["demo01", "demo02", "demo03"]
    assert report["cases"][-1]["refund"]["body"]["matched_expectations"] is False
    assert "dashboard" not in report["cases"][-1]


def test_offline_mock_transport_requires_queryable_followup_chronology(
    tmp_path: Path,
    monkeypatch,
) -> None:
    system = MockFinalSystem()
    original = system._followup_result

    def missing_history(case: DemoCase, *, replay: bool) -> dict[str, Any]:
        result = original(case, replay=replay)
        result["persistence"]["history"]["initial_rows_preserved"] = False
        return result

    monkeypatch.setattr(system, "_followup_result", missing_history)
    refund, dashboard = _clients(system)

    with pytest.raises(
        HttpAcceptanceError,
        match="demo10: follow-up chronology check initial_rows_preserved failed",
    ):
        run_http_acceptance(
            _config(tmp_path / "missing-history.json"),
            refund_client=refund,
            dashboard_client=dashboard,
        )


def test_offline_mock_transport_optionally_resolves_and_replays_demo07(tmp_path: Path) -> None:
    system = MockFinalSystem()
    refund, dashboard = _clients(system)
    output = tmp_path / "approval.json"
    approval = ApprovalPlan(
        decision="partial_refund",
        resolved_amount=Decimal("199.99"),
        reviewer="acceptance-reviewer",
        notes="Approved by the explicit offline contract test.",
    )

    report = run_http_acceptance(
        _config(output, approval=approval),
        refund_client=refund,
        dashboard_client=dashboard,
    )

    assert report["status"] == "passed"
    assert report["summary"]["approval_exercised"] is True
    assert report["approval"]["first"]["body"]["idempotent"] is False
    assert report["approval"]["replay"]["body"]["idempotent"] is True
    assert report["approval"]["dashboard_after"]["body"]["refund"]["amount"] == 199.99
    assert report["approval"]["first"]["request"]["json"]["reviewer"] == "[provided]"
    assert report["approval"]["first"]["request"]["json"]["notes"] == "[provided]"
    assert "demo07" not in {
        row["trace_id"] for row in report["approval"]["pending_after"]["body"]
    }
