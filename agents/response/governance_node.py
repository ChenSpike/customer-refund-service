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
from governance.node import (
    build_check_result_payload,
    build_statement_from_check_results,
)


class ResponseGovernanceNode(BaseGovernanceNode):
    """Deterministic ASI02 + ASI07 governance for the response agent output."""

    STAGE = "response"
    AGENT = "response_agent"

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        trace_id  = state.get("trace_id", "unknown")
        ticket_id = state.get("ticket_id")
        user_id   = state.get("user_id")

        findings = [
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

        log_governance_event(
            trace_id=trace_id,
            ticket_id=ticket_id,
            user_id=user_id,
            result=payload,
            stage=self.STAGE,
        )

        return {
            "current_stage":              "response_governance",
            "response_governance_result": payload,
            "audit_trail":                [statement.model_dump(mode="json")],
        }
