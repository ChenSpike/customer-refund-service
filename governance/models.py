from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


InterceptorAction = Literal["allow", "quarantine"]
GovernanceFlag = Literal["semantic_drift", "forbidden_tool", "pii_risk"]
GovernanceSource = Literal["deterministic", "llm"]
OWASP_GOVERNANCE_FLAGS = {"semantic_drift", "forbidden_tool", "pii_risk"}


class GovernanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Governance(GovernanceModel):
    semantic_drift_score: float = Field(ge=0, le=1)
    interceptor_action: InterceptorAction
    flags: list[GovernanceFlag] = Field(default_factory=list)


class GovernanceFinding(GovernanceModel):
    flag: str = Field(min_length=1)
    score: float | None = Field(default=None, ge=0, le=1)
    detail: str
    offending_content: str | None = None
    source: GovernanceSource = "llm"


class GovernanceCheckResult(GovernanceModel):
    name: str = Field(min_length=1)
    status: Literal["allow", "block"]
    detail: str = ""
    evidence: dict[str, object] = Field(default_factory=dict)
    source: GovernanceSource

    @model_validator(mode="after")
    def validate_status(self) -> "GovernanceCheckResult":
        if self.status == "allow" and self.detail:
            raise ValueError("allow check results cannot include detail")
        if self.status == "block" and not self.detail:
            raise ValueError("block check results must include detail")
        return self


class GovernanceAssessment(GovernanceModel):
    """Shared internal governance result with detailed findings."""

    governance: Governance
    findings: list[GovernanceFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_findings(self) -> "GovernanceAssessment":
        finding_flags = [finding.flag for finding in self.findings]
        if len(finding_flags) != len(set(finding_flags)):
            raise ValueError("governance findings must use unique flags")
        if any(flag not in OWASP_GOVERNANCE_FLAGS for flag in finding_flags):
            raise ValueError("governance assessment findings must use OWASP flags only")
        if self.governance.flags != finding_flags:
            raise ValueError("governance flags must match findings in the same order")
        if finding_flags and self.governance.interceptor_action == "allow":
            raise ValueError("OWASP findings cannot use interceptor_action=allow")
        if not finding_flags and self.governance.interceptor_action != "allow":
            raise ValueError("quarantine requires at least one OWASP finding")
        return self


class GovernanceStatement(GovernanceModel):
    """Shared persistence payload written by governance-capable subgraphs."""

    trace_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    status: Literal["allow", "block"]
    summary: str = Field(min_length=1)
    findings: list[GovernanceFinding] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_status(self) -> "GovernanceStatement":
        if self.status == "allow" and self.findings:
            raise ValueError("allow statements cannot include governance findings")
        if self.status == "block" and not self.findings:
            raise ValueError("block statements must include at least one governance finding")
        return self
