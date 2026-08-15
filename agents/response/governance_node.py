"""
agents/response/governance_node.py

Response governance node — runs after response_node, before END or human_approval.

Checkers:
  check_response_tool_misuse  ASI02 — no tool-invocation language in draft body
  check_response_pii          ASI07 — no emails or phone numbers in draft body

Reads:  response_result.response.body  (written by response_node)
Writes: response_governance_result, audit_trail
"""

from __future__ import annotations

from typing import Any

from governance.audit_logger import log_governance_event
from governance.base import BaseGovernanceNode
from governance.checkers import check_response_pii, check_response_tool_misuse
from governance import GovernanceCheckResult, GovernanceEventWriter
from governance.node import (
    build_check_result_payload,
    build_statement_from_check_results,
)


def check_response_semantics(state: dict[str, Any]) -> GovernanceCheckResult:
    """Fail closed when the generated customer message contradicts its state."""

    response_result = state.get("response_result") or {}
    checks = response_result.get("content_checks") or {}
    required_checks = (
        "decision_reflected",
        "missing_info_requested",
        "safe_summary_reflected",
        "outcome_anchor_reflected",
    )
    failed = [name for name in required_checks if checks.get(name) is not True]
    if not failed:
        return GovernanceCheckResult(
            name="semantic_drift",
            status="allow",
            source="deterministic",
        )

    errors = checks.get("semantic_errors")
    if not isinstance(errors, list) or not errors:
        errors = [f"missing or failed semantic check: {name}" for name in failed]
    return GovernanceCheckResult(
        name="semantic_drift",
        status="block",
        detail="; ".join(str(error) for error in errors),
        evidence={
            "failed_checks": failed,
            "expected_outcome": response_result.get("final_outcome")
            or state.get("final_outcome"),
        },
        source="deterministic",
    )


class ResponseGovernanceNode(BaseGovernanceNode):
    """Deterministic semantic, ASI02, and ASI07 response governance."""

    STAGE = "response"
    AGENT = "response_agent"

    def __init__(self, event_writer: GovernanceEventWriter | None = None) -> None:
        self.event_writer = event_writer

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        trace_id  = state.get("trace_id", "unknown")
        ticket_id = state.get("ticket_id")
        user_id   = state.get("user_id")

        findings = [
            check_response_semantics(state),
            check_response_tool_misuse(state),
            check_response_pii(state),
        ]

        payload = build_check_result_payload(self.STAGE, findings)

        statement = build_statement_from_check_results(
            trace_id=trace_id,
            agent=self.AGENT,
            stage=self.STAGE,
            findings=findings,
        )

        governance_event_id = None
        if self.event_writer is not None:
            governance_event_id = self.event_writer.save_event(statement)

        log_governance_event(
            trace_id=trace_id,
            ticket_id=ticket_id,
            user_id=user_id,
            result=payload,
            stage=self.STAGE,
        )

        response_blocked = payload["status"] == "block"
        pending_review = (
            bool(state.get("human_review_required"))
            or (state.get("human_review") or {}).get("status") == "pending"
        )
        workflow_status = (
            "waiting_human"
            if response_blocked or pending_review
            else state.get("workflow_status", "completed")
        )
        patch = {
            "current_stage":              "response_governance",
            "response_governance_result": payload,
            # A clean customer-facing response must not release an approval
            # created by Triage or Policy. Only the explicit approval service
            # may clear that durable pause.
            "human_review_required":      response_blocked or pending_review,
            "workflow_status":            workflow_status,
            "review_trigger_stage":       self.STAGE if response_blocked else state.get("review_trigger_stage", ""),
            "review_trigger_reason":      "governance_block" if response_blocked else state.get("review_trigger_reason", ""),
            "audit_trail":                [statement.model_dump(mode="json")],
        }
        if response_blocked or pending_review:
            # Response persistence reads the nested status first. Keep it in
            # lockstep with the governance patch so a quarantined draft cannot
            # be persisted as a completed customer response.
            response_result = dict(state.get("response_result") or {})
            response_result["workflow_status"] = "waiting_human"
            if response_blocked:
                response_result["delivery_status"] = "quarantined"
            patch["response_result"] = response_result
        if governance_event_id is not None:
            patch["governance_event_id"] = governance_event_id
        return patch
