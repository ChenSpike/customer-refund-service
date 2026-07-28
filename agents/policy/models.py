from typing import Literal, TypedDict


DecisionType = Literal[
    "approve",
    "deny",
    "partial_refund",
    "request_info",
    "manual_review",
]

ConfidenceLevel = Literal["high", "medium", "low"]


class PolicyDecision(TypedDict):
    decision: DecisionType
    refund_amount: float
    reason: str
    confidence: ConfidenceLevel


class PolicyNodeResult(TypedDict, total=False):
    trace_id: str
    ticket_id: str
    policy_decision: PolicyDecision
    llm_input_tokens: int
    llm_output_tokens: int