from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PolicyEffect = Literal["supports_approval", "supports_denial", "supports_partial", "requires_review"]
GapType = Literal["missing_fact", "policy_conflict", "low_confidence"]
DecisionType = Literal["approve", "deny", "partial_refund", "request_info", "manual_review"]
ActionableDecision = Literal["approve", "deny", "partial_refund"]
NextAgent = Literal["refund_agent", "response_agent", "human_approval", "triage_agent"]
InterceptorAction = Literal["allow", "quarantine"]
GovernanceFlag = Literal["semantic_drift", "forbidden_tool", "pii_risk"]
EvidenceAssessment = Literal["supports", "conflicts"]
ConfidenceLevel = Literal["high", "moderate", "low", "insufficient"]
PolicySupport = Literal["clear", "minor_ambiguity", "weak", "none"]
PrecedentAssessment = Literal[
    "supportive",
    "mixed",
    "strongly_disagrees",
    "none_relevant",
    "unavailable",
]
PrecedentStatus = Literal["available", "insufficient", "unavailable"]
HumanOutcome = Literal["approved", "rejected", "partially_approved"]
MemoryStatus = Literal["loaded", "empty", "missing", "malformed", "version_mismatch", "provider_error"]


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

    @model_validator(mode="after")
    def validate_unique_policies(self) -> "PolicyEvaluation":
        policy_ids = [policy.policy_id for policy in self.matched_policies]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("matched policy IDs must be unique")
        return self


class PolicyEvidenceItem(StrictModel):
    evidence_id: str
    policy_id: str
    fact_path: str
    assessment: EvidenceAssessment
    explanation: str


class PolicyPrecedentMatch(StrictModel):
    precedent_id: str
    similarity: float = Field(ge=0, le=1)
    explanation: str


