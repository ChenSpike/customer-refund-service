"""Real Triage continuation for the two canonical waiting-customer cases."""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

from db.followup_context import followup_fence
from db.followup_store import CustomerFollowupConflictError, CustomerFollowupStore
from demo.catalog import DemoCase
from demo.runner import _scoped_environment


class CustomerFollowupExecutionError(RuntimeError):
    """The claimed follow-up failed before a proven terminal result."""


class CustomerFollowupService:
    """Resume one waiting workflow through the real graph and persistence stack."""

    def __init__(
        self,
        store: CustomerFollowupStore,
        *,
        graph: Any | None = None,
        clock: Any = time.perf_counter,
    ) -> None:
        self.store = store
        self.graph = graph
        self.clock = clock

    def run(self, case: DemoCase) -> dict[str, Any]:
        if case.trace_id not in {"demo10", "demo14"} or case.follow_up is None:
            raise ValueError("Customer follow-up is defined only for demo10 and demo14")

        started = self.clock()
        claim = self.store.claim(case)
        if claim.completed_result is not None:
            replay = deepcopy(claim.completed_result)
            replay["follow_up"] = {
                "idempotent": True,
                "status": "already_completed",
                "receipt_log_id": claim.receipt_log_id,
                "recovered_without_graph": claim.recovered,
            }
            replay.setdefault("timings_ms", {})["replay_total"] = _milliseconds(
                self.clock() - started
            )
            return replay

        if not claim.claim_token:
            raise CustomerFollowupExecutionError(
                f"{case.trace_id}: continuation claim did not include a fencing token"
            )
        try:
            workflow_started = self.clock()
            # The initial batch and the continuation must evaluate the same
            # fixed policy date; otherwise a later demo date can cross the
            # 30-day window and change an identical case's decision.
            graph_input = case.follow_up_graph_input()
            graph_input["request_context"]["followup_claim_token"] = claim.claim_token
            if claim.assistant_response:
                graph_input["conversation_history"] = [
                    {"role": "user", "content": case.message},
                    {"role": "assistant", "content": claim.assistant_response},
                ]
            with (
                _scoped_environment("POLICY_EVALUATION_DATE", case.evaluation_date),
                followup_fence(case.trace_id, claim.claim_token),
            ):
                state = self._live_graph().invoke(graph_input)
            workflow_ms = _milliseconds(self.clock() - workflow_started)
            result = normalize_followup_state(case, state)
            result["timings_ms"] = {
                "workflow": workflow_ms,
                "total": _milliseconds(self.clock() - started),
            }
            if not result["matched_expectations"]:
                raise CustomerFollowupExecutionError(
                    f"{case.trace_id}: resumed graph failed the follow-up acceptance contract"
                )
            return self.store.complete(case, result, claim.claim_token)
        except CustomerFollowupConflictError:
            raise
        except Exception as error:
            try:
                self.store.fail(case, error, claim.claim_token)
            except Exception as persistence_error:
                raise CustomerFollowupExecutionError(
                    f"{case.trace_id}: continuation and failure persistence both failed"
                ) from persistence_error
            if isinstance(error, CustomerFollowupExecutionError):
                raise
            raise CustomerFollowupExecutionError(
                f"{case.trace_id}: customer follow-up continuation failed"
            ) from error

    def _live_graph(self) -> Any:
        if self.graph is None:
            from app.graph import build_graph

            self.graph = build_graph(repository=self.store.repository)
        return self.graph


def normalize_followup_state(case: DemoCase, state: dict[str, Any]) -> dict[str, Any]:
    """Normalize the resumed graph and evaluate its strict fixture contract."""

    follow_up = case.follow_up
    if follow_up is None:
        raise ValueError(f"{case.trace_id}: no customer follow-up is defined")
    response_result = state.get("response_result") or {}
    response_payload = response_result.get("response") or {}
    response_checks = response_result.get("content_checks") or {}
    refund_result = state.get("refund_result") or {}
    policy_decision = state.get("policy_decision") or {}
    order_lookup = state.get("order_lookup_result") or {}
    triage_governance = state.get("triage_governance_result") or {}
    policy_governance = state.get("policy_governance_result") or {}
    response_governance = state.get("response_governance_result") or {}
    route = str((state.get("policy_persistence_result") or {}).get("next_agent") or "")
    workflow_status = str(state.get("workflow_status") or "")
    if workflow_status == "waiting_human":
        workflow_status = "pending_human"
    outcome = str(state.get("final_outcome") or "")
    if refund_result.get("status") == "success":
        outcome = "refund_issued"

    expected = follow_up.expectations.as_dict()
    identities_match = (
        state.get("trace_id") == case.trace_id
        and state.get("ticket_id") == case.ticket_id
        and state.get("user_id") == case.customer_id
        and order_lookup.get("order_id") == case.order_id
        and state.get("message") == follow_up.message
        and state.get("requested_order_id") == case.order_id
        and state.get("order_resolution_source") == "trusted_ui_selection"
    )
    refund_matches = (
        refund_result.get("status") == "success"
        and refund_result.get("order_id") == case.order_id
        and abs(float(refund_result.get("amount") or 0) - follow_up.requested_amount)
        < 0.005
        and refund_result.get("currency") == follow_up.currency
    )
    governance_matches = (
        triage_governance.get("status") == "allow"
        and policy_governance.get("status") == "allow"
        and response_governance.get("status") == "allow"
    )
    response_matches = (
        all(
            response_checks.get(name) is True
            for name in (
                "decision_reflected",
                "missing_info_requested",
                "safe_summary_reflected",
                "outcome_anchor_reflected",
            )
        )
        and response_checks.get("pii_fields_detected") == []
        and response_checks.get("forbidden_phrases") == []
        and not response_checks.get("semantic_errors")
    )
    actual_policy_decision = str(
        policy_decision.get("decision") or policy_decision.get("type") or ""
    )
    matched = (
        identities_match
        and refund_matches
        and governance_matches
        and response_matches
        and route == expected["route"]
        and actual_policy_decision == expected["policy_decision"]
        and outcome == expected["outcome"]
        and workflow_status == expected["terminal_state"]
    )
    return {
        "case_id": case.trace_id,
        "trace_id": state.get("trace_id"),
        "ticket_id": state.get("ticket_id"),
        "customer_id": state.get("user_id"),
        "order_id": order_lookup.get("order_id"),
        "selected_order_id": case.order_id,
        "initial_message": case.message,
        "message": state.get("message"),
        "request_facts": follow_up.request_payload(case),
        "order_resolution_source": state.get("order_resolution_source"),
        "route": route,
        "policy_decision": actual_policy_decision,
        "final_outcome": outcome,
        "workflow_status": workflow_status,
        "response_body": (
            response_payload.get("body")
            or response_result.get("body")
            or "(no response generated)"
        ),
        "response_content_checks": response_checks,
        "governance": {
            "triage": triage_governance.get("status"),
            "policy": policy_governance.get("status"),
            "response": response_governance.get("status"),
        },
        "refund_result": refund_result or None,
        "expected": expected,
        "success": True,
        "matched_expectations": matched,
    }


def _milliseconds(seconds: float) -> float:
    return round(seconds * 1000, 3)


__all__ = [
    "CustomerFollowupExecutionError",
    "CustomerFollowupService",
    "normalize_followup_state",
]
