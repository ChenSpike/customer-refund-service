from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from pydantic import Field, model_validator
from governance import Governance, GovernanceAssessment, GovernanceFinding, GovernanceFlag

from .azure import AzureJsonClient
from .governance_node import GovernanceNode
from .models import (
    ConfidenceEvidence,
    ConfidenceLevel,
    DecisionType,
    Handoff,
    PolicyAgentInput,
    PolicyAgentOutput,
    PolicyDecision,
    PolicyEvaluation,
    PolicyEvidenceManifest,
    PolicyReasoningResult,
    PrecedentContext,
    PrecedentEvidence,
    ResponseGuidance,
    StrictModel,
    TokenUsage,
    exact_policy_input,
)
from .policy_node import (
    PolicyReasoningNode,
    load_policy_context,
    validate_policy_result,
)
from .routing import RouteName, handoff_reason, route_policy


class PolicyDecisionState(StrictModel):
    decision: DecisionType
    refund_amount: float = Field(ge=0)
    confidence: Literal[0, 1, 2, 3]
    confidence_level: ConfidenceLevel
    confidence_evidence: ConfidenceEvidence
    precedent_evidence: PrecedentEvidence
    reason: str


class PolicyContextState(StrictModel):
    policy_version_used: str
    policy_evaluation: PolicyEvaluation
    response_guidance: ResponseGuidance
    evidence_manifest: PolicyEvidenceManifest
    precedent_context: PrecedentContext


class PolicyGovernanceResultState(StrictModel):
    stage: Literal["policy"]
    status: Literal["allow", "block"]
    semantic_drift_score: float = Field(ge=0, le=1)
    flags: list[GovernanceFlag]
    findings: list[GovernanceFinding]

    @model_validator(mode="after")
    def validate_findings(self) -> "PolicyGovernanceResultState":
        finding_flags = [finding.flag for finding in self.findings]
        if self.flags != finding_flags:
            raise ValueError("governance flags must match findings in the same order")
        if self.status == "block" and not self.findings:
            raise ValueError("blocked governance requires at least one finding")
        if self.status == "allow" and self.findings:
            raise ValueError("allowed governance cannot contain findings")
        return self


class RiskFindingState(StrictModel):
    stage: Literal["policy"]
    flag: GovernanceFlag
    score: float | None = Field(default=None, ge=0, le=1)
    detail: str
    offending_content: str | None


class LlmUsageEvent(StrictModel):
    agent: Literal["policy_agent"]
    stage: Literal["policy_reasoning", "policy_governance"]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class PolicyAppState(TypedDict, total=False):
    trace_id: str
    ticket_id: str
    triage_output: dict[str, Any]
    current_stage: str
    workflow_status: str
    human_review_required: bool
    triage_governance_result: dict[str, Any]
    policy_decision: dict[str, Any]
    policy_context: dict[str, Any]
    policy_governance_result: dict[str, Any]
    risk_flags: Annotated[list[dict[str, Any]], operator.add]
    llm_input_tokens: Annotated[int, operator.add]
    llm_output_tokens: Annotated[int, operator.add]
    llm_usage_events: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[dict[str, Any]], operator.add]
    audit_trail: Annotated[list[dict[str, Any]], operator.add]
    snapshots: Annotated[list[dict[str, Any]], operator.add]


@dataclass(frozen=True)
class PolicyStateNodes:
    policy_reasoning: "PolicyReasoningStateNode"
    policy_governance: "PolicyGovernanceStateNode"


class PolicyReasoningStateNode:
    """Adapt the parent AppState into the existing Azure policy reasoner."""

    def __init__(self, node: PolicyReasoningNode) -> None:
        self.node = node

    def __call__(self, state: PolicyAppState) -> dict[str, Any]:
        policy_input = policy_input_from_state(state)
        result = self.node({"policy_input": policy_input})
        return policy_state_patch(
            PolicyReasoningResult.model_validate(result["policy_result"]),
            PrecedentContext.model_validate(result["precedent_context"]),
            TokenUsage.model_validate(result["policy_usage"]),
        )


