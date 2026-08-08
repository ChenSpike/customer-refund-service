"""Triage governance — deterministic ASI07 data-leakage check.

Unlike the policy governance node (an Azure OWASP LLM scan), triage governance is
a deterministic rule check on the raw order lookup: the whole point is to catch
an ownership/schema leak before the order data reaches Policy. It implements the
shared BaseGovernanceNode contract (__call__(state) -> dict) and returns
`triage_governance_result` (the per-stage key the router and human_approval read).

Team note: the result is a plain verdict dict, not a GovernanceAssessment. ASI07
ownership/schema is not one of the LLM OWASP flags (semantic_drift/forbidden_tool/
pii_risk), so persisting these events to governance_events will need a small
mapping in the triage persistence layer (deferred, same as the write path).
"""
from __future__ import annotations

from typing import Any

from governance import BaseGovernanceNode
from governance.audit_logger import log_governance_event
from governance.checkers import check_pii_risk, check_semantic_drift

# Every field Order_Database_Lookup must return, with expected Python types.
_REQUIRED_FIELDS: dict[str, type | tuple] = {
    "order_id":            str,
    "order_customer_id":   str,
    "product_type":        str,
    "purchase_date":       str,
    "item_status":         str,
    "amount_paid":         (int, float),
    "prior_refund_total":  (int, float),
    "contact_customer_id": str,
    "contact_email":       str,
    "contact_name":        str,
}

_VALID_ITEM_STATUSES = {"delivered", "damaged", "returned", "unknown"}


def _allow(name: str) -> dict:
    return {"name": name, "status": "allow", "detail": "", "evidence": {}}


def _block(name: str, detail: str, evidence: dict) -> dict:
    return {"name": name, "status": "block", "detail": detail, "evidence": evidence}


def check_data_leakage(state: dict[str, Any]) -> dict:
    """ASI07: schema validation + ownership match on the raw order lookup."""
    raw = state.get("order_lookup_result") or {}
    if not raw:
        # Nothing was looked up (awaiting order id, content-filter block, ...).
        return _allow("data_leakage")

    user_id = state.get("user_id")

    # A. Schema validation
    for field, expected_type in _REQUIRED_FIELDS.items():
        if field not in raw:
            return _block("data_leakage",
                          f"ASI07 schema: missing required field '{field}'",
                          {"rule": "ASI07", "failed_check": "schema", "field": field})
        if not isinstance(raw[field], expected_type):
            return _block("data_leakage",
                          f"ASI07 schema: field '{field}' has wrong type "
                          f"(expected {expected_type}, got {type(raw[field]).__name__})",
                          {"rule": "ASI07", "failed_check": "schema", "field": field})

    if raw["item_status"] not in _VALID_ITEM_STATUSES:
        return _block("data_leakage",
                      f"ASI07 schema: invalid item_status '{raw['item_status']}'",
                      {"rule": "ASI07", "failed_check": "schema", "field": "item_status"})

    # B. Ownership — the joined contact must belong to the requesting user.
    if raw["contact_customer_id"] != user_id:
        return _block("data_leakage",
                      f"ASI07 ownership: contact_customer_id '{raw['contact_customer_id']}' "
                      f"does not match requesting user '{user_id}'",
                      {"rule": "ASI07", "failed_check": "ownership",
                       "offending_field": "contact_customer_id",
                       "offending_value": raw["contact_customer_id"]})

    return _allow("data_leakage")


class GovernanceNode(BaseGovernanceNode):
    """Deterministic triage governance: PII / semantic-drift / ASI07 leakage."""

    CHECKERS = (check_pii_risk, check_semantic_drift, check_data_leakage)

    def __init__(self, name: str = "triage") -> None:
        self.name = name

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        findings = [checker(state) for checker in self.CHECKERS]
        blocked = [item for item in findings if item["status"] == "block"]

        result = {
            "stage": self.name,
            "status": "block" if blocked else "allow",
            "findings": blocked,
            "all_checks": findings,
        }

        log_governance_event(
            trace_id=state.get("trace_id", "unknown"),
            ticket_id=state.get("ticket_id"),
            user_id=state.get("user_id"),
            result=result,
            stage=self.name,
        )

        return {"triage_governance_result": result}
