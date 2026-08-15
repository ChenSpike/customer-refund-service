"""Strict TCP/HTTP acceptance for the final 20-case demonstration.

The harness deliberately does not import either FastAPI application.  Normal
execution creates ordinary ``httpx.Client`` instances and therefore requires
already-running refund and dashboard services reachable over TCP.  Tests may
inject clients backed by ``httpx.MockTransport`` without weakening the live
CLI contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from demo.catalog import (
    DEFAULT_MANIFEST_PATH,
    DEMO_IDS,
    FINAL_DATABASE,
    DemoCase,
    DemoCatalog,
    load_demo_catalog,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_PATH = REPO_ROOT / "reports" / "final-http-e2e.json"
DEFAULT_REFUND_URL = "http://127.0.0.1:8077"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8000"
SCHEMA_VERSION = "2.0"


class HttpAcceptanceError(RuntimeError):
    """A public HTTP acceptance invariant failed."""


@dataclass(frozen=True)
class ApprovalPlan:
    """Optional demo07 continuation performed after the initial 20-case proof."""

    decision: Literal["approve", "partial_refund", "deny"]
    resolved_amount: Decimal | None
    reviewer: str
    notes: str
    trace_id: str = "demo07"

    def validate(self, catalog: DemoCatalog) -> None:
        if self.trace_id != "demo07":
            raise HttpAcceptanceError("The optional approval phase is restricted to demo07")
        case = catalog.get(self.trace_id)
        if not self.reviewer.strip():
            raise HttpAcceptanceError("Approval reviewer must be non-empty")
        if not self.notes.strip():
            raise HttpAcceptanceError("Approval notes must be non-empty")
        if self.decision == "deny":
            if self.resolved_amount is not None:
                raise HttpAcceptanceError("A denied approval must omit resolved_amount")
            return
        if self.resolved_amount is None or self.resolved_amount <= 0:
            raise HttpAcceptanceError("Refund approval requires a positive resolved_amount")
        if self.resolved_amount.as_tuple().exponent < -2:
            raise HttpAcceptanceError("resolved_amount may have at most two decimal places")
        requested = Decimal(str(case.ticket["requested_amount"]))
        remaining = max(
            Decimal(str(case.order["amount_paid"]))
            - Decimal(str(case.order["prior_refund_total"])),
            Decimal("0"),
        )
        if self.resolved_amount > requested or self.resolved_amount > remaining:
            raise HttpAcceptanceError(
                "resolved_amount cannot exceed demo07 requested or remaining refundable amount"
            )
        if self.decision == "partial_refund" and self.resolved_amount >= requested:
            raise HttpAcceptanceError("partial_refund amount must be below the requested amount")

    def request_payload(self, approval_id: str) -> dict[str, Any]:
        return {
            "approval_id": approval_id,
            "decision": self.decision,
            "resolved_amount": (
                float(self.resolved_amount) if self.resolved_amount is not None else None
            ),
            "reviewer": self.reviewer.strip(),
            "notes": self.notes.strip(),
        }


@dataclass(frozen=True)
class HttpAcceptanceConfig:
    refund_base_url: str = DEFAULT_REFUND_URL
    dashboard_base_url: str = DEFAULT_DASHBOARD_URL
    output_path: Path = DEFAULT_REPORT_PATH
    manifest_path: Path = DEFAULT_MANIFEST_PATH
    timeout_seconds: float = 180.0
    approval: ApprovalPlan | None = None


class HttpAcceptanceHarness:
    """Exercise both public APIs and retain their complete JSON evidence."""

    def __init__(
        self,
        config: HttpAcceptanceConfig,
        *,
        refund_client: httpx.Client | None = None,
        dashboard_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._canonical_root_fingerprint: str | None = None
        self.catalog = load_demo_catalog(config.manifest_path)
        if tuple(case.trace_id for case in self.catalog.cases) != DEMO_IDS:
            raise HttpAcceptanceError("Local fixture is not the exact ordered demo01-demo20 catalog")
        if self.catalog.database != FINAL_DATABASE:
            raise HttpAcceptanceError("Local fixture is not bound to database final")
        if config.approval is not None:
            config.approval.validate(self.catalog)

        self.refund_base_url = _normalize_base_url(config.refund_base_url, "refund")
        self.dashboard_base_url = _normalize_base_url(config.dashboard_base_url, "dashboard")
        timeout = httpx.Timeout(config.timeout_seconds, connect=min(config.timeout_seconds, 10.0))
        self._owns_refund_client = refund_client is None
        self._owns_dashboard_client = dashboard_client is None
        self.refund_client = refund_client or httpx.Client(
            base_url=self.refund_base_url,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "customer-refund-final-http-acceptance/1.0"},
        )
        self.dashboard_client = dashboard_client or httpx.Client(
            base_url=self.dashboard_base_url,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "customer-refund-final-http-acceptance/1.0"},
        )

    def run(self) -> dict[str, Any]:
        started_wall = _utc_now()
        started = time.perf_counter()
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "acceptance": "refund_http_to_dashboard_http_with_customer_continuations",
            "status": "running",
            "started_at": started_wall,
            "transport": (
                "tcp"
                if self._owns_refund_client and self._owns_dashboard_client
                else "injected_test_transport"
            ),
            "services": {
                "refund": self.refund_base_url,
                "dashboard": self.dashboard_base_url,
            },
            "source": _source_metadata(self.config.manifest_path),
            "catalog": {
                "database": self.catalog.database,
                "evaluation_date": self.catalog.evaluation_date,
                "case_ids": list(DEMO_IDS),
            },
            "preflight": {},
            "cases": [],
            "aggregates": {},
            "followups": [],
            "post_followup": {},
            "approval": None,
        }
        try:
            self._run_preflight(report)
            for case in self.catalog.cases:
                case_evidence: dict[str, Any] = {"case_id": case.trace_id}
                report["cases"].append(case_evidence)
                self._run_case(case, case_evidence)
            report["aggregates"] = self._validate_aggregates(report["cases"])
            for case_id in ("demo10", "demo14"):
                case = self.catalog.get(case_id)
                followup_evidence: dict[str, Any] = {"case_id": case_id}
                report["followups"].append(followup_evidence)
                self._run_followup(case, followup_evidence)
            report["post_followup"] = self._validate_post_followups(
                report["aggregates"],
                report["followups"],
            )
            if self.config.approval is not None:
                report["approval"] = self._run_approval(
                    self.config.approval,
                    report["aggregates"]["pending_approvals"]["body"],
                )
            report["status"] = "passed"
            report["summary"] = {
                "requested": len(DEMO_IDS),
                "successful": len(report["cases"]),
                "matched_expectations": len(report["cases"]),
                "dashboard_observed": len(report["cases"]),
                "followups_exercised": len(report["followups"]),
                "followups_matched": len(report["followups"]),
                "approval_exercised": self.config.approval is not None,
            }
            return report
        except Exception as error:
            report["status"] = "failed"
            report["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            if isinstance(error, HttpAcceptanceError):
                raise
            raise HttpAcceptanceError(f"HTTP acceptance failed: {error}") from error
        finally:
            report["finished_at"] = _utc_now()
            report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            _write_report(self.config.output_path, report)
            if self._owns_refund_client:
                self.refund_client.close()
            if self._owns_dashboard_client:
                self.dashboard_client.close()

    def _run_preflight(self, report: dict[str, Any]) -> None:
        refund_health = _exchange(self.refund_client, "GET", "/api/health")
        report["preflight"]["refund_health"] = refund_health
        body = _mapping(refund_health["body"], "refund health")
        _require(body.get("status") == "ok", "Refund health status is not ok")
        _require(body.get("mode") == "live", "Refund service is not in live mode")
        _require(body.get("database") == FINAL_DATABASE, "Refund service is not using final")
        _require(
            body.get("database_status") == "ok",
            "Refund service did not verify the selected final database",
        )
        _require(body.get("exact_demo_roots") is True, "Refund service did not verify exact demo roots")
        _require(
            body.get("canonical_root_data") is True,
            "Refund service did not verify canonical customer/order/ticket facts",
        )
        _require(
            isinstance(body.get("canonical_root_fingerprint"), str)
            and len(body["canonical_root_fingerprint"]) == 64,
            "Refund service did not expose a canonical root fingerprint",
        )
        self._canonical_root_fingerprint = str(body["canonical_root_fingerprint"])
        _require(body.get("clean_case_count") == 20, "Refund service does not have 20 clean cases")
        _require(body.get("dirty_case_count") == 0, "Refund service requires a clean reset")
        _require(
            body.get("clean_case_ids") == list(DEMO_IDS),
            "Refund service clean-case allowlist is not exact or ordered",
        )
        case_states = _mapping(body.get("case_states"), "refund health case states")
        _require(
            set(case_states) == set(DEMO_IDS)
            and all(
                _mapping(case_states.get(case_id), f"{case_id} health state").get(
                    "workflow_status"
                )
                == "running"
                and _mapping(
                    case_states.get(case_id), f"{case_id} health state"
                ).get("current_agent")
                == "triage_agent"
                for case_id in DEMO_IDS
            ),
            "Refund service did not prove the exact clean workflow state for all 20 cases",
        )
        _require(
            body.get("ready_for_full_run") is True,
            "Refund service is not ready for a full 20-case run",
        )

        dashboard_health = _exchange(self.dashboard_client, "GET", "/api/health")
        report["preflight"]["dashboard_health"] = dashboard_health
        body = _mapping(dashboard_health["body"], "dashboard health")
        database = _mapping(body.get("database"), "dashboard health database")
        config = _mapping(body.get("config"), "dashboard health config")
        _require(body.get("status") == "ok", "Dashboard health status is not ok")
        _require(config.get("status") == "ok", "Dashboard database configuration is not ok")
        _require(config.get("database") == FINAL_DATABASE, "Dashboard is not configured for final")
        _require(database.get("status") == "ok", "Dashboard database is not reachable")
        _require(database.get("database") == FINAL_DATABASE, "Dashboard selected database is not final")

        catalog_exchange = _exchange(self.refund_client, "GET", "/api/cases")
        report["preflight"]["refund_catalog"] = catalog_exchange
        catalog_body = _mapping(catalog_exchange["body"], "refund case catalog")
        expected_cases = [case.public_summary() for case in self.catalog.cases]
        _require(catalog_body.get("database") == FINAL_DATABASE, "Remote catalog is not for final")
        _require(
            catalog_body.get("evaluation_date") == self.catalog.evaluation_date,
            "Remote catalog evaluation date differs from the fixture",
        )
        _require(
            catalog_body.get("cases") == expected_cases,
            "Remote catalog is not the exact canonical demo01-demo20 fixture",
        )
    def _run_case(self, case: DemoCase, evidence: dict[str, Any]) -> None:
        refund_exchange = _exchange(
            self.refund_client,
            "POST",
            "/api/refund",
            json_body={"case_id": case.trace_id},
        )
        evidence["refund"] = refund_exchange
        refund = _mapping(refund_exchange["body"], f"{case.trace_id} refund response")
        self._validate_refund_result(case, refund)

        dashboard_exchange = _exchange(
            self.dashboard_client,
            "GET",
            f"/api/cases/{case.trace_id}",
        )
        evidence["dashboard"] = dashboard_exchange
        dashboard = _mapping(
            dashboard_exchange["body"],
            f"{case.trace_id} dashboard case detail",
        )
        self._validate_dashboard_detail(case, dashboard)
        customer_response = _mapping(
            dashboard.get("customerResponse"),
            f"{case.trace_id} dashboard customer response",
        )
        _require(
            customer_response.get("body") == refund.get("response_body"),
            f"{case.trace_id}: dashboard response differs from the refund HTTP result",
        )
        _require(
            customer_response.get("contentChecks") == refund.get("response_content_checks"),
            f"{case.trace_id}: dashboard semantic checks differ from the refund HTTP result",
        )

    def _run_followup(self, case: DemoCase, evidence: dict[str, Any]) -> None:
        """Resume and idempotently replay one canonical waiting-customer case."""

        follow_up = case.follow_up
        _require(follow_up is not None, f"{case.trace_id}: no follow-up fixture exists")
        payload = follow_up.request_payload(case)
        path = f"/api/refund/{case.trace_id}/follow-up"

        first = _exchange(self.refund_client, "POST", path, json_body=payload)
        evidence["first"] = first
        first_body = _mapping(first["body"], f"{case.trace_id} follow-up response")
        self._validate_followup_result(case, first_body, replay=False)

        dashboard = _exchange(
            self.dashboard_client,
            "GET",
            f"/api/cases/{case.trace_id}",
        )
        evidence["dashboard_after"] = dashboard
        dashboard_body = _mapping(
            dashboard["body"],
            f"{case.trace_id} post-follow-up dashboard case detail",
        )
        self._validate_followup_dashboard(case, first_body, dashboard_body)

        replay = _exchange(self.refund_client, "POST", path, json_body=payload)
        evidence["replay"] = replay
        replay_body = _mapping(replay["body"], f"{case.trace_id} follow-up replay")
        self._validate_followup_result(case, replay_body, replay=True)
        for field in (
            "trace_id",
            "ticket_id",
            "customer_id",
            "order_id",
            "message",
            "route",
            "policy_decision",
            "final_outcome",
            "workflow_status",
            "response_body",
            "response_content_checks",
            "refund_result",
            "persistence",
        ):
            _require(
                replay_body.get(field) == first_body.get(field),
                f"{case.trace_id}: idempotent follow-up replay changed {field}",
            )

    @staticmethod
    def _validate_followup_result(
        case: DemoCase,
        result: Mapping[str, Any],
        *,
        replay: bool,
    ) -> None:
        follow_up = case.follow_up
        _require(follow_up is not None, f"{case.trace_id}: no follow-up fixture exists")
        expected = follow_up.expectations
        _require(
            result.get("mode") == {"workflow": "live", "db": "real"},
            f"{case.trace_id}: follow-up was not live/real",
        )
        _require(
            result.get("execution_boundary")
            == {
                "entrypoint": "refund_followup_http_api",
                "database": FINAL_DATABASE,
                "azure": "real",
                "continuation": "customer_to_triage",
            },
            f"{case.trace_id}: follow-up HTTP boundary was not attested",
        )
        exact = {
            "case_id": case.trace_id,
            "trace_id": case.trace_id,
            "ticket_id": case.ticket_id,
            "customer_id": case.customer_id,
            "order_id": case.order_id,
            "selected_order_id": case.order_id,
            "message": follow_up.message,
            "order_resolution_source": "trusted_ui_selection",
            "route": expected.route,
            "policy_decision": expected.policy_decision,
            "final_outcome": expected.outcome,
            "workflow_status": expected.terminal_state,
        }
        for field, wanted in exact.items():
            _require(
                result.get(field) == wanted,
                f"{case.trace_id} follow-up: {field} differs "
                f"(wanted {wanted!r}, got {result.get(field)!r})",
            )
        _require(result.get("success") is True, f"{case.trace_id}: follow-up was not successful")
        _require(
            result.get("matched_expectations") is True,
            f"{case.trace_id}: follow-up did not match its canonical contract",
        )
        _require(
            result.get("selected_case")
            == {
                "case_id": case.trace_id,
                "trace_id": case.trace_id,
                "ticket_id": case.ticket_id,
                "customer_id": case.customer_id,
                "order_id": case.order_id,
                "message": follow_up.message,
            },
            f"{case.trace_id}: follow-up selected-case identity differs",
        )
        _require(
            result.get("request_facts") == follow_up.request_payload(case),
            f"{case.trace_id}: observed follow-up facts differ",
        )
        response_body = str(result.get("response_body") or "").strip()
        _require(bool(response_body), f"{case.trace_id}: follow-up response is empty")
        checks = _mapping(
            result.get("response_content_checks"),
            f"{case.trace_id} follow-up response checks",
        )
        for name in (
            "decision_reflected",
            "missing_info_requested",
            "safe_summary_reflected",
            "outcome_anchor_reflected",
        ):
            _require(checks.get(name) is True, f"{case.trace_id}: follow-up {name} failed")
        _require(checks.get("pii_fields_detected") == [], f"{case.trace_id}: follow-up exposed PII")
        _require(
            checks.get("forbidden_phrases") == [],
            f"{case.trace_id}: follow-up response contains forbidden text",
        )
        _require(
            result.get("governance")
            == {"triage": "allow", "policy": "allow", "response": "allow"},
            f"{case.trace_id}: follow-up governance path differs",
        )
        refund = _mapping(result.get("refund_result"), f"{case.trace_id} follow-up refund")
        _require(refund.get("status") == "success", f"{case.trace_id}: follow-up refund failed")
        _require(refund.get("order_id") == case.order_id, f"{case.trace_id}: refund order differs")
        _require(
            _same_amount(refund.get("amount"), follow_up.requested_amount),
            f"{case.trace_id}: follow-up refund amount differs",
        )
        _require(refund.get("currency") == follow_up.currency, f"{case.trace_id}: refund currency differs")
        persistence = _mapping(result.get("persistence"), f"{case.trace_id} follow-up persistence")
        _require(
            persistence.get("database") == FINAL_DATABASE and persistence.get("matched") is True,
            f"{case.trace_id}: follow-up persistence was not proved in final",
        )
        _require(
            persistence.get("exact_root_counts")
            == {"workflow_runs": 20, "customers": 20, "orders": 20, "tickets": 20},
            f"{case.trace_id}: follow-up changed the exact root corpus",
        )
        _require(
            persistence.get("ticket_raw_text_preserved") is True,
            f"{case.trace_id}: follow-up changed the immutable initial ticket text",
        )
        history = _mapping(
            persistence.get("history"),
            f"{case.trace_id} follow-up chronology proof",
        )
        for field in (
            "queryable_in_receipt_audit",
            "queryable_in_live_tables",
            "initial_rows_preserved",
        ):
            _require(
                history.get(field) is True,
                f"{case.trace_id}: follow-up chronology check {field} failed",
            )
        snapshot_sha256 = str(history.get("snapshot_sha256") or "")
        _require(
            len(snapshot_sha256) == 64
            and all(character in "0123456789abcdef" for character in snapshot_sha256.lower()),
            f"{case.trace_id}: follow-up history snapshot digest is invalid",
        )
        _require(
            bool(str(history.get("assistant_request_info_response") or "").strip()),
            f"{case.trace_id}: initial assistant request-info response is missing",
        )
        for table in ("handoff", "audit", "governance"):
            initial_count = int(history.get(f"initial_{table}_count") or 0)
            live_count = int(history.get(f"live_{table}_count") or 0)
            _require(
                live_count > initial_count,
                f"{case.trace_id}: {table} chronology was not appended",
            )
        _require(
            history.get("continuation_handoff_count") == 4
            and history.get("continuation_audit_count") == 4
            and history.get("continuation_governance_count") == 2,
            f"{case.trace_id}: continuation chronology counts differ",
        )
        _require(
            set(_string_list(persistence.get("required_routes"), f"{case.trace_id} routes"))
            == {
                "customer->triage_agent",
                "triage_agent->policy_agent",
                "policy_agent->refund_agent",
                "refund_agent->response_agent",
                "response_agent->end",
            },
            f"{case.trace_id}: persisted continuation routes differ",
        )
        followup_state = _mapping(result.get("follow_up"), f"{case.trace_id} follow-up marker")
        _require(
            followup_state.get("idempotent") is replay,
            f"{case.trace_id}: follow-up idempotency flag differs",
        )
        _require(
            followup_state.get("status") == ("already_completed" if replay else "completed"),
            f"{case.trace_id}: follow-up durable status differs",
        )

    @staticmethod
    def _validate_followup_dashboard(
        case: DemoCase,
        result: Mapping[str, Any],
        detail: Mapping[str, Any],
    ) -> None:
        follow_up = case.follow_up
        _require(follow_up is not None, f"{case.trace_id}: no follow-up fixture exists")
        exact = {
            "traceId": case.trace_id,
            "id": case.ticket_id,
            "workflowStatus": "completed",
            "currentAgent": "completed",
            "status": "auto_approved",
            "finalOutcome": "approved",
        }
        for field, wanted in exact.items():
            _require(
                detail.get(field) == wanted,
                f"{case.trace_id} post-follow-up dashboard: {field} differs",
            )
        _require(detail.get("pendingApprovalId") is None, f"{case.trace_id}: unexpected approval")
        _require(not _list(detail.get("approvals"), f"{case.trace_id} follow-up approvals"),
                 f"{case.trace_id}: dashboard shows an unexpected approval")
        refund = _mapping(detail.get("refund"), f"{case.trace_id} follow-up dashboard refund")
        _require(refund.get("status") == "issued", f"{case.trace_id}: dashboard refund not issued")
        _require(
            _same_amount(refund.get("amount"), follow_up.requested_amount),
            f"{case.trace_id}: dashboard follow-up refund amount differs",
        )
        response = _mapping(detail.get("customerResponse"), f"{case.trace_id} follow-up response")
        _require(
            response.get("body") == result.get("response_body")
            and response.get("contentChecks") == result.get("response_content_checks"),
            f"{case.trace_id}: dashboard does not show the persisted follow-up response",
        )

    @staticmethod
    def _validate_refund_result(case: DemoCase, result: Mapping[str, Any]) -> None:
        expected = case.expectations
        mode = _mapping(result.get("mode"), f"{case.trace_id} mode")
        _require(mode == {"workflow": "live", "db": "real"}, f"{case.trace_id}: non-live mode")
        _require(
            result.get("execution_boundary")
            == {
                "entrypoint": "refund_http_api",
                "database": FINAL_DATABASE,
                "azure": "real",
            },
            f"{case.trace_id}: refund HTTP execution boundary was not attested",
        )
        _require(result.get("success") is True, f"{case.trace_id}: execution was not successful")
        _require(
            result.get("matched_expectations") is True,
            f"{case.trace_id}: matched_expectations is not true",
        )
        exact = {
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
            "route": expected.route,
            "final_outcome": expected.outcome,
            "workflow_status": expected.terminal_state,
        }
        for field, wanted in exact.items():
            _require(
                result.get(field) == wanted,
                f"{case.trace_id}: {field} differs (wanted {wanted!r}, got {result.get(field)!r})",
            )
        response_body = str(result.get("response_body") or "").strip()
        _require(
            bool(response_body) and response_body != "(no response generated)",
            f"{case.trace_id}: no customer response was generated",
        )
        response_checks = _mapping(
            result.get("response_content_checks"),
            f"{case.trace_id} response content checks",
        )
        for check_name in (
            "decision_reflected",
            "missing_info_requested",
            "safe_summary_reflected",
            "outcome_anchor_reflected",
        ):
            _require(
                response_checks.get(check_name) is True,
                f"{case.trace_id}: {check_name} failed",
            )
        _require(
            response_checks.get("pii_fields_detected") == [],
            f"{case.trace_id}: response contains PII",
        )
        _require(
            response_checks.get("forbidden_phrases") == [],
            f"{case.trace_id}: response contains forbidden text",
        )

        governance = _mapping(result.get("governance"), f"{case.trace_id} governance")
        expected_triage = "block" if case.trace_id in {"demo12", "demo13"} else "allow"
        expected_policy = None if expected_triage == "block" else "allow"
        _require(
            governance.get("triage") == expected_triage,
            f"{case.trace_id}: Triage governance differs",
        )
        _require(
            governance.get("policy") == expected_policy,
            f"{case.trace_id}: Policy governance differs",
        )
        _require(
            governance.get("response") == "allow",
            f"{case.trace_id}: Response governance differs",
        )
        expected_decision = None if expected_triage == "block" else expected.policy_decision
        _require(
            result.get("policy_decision") == expected_decision,
            f"{case.trace_id}: observed Policy decision differs",
        )
        _require(
            result.get("selected_case")
            == {
                "case_id": case.trace_id,
                "trace_id": case.trace_id,
                "ticket_id": case.ticket_id,
                "customer_id": case.customer_id,
                "order_id": case.order_id,
                "selected_order_id": case.selected_order_id,
                "message": case.message,
            },
            f"{case.trace_id}: selected-case identity differs from the canonical fixture",
        )

        persistence = _mapping(result.get("persistence"), f"{case.trace_id} persistence")
        checks = _mapping(persistence.get("checks"), f"{case.trace_id} persistence checks")
        _require(persistence.get("matched") is True, f"{case.trace_id}: persistence did not match")
        _require(bool(checks), f"{case.trace_id}: persistence checks are empty")
        failed_checks = sorted(name for name, value in checks.items() if value is not True)
        _require(not failed_checks, f"{case.trace_id}: failed persistence checks {failed_checks}")

        refund = result.get("refund_result")
        if expected.outcome == "refund_issued":
            refund = _mapping(refund, f"{case.trace_id} refund result")
            _require(refund.get("status") == "success", f"{case.trace_id}: refund did not succeed")
            _require(refund.get("order_id") == case.order_id, f"{case.trace_id}: refund order differs")
            _require(
                _same_amount(refund.get("amount"), case.ticket.get("requested_amount")),
                f"{case.trace_id}: refund amount differs",
            )
        else:
            _require(refund is None, f"{case.trace_id}: unexpected refund result")

        human_review = result.get("human_review")
        if expected.terminal_state == "pending_human":
            human_review = _mapping(human_review, f"{case.trace_id} human review")
            _require(human_review.get("status") == "pending", f"{case.trace_id}: review is not pending")
        else:
            _require(human_review is None, f"{case.trace_id}: unexpected human review")

    @staticmethod
    def _validate_dashboard_detail(case: DemoCase, detail: Mapping[str, Any]) -> None:
        expected = case.expectations
        expected_status = _expected_dashboard_status(case)
        expected_current_agent = {
            "completed": "completed",
            "pending_human": "human_approval",
            "waiting_user": "triage_agent",
        }[expected.terminal_state]
        expected_final_outcome = (
            "approved" if expected.outcome == "refund_issued" else expected.outcome
        )
        exact = {
            "traceId": case.trace_id,
            "id": case.ticket_id,
            "workflowStatus": expected.terminal_state,
            "currentAgent": expected_current_agent,
            "status": expected_status,
            "finalOutcome": expected_final_outcome,
        }
        for field, wanted in exact.items():
            _require(
                detail.get(field) == wanted,
                f"{case.trace_id} dashboard: {field} differs "
                f"(wanted {wanted!r}, got {detail.get(field)!r})",
            )
        customer_response = _mapping(
            detail.get("customerResponse"),
            f"{case.trace_id} dashboard customer response",
        )
        _require(
            bool(str(customer_response.get("body") or "").strip()),
            f"{case.trace_id}: dashboard response is empty",
        )
        dashboard_checks = _mapping(
            customer_response.get("contentChecks"),
            f"{case.trace_id} dashboard content checks",
        )
        for check_name in (
            "decision_reflected",
            "missing_info_requested",
            "safe_summary_reflected",
            "outcome_anchor_reflected",
        ):
            _require(
                dashboard_checks.get(check_name) is True,
                f"{case.trace_id}: dashboard {check_name} failed",
            )
        order = _mapping(detail.get("order"), f"{case.trace_id} dashboard order")
        _require(order.get("orderId") == case.order_id, f"{case.trace_id}: dashboard order differs")
        approvals = _list(detail.get("approvals"), f"{case.trace_id} dashboard approvals")
        if expected.terminal_state == "pending_human":
            _require(len(approvals) == 1, f"{case.trace_id}: dashboard must show one approval")
            _require(approvals[0].get("status") == "pending", f"{case.trace_id}: approval not pending")
            _require(bool(detail.get("pendingApprovalId")), f"{case.trace_id}: missing pending approval ID")
        else:
            _require(not approvals, f"{case.trace_id}: dashboard shows an unexpected approval")
            _require(detail.get("pendingApprovalId") is None, f"{case.trace_id}: unexpected approval ID")

        dashboard_refund = detail.get("refund")
        if expected.outcome == "refund_issued":
            dashboard_refund = _mapping(
                dashboard_refund,
                f"{case.trace_id} dashboard refund",
            )
            _require(
                dashboard_refund.get("status") == "issued",
                f"{case.trace_id}: dashboard refund is not issued",
            )
            _require(
                _same_amount(dashboard_refund.get("amount"), case.ticket.get("requested_amount")),
                f"{case.trace_id}: dashboard refund amount differs",
            )
        else:
            _require(dashboard_refund is None, f"{case.trace_id}: dashboard shows unexpected refund")

    def _validate_aggregates(self, cases: list[dict[str, Any]]) -> dict[str, Any]:
        case_exchange = _exchange(
            self.dashboard_client,
            "GET",
            "/api/cases",
            params={"limit": 200},
        )
        case_rows = _list(case_exchange["body"], "dashboard cases")
        by_trace = {str(row.get("traceId")): row for row in case_rows}
        _require(len(case_rows) == len(DEMO_IDS), "Dashboard case list does not contain exactly 20 rows")
        _require(set(by_trace) == set(DEMO_IDS), "Dashboard case list is not exactly demo01-demo20")
        for case in self.catalog.cases:
            self._validate_dashboard_summary(case, _mapping(by_trace[case.trace_id], case.trace_id))

        metrics_exchange = _exchange(self.dashboard_client, "GET", "/api/console-metrics")
        metrics = _mapping(metrics_exchange["body"], "dashboard metrics")
        secondary = {
            str(row.get("label")): str(row.get("value"))
            for row in _list(metrics.get("secondaryStats"), "dashboard secondary metrics")
        }
        status_breakdown = {
            str(row.get("status")): int(row.get("count"))
            for row in _list(metrics.get("statusBreakdown"), "dashboard status breakdown")
        }
        expected_status_breakdown = dict(
            Counter(_expected_dashboard_status(case) for case in self.catalog.cases)
        )
        _require(
            status_breakdown == expected_status_breakdown,
            "Metrics status breakdown differs from the 20 dashboard case projections",
        )

        audit_exchange = _exchange(
            self.dashboard_client,
            "GET",
            "/api/audit-log/query",
            params={"limit": 1000},
        )
        audit_rows = _list(audit_exchange["body"], "dashboard audit rows")
        governance_exchange = _exchange(
            self.dashboard_client,
            "GET",
            "/api/governance-events",
            params={"limit": 1000},
        )
        governance_rows = _list(governance_exchange["body"], "dashboard governance rows")
        pending_exchange = _exchange(
            self.dashboard_client,
            "GET",
            "/api/approvals/pending",
            params={"limit": 500},
        )
        pending_rows = _list(pending_exchange["body"], "dashboard pending approvals")

        expected_pending = {
            case.trace_id
            for case in self.catalog.cases
            if case.expectations.terminal_state == "pending_human"
        }
        _require(
            Counter(str(row.get("trace_id")) for row in pending_rows)
            == Counter({trace_id: 1 for trace_id in expected_pending}),
            "Pending approval endpoint does not show exactly one row for each expected trace",
        )
        expected_audit_counts = {
            entry["case_id"]: int(
                _mapping(
                    _mapping(entry["refund"]["body"], "refund body").get("persistence"),
                    "persistence",
                )["observed"]["audit_count"]
            )
            for entry in cases
        }
        expected_governance_counts = {
            entry["case_id"]: int(
                _mapping(
                    _mapping(entry["refund"]["body"], "refund body").get("persistence"),
                    "persistence",
                )["observed"]["governance_count"]
            )
            for entry in cases
        }
        _require(
            Counter(str(row.get("trace_id")) for row in audit_rows) == Counter(expected_audit_counts),
            "Audit endpoint counts differ from refund persistence evidence",
        )
        _require(
            Counter(str(row.get("trace_id")) for row in governance_rows)
            == Counter(expected_governance_counts),
            "Governance endpoint counts differ from refund persistence evidence",
        )
        _require(secondary.get("Total Cases") == "20", "Metrics total case count is not 20")
        _require(
            secondary.get("Pending Approvals") == str(len(expected_pending)),
            "Metrics pending approval count differs",
        )
        _require(
            secondary.get("Audit Events") == str(sum(expected_audit_counts.values())),
            "Metrics audit event count differs",
        )
        _require(
            secondary.get("Governance Checks") == str(sum(expected_governance_counts.values())),
            "Metrics governance event count differs",
        )
        return {
            "cases": case_exchange,
            "metrics": metrics_exchange,
            "audit": audit_exchange,
            "governance": governance_exchange,
            "pending_approvals": pending_exchange,
        }

    def _validate_post_followups(
        self,
        initial: Mapping[str, Any],
        followups: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Prove the two customer continuations through health and dashboard APIs."""

        followup_ids = {"demo10", "demo14"}
        _require(
            {str(entry.get("case_id")) for entry in followups} == followup_ids,
            "The required demo10/demo14 follow-up set was not exercised",
        )
        health = _exchange(self.refund_client, "GET", "/api/health")
        health_body = _mapping(health["body"], "post-follow-up refund health")
        _require(health_body.get("status") == "ok", "Refund health failed after follow-ups")
        _require(health_body.get("mode") == "live", "Refund service left live mode")
        _require(health_body.get("database") == FINAL_DATABASE, "Refund service left final")
        _require(health_body.get("exact_demo_roots") is True, "Follow-ups changed the root allowlist")
        _require(
            health_body.get("canonical_root_data") is True
            and health_body.get("canonical_root_fingerprint")
            == self._canonical_root_fingerprint,
            "Follow-ups changed canonical customer/order/ticket/workflow root facts",
        )
        _require(health_body.get("dirty_case_count") == 20, "Not all initial cases stayed persisted")
        _require(health_body.get("clean_case_count") == 0, "A processed case was reported clean")
        _require(health_body.get("ready_for_full_run") is False, "Dirty DB was reported runnable")
        case_states = _mapping(health_body.get("case_states"), "post-follow-up case states")
        _require(set(case_states) == set(DEMO_IDS), "Post-follow-up health lost a demo case")
        for case in self.catalog.cases:
            if case.trace_id in followup_ids:
                wanted = {"workflow_status": "completed", "current_agent": "completed"}
            else:
                wanted = {
                    "workflow_status": case.expectations.terminal_state,
                    "current_agent": {
                        "completed": "completed",
                        "pending_human": "human_approval",
                        "waiting_user": "triage_agent",
                    }[case.expectations.terminal_state],
                }
            _require(
                all(
                    _mapping(
                        case_states.get(case.trace_id),
                        f"{case.trace_id} post-follow-up state",
                    ).get(field)
                    == value
                    for field, value in wanted.items()
                ),
                f"{case.trace_id}: post-follow-up health state differs",
            )
        for trace_id in followup_ids:
            _require(
                _mapping(case_states.get(trace_id), f"{trace_id} follow-up health").get(
                    "followup_status"
                )
                == "completed",
                f"{trace_id}: health did not expose durable follow-up completion",
            )

        cases_exchange = _exchange(
            self.dashboard_client,
            "GET",
            "/api/cases",
            params={"limit": 200},
        )
        case_rows = _list(cases_exchange["body"], "post-follow-up dashboard cases")
        by_trace = {str(row.get("traceId")): row for row in case_rows}
        _require(
            len(case_rows) == 20 and set(by_trace) == set(DEMO_IDS),
            "Post-follow-up dashboard case list is not exactly demo01-demo20",
        )
        expected_statuses: Counter[str] = Counter()
        for case in self.catalog.cases:
            summary = _mapping(by_trace[case.trace_id], f"{case.trace_id} post-follow-up summary")
            if case.trace_id in followup_ids:
                wanted_status = "auto_approved"
                wanted_workflow = "completed"
            else:
                wanted_status = _expected_dashboard_status(case)
                wanted_workflow = case.expectations.terminal_state
            expected_statuses[wanted_status] += 1
            _require(summary.get("traceId") == case.trace_id, f"{case.trace_id}: summary trace differs")
            _require(summary.get("id") == case.ticket_id, f"{case.trace_id}: summary ticket differs")
            _require(
                summary.get("status") == wanted_status
                and summary.get("workflowStatus") == wanted_workflow,
                f"{case.trace_id}: post-follow-up dashboard summary differs",
            )

        metrics = _exchange(self.dashboard_client, "GET", "/api/console-metrics")
        metrics_body = _mapping(metrics["body"], "post-follow-up metrics")
        secondary = {
            str(row.get("label")): str(row.get("value"))
            for row in _list(metrics_body.get("secondaryStats"), "post-follow-up secondary metrics")
        }
        breakdown = {
            str(row.get("status")): int(row.get("count"))
            for row in _list(metrics_body.get("statusBreakdown"), "post-follow-up status breakdown")
        }
        _require(breakdown == dict(expected_statuses), "Post-follow-up status metrics differ")
        _require(secondary.get("Total Cases") == "20", "Post-follow-up metrics lost cases")

        pending = _exchange(
            self.dashboard_client,
            "GET",
            "/api/approvals/pending",
            params={"limit": 500},
        )
        pending_rows = _list(pending["body"], "post-follow-up pending approvals")
        expected_pending = {
            case.trace_id
            for case in self.catalog.cases
            if case.expectations.terminal_state == "pending_human"
        }
        _require(
            Counter(str(row.get("trace_id")) for row in pending_rows)
            == Counter({trace_id: 1 for trace_id in expected_pending}),
            "Customer follow-ups changed the human-approval queue",
        )
        _require(
            secondary.get("Pending Approvals") == str(len(expected_pending)),
            "Post-follow-up pending metric differs",
        )

        audit = _exchange(
            self.dashboard_client,
            "GET",
            "/api/audit-log/query",
            params={"limit": 1000},
        )
        audit_rows = _list(audit["body"], "post-follow-up audit rows")
        initial_audit_rows = _list(
            _mapping(initial.get("audit"), "initial audit exchange").get("body"),
            "initial audit rows",
        )
        _require(len(audit_rows) > len(initial_audit_rows), "Follow-ups added no audit history")
        for trace_id in followup_ids:
            events = Counter(
                str(row.get("event_type"))
                for row in audit_rows
                if row.get("trace_id") == trace_id
            )
            _require(
                events["customer_followup_received"] == 1
                and events["customer_followup_completed"] == 1,
                f"{trace_id}: customer continuation audit markers are not singular",
            )

        governance = _exchange(
            self.dashboard_client,
            "GET",
            "/api/governance-events",
            params={"limit": 1000},
        )
        governance_rows = _list(governance["body"], "post-follow-up governance rows")
        initial_governance_rows = _list(
            _mapping(initial.get("governance"), "initial governance exchange").get("body"),
            "initial governance rows",
        )
        for trace_id in followup_ids:
            before = sum(1 for row in initial_governance_rows if row.get("trace_id") == trace_id)
            after = sum(1 for row in governance_rows if row.get("trace_id") == trace_id)
            _require(after > before, f"{trace_id}: follow-up governance history was not appended")
        _require(
            secondary.get("Audit Events") == str(len(audit_rows)),
            "Post-follow-up audit metric differs",
        )
        _require(
            secondary.get("Governance Checks") == str(len(governance_rows)),
            "Post-follow-up governance metric differs",
        )
        return {
            "refund_health": health,
            "cases": cases_exchange,
            "metrics": metrics,
            "audit": audit,
            "governance": governance,
            "pending_approvals": pending,
        }

    @staticmethod
    def _validate_dashboard_summary(case: DemoCase, summary: Mapping[str, Any]) -> None:
        expected = case.expectations
        _require(summary.get("traceId") == case.trace_id, f"{case.trace_id}: summary trace differs")
        _require(summary.get("id") == case.ticket_id, f"{case.trace_id}: summary ticket differs")
        _require(
            summary.get("workflowStatus") == expected.terminal_state,
            f"{case.trace_id}: summary workflow state differs",
        )
        _require(
            summary.get("status") == _expected_dashboard_status(case),
            f"{case.trace_id}: summary derived status differs",
        )

    def _run_approval(
        self,
        plan: ApprovalPlan,
        initial_pending: Any,
    ) -> dict[str, Any]:
        pending_rows = _list(initial_pending, "initial pending approvals")
        matches = [row for row in pending_rows if row.get("trace_id") == plan.trace_id]
        _require(len(matches) == 1, "demo07 must have exactly one pending approval")
        _require(
            matches[0].get("approved_next_agent") == "refund_agent",
            "demo07 pending approval does not route approval to the Refund Agent",
        )
        approval_id = str(matches[0].get("approval_id") or "")
        _require(bool(approval_id), "demo07 pending approval has no approval_id")
        payload = plan.request_payload(approval_id)
        path = f"/api/approvals/{plan.trace_id}/resolve"

        first = _exchange(self.dashboard_client, "POST", path, json_body=payload)
        first_body = _mapping(first["body"], "demo07 approval response")
        _require(first_body.get("trace_id") == plan.trace_id, "Approval response trace differs")
        _require(first_body.get("approval_id") == approval_id, "Approval response ID differs")
        _require(first_body.get("decision") == plan.decision, "Approval response decision differs")
        _require(
            _same_amount(first_body.get("resolved_amount"), plan.resolved_amount)
            if plan.resolved_amount is not None
            else first_body.get("resolved_amount") is None,
            "Approval response amount differs",
        )
        _require(first_body.get("idempotent") is False, "First approval unexpectedly idempotent")
        _require(
            first_body.get("continuation_status") == "completed",
            "First approval continuation did not complete",
        )

        replay = _exchange(self.dashboard_client, "POST", path, json_body=payload)
        replay_body = _mapping(replay["body"], "demo07 approval replay")
        _require(replay_body.get("approval_id") == approval_id, "Replay approval ID differs")
        _require(replay_body.get("idempotent") is True, "Approval replay was not idempotent")
        _require(
            replay_body.get("continuation_status") == "already_completed",
            "Approval replay did not return the completed durable state",
        )
        _require(
            replay_body.get("refund_result") == first_body.get("refund_result"),
            "Approval replay changed the refund result",
        )

        detail = _exchange(self.dashboard_client, "GET", f"/api/cases/{plan.trace_id}")
        detail_body = _mapping(detail["body"], "post-approval dashboard detail")
        _require(detail_body.get("pendingApprovalId") is None, "demo07 is still pending after approval")
        _require(detail_body.get("workflowStatus") == "completed", "demo07 did not complete")
        approvals = _list(detail_body.get("approvals"), "post-approval approvals")
        _require(len(approvals) == 1, "demo07 approval history is not singular")
        _require(
            approvals[0].get("status") in {"approved", "rejected"},
            "demo07 approval is not resolved",
        )
        if plan.decision in {"approve", "partial_refund"}:
            refund = _mapping(detail_body.get("refund"), "post-approval refund")
            _require(refund.get("status") == "issued", "demo07 refund is not issued")
            _require(
                _same_amount(refund.get("amount"), plan.resolved_amount),
                "demo07 refund amount differs from the resolution",
            )
        else:
            _require(detail_body.get("refund") is None, "Denied demo07 unexpectedly has a refund")

        pending_after = _exchange(
            self.dashboard_client,
            "GET",
            "/api/approvals/pending",
            params={"limit": 500},
        )
        pending_after_rows = _list(pending_after["body"], "pending after")
        expected_pending_after = {
            case.trace_id
            for case in self.catalog.cases
            if case.expectations.terminal_state == "pending_human" and case.trace_id != plan.trace_id
        }
        _require(
            Counter(str(row.get("trace_id")) for row in pending_after_rows)
            == Counter({trace_id: 1 for trace_id in expected_pending_after}),
            "Pending approvals after demo07 resolution are not the expected remaining set",
        )
        metrics_after = _exchange(self.dashboard_client, "GET", "/api/console-metrics")
        metrics_after_body = _mapping(metrics_after["body"], "post-approval metrics")
        secondary_after = {
            str(row.get("label")): str(row.get("value"))
            for row in _list(
                metrics_after_body.get("secondaryStats"),
                "post-approval secondary metrics",
            )
        }
        _require(secondary_after.get("Total Cases") == "20", "Post-approval metrics lost cases")
        _require(
            secondary_after.get("Pending Approvals") == str(len(expected_pending_after)),
            "Post-approval pending metric differs",
        )
        _redact_approval_request(first)
        _redact_approval_request(replay)
        return {
            "trace_id": plan.trace_id,
            "decision": plan.decision,
            "resolved_amount": float(plan.resolved_amount) if plan.resolved_amount is not None else None,
            "first": first,
            "replay": replay,
            "dashboard_after": detail,
            "pending_after": pending_after,
            "metrics_after": metrics_after,
        }