class PolicyEvidenceManifest(StrictModel):
    applicable_rule_ids: list[str] = Field(default_factory=list)
    supporting_rule_ids: list[str] = Field(default_factory=list)
    required_fact_paths: list[str] = Field(default_factory=list)
    evidence_items: list[PolicyEvidenceItem] = Field(default_factory=list)
    precedent_matches: list[PolicyPrecedentMatch] = Field(default_factory=list)
    precedent_comparison_decision: ActionableDecision | None = Field(
        description=(
            "Actionable candidate used only to compare precedents. It is required and must equal the final "
            "decision for approve, deny, or partial_refund, even when precedent memory is unavailable."
        )
    )

    @model_validator(mode="after")
    def validate_identifiers(self) -> "PolicyEvidenceManifest":
        for label, values in (
            ("applicable rule IDs", self.applicable_rule_ids),
            ("supporting rule IDs", self.supporting_rule_ids),
            ("required fact paths", self.required_fact_paths),
            ("evidence IDs", [item.evidence_id for item in self.evidence_items]),
            ("precedent IDs", [item.precedent_id for item in self.precedent_matches]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if not set(self.supporting_rule_ids).issubset(self.applicable_rule_ids):
            raise ValueError("supporting rule IDs must be a subset of applicable rule IDs")
        return self


class ConfidenceEvidence(StrictModel):
    facts_complete: bool
    essential_fact_paths_missing: list[str] = Field(default_factory=list)
    policy_support: PolicySupport
    minor_ambiguities: list[str] = Field(default_factory=list)
    important_conflicts: list[str] = Field(default_factory=list)
    explanation: str

    @model_validator(mode="after")
    def validate_evidence(self) -> "ConfidenceEvidence":
        for label, values in (
            ("essential missing fact paths", self.essential_fact_paths_missing),
            ("minor ambiguities", self.minor_ambiguities),
            ("important conflicts", self.important_conflicts),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if self.facts_complete == bool(self.essential_fact_paths_missing):
            raise ValueError("facts_complete must be true exactly when no essential fact paths are missing")
        if self.policy_support == "minor_ambiguity" and not self.minor_ambiguities:
            raise ValueError("minor_ambiguity policy support requires a stated ambiguity")
        if not self.explanation.strip():
            raise ValueError("confidence evidence explanation cannot be blank")
        return self


class SimilarityRange(StrictModel):
    minimum: float = Field(ge=0, le=1)
    maximum: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_order(self) -> "SimilarityRange":
        if self.minimum > self.maximum:
            raise ValueError("similarity minimum cannot exceed maximum")
        return self


class PrecedentEvidence(StrictModel):
    available: bool
    status: PrecedentStatus
    memory_status: MemoryStatus
    assessment: PrecedentAssessment
    support_count: int = Field(ge=0)
    contradiction_count: int = Field(ge=0)
    similarity_range: SimilarityRange | None
    referenced_precedent_ids: list[str] = Field(default_factory=list)
    explanation: str

    @model_validator(mode="after")
    def validate_availability(self) -> "PrecedentEvidence":
        if len(self.referenced_precedent_ids) != len(set(self.referenced_precedent_ids)):
            raise ValueError("referenced precedent IDs must be unique")
        if self.status == "available":
            if not self.available or not self.referenced_precedent_ids:
                raise ValueError("available precedent evidence requires at least one relevant precedent")
            if self.memory_status != "loaded":
                raise ValueError("available precedent evidence requires loaded memory")
            if self.similarity_range is None:
                raise ValueError("available precedent evidence requires a similarity range")
            if self.assessment not in {"supportive", "mixed", "strongly_disagrees"}:
                raise ValueError("available precedent evidence requires a comparison assessment")
        elif self.available:
            raise ValueError("unavailable or insufficient precedent evidence cannot be marked available")
        if self.status == "unavailable" and (
            self.memory_status == "loaded"
            or self.assessment != "unavailable"
            or self.support_count != 0
            or self.contradiction_count != 0
            or self.similarity_range is not None
            or self.referenced_precedent_ids
        ):
            raise ValueError("unavailable precedent memory cannot reference precedents")
        if self.status == "insufficient" and (
            self.memory_status != "loaded"
            or self.assessment != "none_relevant"
            or self.support_count != 0
            or self.contradiction_count != 0
            or self.similarity_range is not None
            or self.referenced_precedent_ids
        ):
            raise ValueError("insufficient precedent evidence must report no relevant precedents")
        if self.support_count + self.contradiction_count != len(self.referenced_precedent_ids):
            raise ValueError("precedent support and contradiction counts must match referenced precedent IDs")
        if not self.explanation.strip():
            raise ValueError("precedent evidence explanation cannot be blank")
        return self


class PolicyDecision(StrictModel):
    type: DecisionType
    refund_amount: float = Field(ge=0)
    confidence: Literal[0, 1, 2, 3]
    confidence_level: ConfidenceLevel
    confidence_evidence: ConfidenceEvidence
    precedent_evidence: PrecedentEvidence
    reason: str

    @model_validator(mode="after")
    def validate_confidence_level(self) -> "PolicyDecision":
        expected = {3: "high", 2: "moderate", 1: "low", 0: "insufficient"}[self.confidence]
        if self.confidence_level != expected:
            raise ValueError(f"confidence {self.confidence} requires confidence_level={expected}")
        if self.type in {"approve", "deny", "partial_refund"} and self.confidence < 2:
            raise ValueError("actionable decisions require moderate or high confidence")
        if self.confidence == 1 and self.type != "manual_review":
            raise ValueError("low confidence requires manual_review")
        if self.confidence == 0 and self.type not in {"request_info", "manual_review"}:
            raise ValueError("insufficient confidence requires request_info or manual_review")
        if self.type == "request_info" and self.confidence != 0:
            raise ValueError("request_info requires insufficient confidence")
        if self.type in {"deny", "request_info", "manual_review"} and self.refund_amount != 0:
            raise ValueError(f"{self.type} requires refund_amount=0")
        if self.type in {"approve", "partial_refund"} and self.refund_amount <= 0:
            raise ValueError(f"{self.type} requires a positive refund amount")
        return self


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


class GovernanceFinding(StrictModel):
    flag: GovernanceFlag
    score: float | None = Field(default=None, ge=0, le=1)
    detail: str
    offending_content: str | None = None


class PrecedentAttributes(StrictModel):
    refund_reason: str | None = None
    item_status: str | None = None
    product_type: str | None = None
    amount_band: Literal["zero", "low", "medium", "high"] | None = None
    purchase_window: Literal["within_30_days", "outside_30_days", "unknown"] | None = None
    prior_refund_state: Literal["none", "partial", "full_or_more"] | None = None

    @model_validator(mode="after")
    def validate_nonempty(self) -> "PrecedentAttributes":
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("precedent attributes must contain at least one relevant attribute")
        return self


class PrecedentRecord(StrictModel):
    precedent_id: str = Field(pattern=r"^PREC-[A-Za-z0-9][A-Za-z0-9._-]*$")
    policy_version: str
    normalized_case: str = Field(min_length=1, max_length=500)
    relevant_attributes: PrecedentAttributes
    matched_rule_ids: list[str] = Field(min_length=1)
    final_decision: ActionableDecision
    human_outcome: HumanOutcome
    finalized_at: datetime

    @model_validator(mode="after")
    def validate_rule_ids(self) -> "PrecedentRecord":
        if len(self.matched_rule_ids) != len(set(self.matched_rule_ids)):
            raise ValueError("precedent matched rule IDs must be unique")
        return self


class PrecedentMemoryFile(StrictModel):
    schema_version: Literal["1.0"]
    policy_version: str
    generated_at: datetime | None
    derived_guidance: list[str] = Field(default_factory=list)
    precedents: list[PrecedentRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_records(self) -> "PrecedentMemoryFile":
        precedent_ids = [record.precedent_id for record in self.precedents]
        if len(precedent_ids) != len(set(precedent_ids)):
            raise ValueError("precedent IDs must be unique")
        mismatches = [
            record.precedent_id
            for record in self.precedents
            if record.policy_version != self.policy_version
        ]
        if mismatches:
            raise ValueError("precedent policy version mismatch: " + ", ".join(mismatches))
        return self


class PrecedentContext(StrictModel):
    policy_version: str
    available: bool
    status: MemoryStatus
    reason: str
    derived_guidance: list[str] = Field(default_factory=list)
    records: list[PrecedentRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status(self) -> "PrecedentContext":
        if self.available != bool(self.records):
            raise ValueError("precedent context availability must match its records")
        if self.available and self.status != "loaded":
            raise ValueError("available precedent context must have loaded status")
        if not self.available and self.status == "loaded":
            raise ValueError("loaded precedent context must contain records")
        return self

    @property
    def records_by_id(self) -> dict[str, PrecedentRecord]:
        return {record.precedent_id: record for record in self.records}

    @classmethod
    def unavailable(cls, policy_version: str, status: MemoryStatus, reason: str) -> "PrecedentContext":
        return cls(
            policy_version=policy_version,
            available=False,
            status=status,
            reason=reason,
            derived_guidance=[],
            records=[],
        )


class PolicyReasoningResult(StrictModel):
    """Complete Azure policy decision plus internal validation evidence."""

    case: OutputCase
    customer_request: CustomerRequest
    policy_evaluation: PolicyEvaluation
    decision: PolicyDecision
    response_guidance: ResponseGuidance
    evidence_manifest: PolicyEvidenceManifest


class GovernanceAssessment(StrictModel):
    """Internal Azure governance result with detailed OWASP evidence."""

    governance: Governance
    findings: list[GovernanceFinding] = Field(default_factory=list)
    handoff: Handoff

    @model_validator(mode="after")
    def validate_findings(self) -> "GovernanceAssessment":
        finding_flags = [finding.flag for finding in self.findings]
        if len(finding_flags) != len(set(finding_flags)):
            raise ValueError("governance findings must use unique flags")
        if self.governance.flags != finding_flags:
            raise ValueError("governance flags must match findings in the same order")
        if finding_flags and self.governance.interceptor_action == "allow":
            raise ValueError("OWASP findings cannot use interceptor_action=allow")
        if not finding_flags and self.governance.interceptor_action != "allow":
            raise ValueError("quarantine requires at least one OWASP finding")
        return self


class PolicyAgentOutput(StrictModel):
    """Proposal Policy Agent output JSON in exact field order."""

    case: OutputCase
    customer_request: CustomerRequest
    policy_evaluation: PolicyEvaluation
    decision: PolicyDecision
    response_guidance: ResponseGuidance
    handoff: Handoff
    governance: Governance

    @model_validator(mode="after")
    def validate_route(self) -> "PolicyAgentOutput":
        expected_route = (
            "human_approval"
            if self.governance.interceptor_action == "quarantine"
            else {
                "approve": "refund_agent",
                "partial_refund": "refund_agent",
                "deny": "response_agent",
                "request_info": "triage_agent",
                "manual_review": "human_approval",
            }[self.decision.type]
        )
        if self.handoff.next_agent != expected_route:
            raise ValueError(f"decision and governance require route {expected_route}")
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