class PolicyGovernanceStateNode:
    """Adapt validated policy state into the existing Azure governance reviewer."""

    def __init__(self, node: GovernanceNode) -> None:
        self.node = node

    def __call__(self, state: PolicyAppState) -> dict[str, Any]:
        policy_input, policy_result = policy_result_from_state(state)
        result = self.node(
            {
                "policy_input": policy_input,
                "policy_result": policy_result,
            }
        )
        return governance_state_patch(
            GovernanceAssessment.model_validate(result["governance_assessment"]),
            TokenUsage.model_validate(result["governance_usage"]),
        )


def build_policy_state_nodes(client: AzureJsonClient | None = None) -> PolicyStateNodes:
    azure = client or AzureJsonClient.from_env()
    return PolicyStateNodes(
        policy_reasoning=PolicyReasoningStateNode(PolicyReasoningNode(azure)),
        policy_governance=PolicyGovernanceStateNode(GovernanceNode(azure)),
    )


def policy_input_from_state(state: PolicyAppState) -> PolicyAgentInput:
    trace_id = _required_state_id(state, "trace_id")
    ticket_id = _required_state_id(state, "ticket_id")
    triage_output = state.get("triage_output")
    if not isinstance(triage_output, dict):
        raise ValueError("triage_output must be a JSON object")

    policy_input = exact_policy_input(triage_output)
    if policy_input.case.trace_id != trace_id:
        raise ValueError("state trace_id must match triage_output.case.trace_id")
    if policy_input.case.ticket_id != ticket_id:
        raise ValueError("state ticket_id must match triage_output.case.ticket_id")
    return policy_input