def run_http_acceptance(
    config: HttpAcceptanceConfig,
    *,
    refund_client: httpx.Client | None = None,
    dashboard_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Run the strict acceptance workflow and always write its JSON report."""

    return HttpAcceptanceHarness(
        config,
        refund_client=refund_client,
        dashboard_client=dashboard_client,
    ).run()


def _exchange(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = client.request(method, path, params=params, json=json_body)
    except httpx.RequestError as error:
        raise HttpAcceptanceError(f"{method} {path} could not reach the service: {error}") from error
    if response.status_code != 200:
        detail = _safe_error_detail(response)
        raise HttpAcceptanceError(
            f"{method} {path} returned HTTP {response.status_code}: {detail}"
        )
    try:
        body = response.json()
    except ValueError as error:
        raise HttpAcceptanceError(f"{method} {path} did not return JSON") from error
    return {
        "request": {
            "method": method,
            "path": path,
            "params": dict(params or {}),
            "json": dict(json_body or {}) if json_body is not None else None,
        },
        "status_code": response.status_code,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "body": body,
    }


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "non-JSON error response"
    if isinstance(body, Mapping):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail[:500]
    return "request failed"


def _redact_approval_request(exchange: dict[str, Any]) -> None:
    """Retain proof that reviewer fields were supplied without persisting them."""

    request = exchange.get("request")
    if not isinstance(request, dict):
        return
    payload = request.get("json")
    if not isinstance(payload, dict):
        return
    if "reviewer" in payload:
        payload["reviewer"] = "[provided]"
    if "notes" in payload:
        payload["notes"] = "[provided]"


def _expected_dashboard_status(case: DemoCase) -> str:
    if case.trace_id in {"demo12", "demo13"}:
        return "quarantined"
    outcome = case.expectations.outcome
    return {
        "refund_issued": "auto_approved",
        "denied": "rejected",
        "need_info": "needs_info",
        "manual_review": "manual_review",
    }[outcome]


def _same_amount(left: Any, right: Any) -> bool:
    try:
        return abs(Decimal(str(left)) - Decimal(str(right))) < Decimal("0.005")
    except (InvalidOperation, TypeError, ValueError):
        return False


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HttpAcceptanceError(f"{label} must be a JSON object")
    return value


def _list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise HttpAcceptanceError(f"{label} must be a JSON array of objects")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(row, str) for row in value):
        raise HttpAcceptanceError(f"{label} must be a JSON array of strings")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HttpAcceptanceError(message)


def _normalize_base_url(value: str, label: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HttpAcceptanceError(f"{label} base URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise HttpAcceptanceError(f"{label} base URL must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise HttpAcceptanceError(f"{label} base URL must not contain a path, query, or fragment")
    netloc = parsed.hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _source_metadata(manifest_path: Path) -> dict[str, Any]:
    resolved_manifest = manifest_path.resolve()
    try:
        relative_manifest = resolved_manifest.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        relative_manifest = resolved_manifest.name
    return {
        "commit_scope": "acceptance_client_checkout",
        "commit": _git_output("rev-parse", "HEAD") or "unknown",
        "dirty": bool(_git_output("status", "--porcelain")),
        "fixture": relative_manifest,
        "fixture_sha256": hashlib.sha256(resolved_manifest.read_bytes()).hexdigest(),
    }


def _git_output(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    output = path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run demo01-demo20 through the live refund HTTP API and verify each result "
            "through the live dashboard HTTP API, then resume and replay the two "
            "waiting-customer cases. Services must already be running."
        )
    )
    parser.add_argument("--confirm-live", required=True, choices=[FINAL_DATABASE])
    parser.add_argument("--refund-url", default=DEFAULT_REFUND_URL)
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--approval-decision",
        choices=["approve", "partial_refund", "deny"],
        help="optionally resolve and idempotently replay demo07 after the initial proof",
    )
    parser.add_argument("--approval-amount")
    parser.add_argument("--approval-reviewer")
    parser.add_argument("--approval-notes")
    return parser


def _approval_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> ApprovalPlan | None:
    supplied = [
        args.approval_decision,
        args.approval_amount,
        args.approval_reviewer,
        args.approval_notes,
    ]
    if not any(value is not None for value in supplied):
        return None
    if args.approval_decision is None or args.approval_reviewer is None or args.approval_notes is None:
        parser.error(
            "approval phase requires --approval-decision, --approval-reviewer, and --approval-notes"
        )
    amount: Decimal | None = None
    if args.approval_amount is not None:
        try:
            amount = Decimal(args.approval_amount)
        except InvalidOperation:
            parser.error("--approval-amount must be a decimal number")
    if args.approval_decision != "deny" and amount is None:
        parser.error("refund approval requires --approval-amount")
    if args.approval_decision == "deny" and amount is not None:
        parser.error("denial must omit --approval-amount")
    return ApprovalPlan(
        decision=args.approval_decision,
        resolved_amount=amount,
        reviewer=args.approval_reviewer,
        notes=args.approval_notes,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    approval = _approval_from_args(args, parser)
    config = HttpAcceptanceConfig(
        refund_base_url=args.refund_url,
        dashboard_base_url=args.dashboard_url,
        output_path=args.output,
        manifest_path=args.manifest,
        timeout_seconds=args.timeout,
        approval=approval,
    )
    try:
        report = run_http_acceptance(config)
    except HttpAcceptanceError as error:
        print(f"HTTP acceptance failed: {error}")
        print(f"Report: {config.output_path.resolve()}")
        return 1
    print(
        "HTTP acceptance passed: "
        f"{report['summary']['matched_expectations']}/20 refund responses and "
        f"{report['summary']['dashboard_observed']}/20 dashboard observations; "
        f"{report['summary']['followups_matched']}/2 customer follow-ups"
    )
    print(f"Report: {config.output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
