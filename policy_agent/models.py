from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PolicyEffect = Literal["supports_approval", "supports_denial", "supports_partial", "requires_review"]
GapType = Literal["missing_fact", "policy_conflict", "low_confidence"]
DecisionType = Literal["approve", "deny", "partial_refund", "request_info", "manual_review"]
NextAgent = Literal["response_agent", "human_approval", "triage_agent"]
InterceptorAction = Literal["allow", "quarantine", "block"]
GovernanceFlag = Literal["low_confidence", "policy_conflict", "semantic_drift", "forbidden_tool", "pii_risk"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseContext(StrictModel):
    trace_id: str
    ticket_id: str
    policy_version: str


class CustomerRequest(StrictModel):
    sanitized_text: str
    refund_reason: str | None
    requested_amount: float | None = Field(ge=0)
    currency: str


class OrderFacts(StrictModel):
    order_id: str
    product_type: str
    purchase_date: date
    item_status: str
    amount_paid: float = Field(ge=0)
    prior_refund_total: float = Field(ge=0)


class PolicyAgentInput(StrictModel):
    """Proposal Policy Agent input JSON in exact field order."""

    case: CaseContext
    customer_request: CustomerRequest
    order_facts: OrderFacts


class OutputCase(StrictModel):
    trace_id: str
    ticket_id: str
    policy_version_used: str


class MatchedPolicy(StrictModel):
    policy_id: str
    rule_summary: str
    input_fact_used: str
    effect: PolicyEffect


class PolicyGapOrConflict(StrictModel):
    type: GapType
    detail: str


class PolicyEvaluation(StrictModel):
    matched_policies: list[MatchedPolicy] = Field(default_factory=list)
    gaps_or_conflicts: list[PolicyGapOrConflict] = Field(default_factory=list)


class PolicyDecision(StrictModel):
    type: DecisionType
    refund_amount: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    reason: str


class ResponseGuidance(StrictModel):
    customer_safe_summary: str
    missing_info_to_request: list[str] = Field(default_factory=list)


class Handoff(StrictModel):
    next_agent: NextAgent
    reason: str


class Governance(StrictModel):
    semantic_drift_score: float = Field(ge=0, le=1)
    interceptor_action: InterceptorAction
    flags: list[GovernanceFlag] = Field(default_factory=list)


class PolicyAgentDraft(StrictModel):
    case: OutputCase
    customer_request: CustomerRequest
    policy_evaluation: PolicyEvaluation
    decision: PolicyDecision
    response_guidance: ResponseGuidance
    handoff: Handoff


class PolicyAgentOutput(PolicyAgentDraft):
    """Proposal Policy Agent output JSON in exact field order."""

    governance: Governance

    @model_validator(mode="after")
    def validate_route(self) -> "PolicyAgentOutput":
        expected_route = {
            "request_info": "triage_agent",
            "manual_review": "human_approval",
            "approve": "response_agent",
            "deny": "response_agent",
            "partial_refund": "response_agent",
        }[self.decision.type]
        if self.handoff.next_agent != expected_route:
            raise ValueError(f"{self.decision.type} must route to {expected_route}")
        if self.governance.interceptor_action in {"quarantine", "block"}:
            if self.decision.type != "manual_review" or self.handoff.next_agent != "human_approval":
                raise ValueError("quarantine or block must route to manual human approval")
        return self


class TokenUsage(StrictModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


def exact_policy_input(payload: dict) -> PolicyAgentInput:
    """Discard triage-only fields and preserve the Proposal input order."""

    normalized = {
        "case": {
            "trace_id": payload["case"]["trace_id"],
            "ticket_id": payload["case"]["ticket_id"],
            "policy_version": payload["case"]["policy_version"],
        },
        "customer_request": {
            "sanitized_text": payload["customer_request"]["sanitized_text"],
            "refund_reason": payload["customer_request"]["refund_reason"],
            "requested_amount": payload["customer_request"]["requested_amount"],
            "currency": payload["customer_request"]["currency"],
        },
        "order_facts": {
            "order_id": payload["order_facts"]["order_id"],
            "product_type": payload["order_facts"]["product_type"],
            "purchase_date": payload["order_facts"]["purchase_date"],
            "item_status": payload["order_facts"]["item_status"],
            "amount_paid": payload["order_facts"]["amount_paid"],
            "prior_refund_total": payload["order_facts"]["prior_refund_total"],
        },
    }
    return PolicyAgentInput.model_validate(normalized)