def policy_state_patch(
    result: PolicyReasoningResult,
    precedents: PrecedentContext,
    usage: TokenUsage,
) -> dict[str, Any]:
    decision = PolicyDecisionState(
        decision=result.decision.type,
        refund_amount=result.decision.refund_amount,
        confidence=result.decision.confidence,
        confidence_level=result.decision.confidence_level,
        confidence_evidence=result.decision.confidence_evidence,
        precedent_evidence=result.decision.precedent_evidence,
        reason=result.decision.reason,
    )
    context = PolicyContextState(
        policy_version_used=result.case.policy_version_used,
        policy_evaluation=result.policy_evaluation,
        response_guidance=result.response_guidance,
        evidence_manifest=result.evidence_manifest,
        precedent_context=precedents,
    )
    usage_event = LlmUsageEvent(
        agent="policy_agent",
        stage="policy_reasoning",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
    return {
        "current_stage": "policy",
        "policy_decision": decision.model_dump(mode="json"),
        "policy_context": context.model_dump(mode="json"),
        "llm_input_tokens": usage.input_tokens,
        "llm_output_tokens": usage.output_tokens,
        "llm_usage_events": [usage_event.model_dump(mode="json")],
    }


def governance_state_patch(
    assessment: GovernanceAssessment,
    usage: TokenUsage,
) -> dict[str, Any]:
    status = "block" if assessment.governance.interceptor_action == "quarantine" else "allow"
    governance = PolicyGovernanceResultState(
        stage="policy",
        status=status,
        semantic_drift_score=assessment.governance.semantic_drift_score,
        flags=assessment.governance.flags,
        findings=assessment.findings,
    )
    risk_flags = [
        RiskFindingState(
            stage="policy",
            flag=finding.flag,
            score=finding.score,
            detail=finding.detail,
            offending_content=finding.offending_content,
        ).model_dump(mode="json")
        for finding in assessment.findings
    ]
    usage_event = LlmUsageEvent(
        agent="policy_agent",
        stage="policy_governance",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
    patch: dict[str, Any] = {
        "current_stage": "policy_governance",
        "policy_governance_result": governance.model_dump(mode="json"),
        "risk_flags": risk_flags,
        "llm_input_tokens": usage.input_tokens,
        "llm_output_tokens": usage.output_tokens,
        "llm_usage_events": [usage_event.model_dump(mode="json")],
    }
    if status == "block":
        patch["human_review_required"] = True
        patch["workflow_status"] = "waiting_human"
    return patch


def policy_result_from_state(
    state: PolicyAppState,
) -> tuple[PolicyAgentInput, PolicyReasoningResult]:
    policy_input = policy_input_from_state(state)
    decision = PolicyDecisionState.model_validate(state.get("policy_decision"))
    context = PolicyContextState.model_validate(state.get("policy_context"))
    result = PolicyReasoningResult(
        case={
            "trace_id": policy_input.case.trace_id,
            "ticket_id": policy_input.case.ticket_id,
            "policy_version_used": context.policy_version_used,
        },
        customer_request=policy_input.customer_request,
        policy_evaluation=context.policy_evaluation,
        decision=PolicyDecision(
            type=decision.decision,
            refund_amount=decision.refund_amount,
            confidence=decision.confidence,
            confidence_level=decision.confidence_level,
            confidence_evidence=decision.confidence_evidence,
            precedent_evidence=decision.precedent_evidence,
            reason=decision.reason,
        ),
        response_guidance=context.response_guidance,
        evidence_manifest=context.evidence_manifest,
    )
    policy_context = load_policy_context(policy_input.case.policy_version)
    validate_policy_result(
        result,
        policy_input,
        policy_context,
        context.precedent_context,
    )
    return policy_input, result


def route_policy_state(state: PolicyAppState) -> RouteName:
    decision = PolicyDecisionState.model_validate(state.get("policy_decision"))
    governance = PolicyGovernanceResultState.model_validate(
        state.get("policy_governance_result")
    )
    return route_policy(decision.decision, governance.status)


def policy_output_from_state(state: PolicyAppState) -> PolicyAgentOutput:
    _policy_input, policy_result = policy_result_from_state(state)
    governance = PolicyGovernanceResultState.model_validate(
        state.get("policy_governance_result")
    )
    assessment = GovernanceAssessment(
        governance=Governance(
            semantic_drift_score=governance.semantic_drift_score,
            interceptor_action="quarantine" if governance.status == "block" else "allow",
            flags=governance.flags,
        ),
        findings=governance.findings,
    )
    return assemble_policy_output(policy_result, assessment)


def policy_usage_from_state(state: PolicyAppState) -> TokenUsage:
    events = [
        LlmUsageEvent.model_validate(event)
        for event in state.get("llm_usage_events", [])
        if isinstance(event, dict) and event.get("agent") == "policy_agent"
    ]
    stages = [event.stage for event in events]
    expected = ["policy_reasoning", "policy_governance"]
    if sorted(stages) != sorted(expected):
        raise ValueError(
            "Policy Agent usage requires exactly one policy_reasoning and one policy_governance event"
        )
    usage = TokenUsage(input_tokens=0, output_tokens=0)
    for event in events:
        usage = usage.add(
            TokenUsage(
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
            )
        )
    return usage


def assemble_policy_output(
    policy_result: PolicyReasoningResult,
    assessment: GovernanceAssessment,
) -> PolicyAgentOutput:
    governance_status = (
        "block" if assessment.governance.interceptor_action == "quarantine" else "allow"
    )
    route = route_policy(policy_result.decision.type, governance_status)
    output = PolicyAgentOutput(
        case=policy_result.case,
        customer_request=policy_result.customer_request,
        policy_evaluation=policy_result.policy_evaluation,
        decision=policy_result.decision,
        response_guidance=policy_result.response_guidance,
        handoff=Handoff(
            next_agent=route,
            reason=handoff_reason(policy_result.decision.type, governance_status),
        ),
        governance=assessment.governance,
    )
    if (
        output.case != policy_result.case
        or output.customer_request != policy_result.customer_request
        or output.policy_evaluation != policy_result.policy_evaluation
        or output.decision != policy_result.decision
        or output.response_guidance != policy_result.response_guidance
    ):
        raise ValueError("output assembly must preserve the complete policy reasoning result")
    return output


def _required_state_id(state: PolicyAppState, name: Literal["trace_id", "ticket_id"]) -> str:
    value = state.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"state {name} must be a non-empty string")
    return value
