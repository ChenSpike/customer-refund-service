from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


InterceptorAction = Literal["allow", "quarantine"]
GovernanceFlag = Literal["semantic_drift", "forbidden_tool", "pii_risk"]


class GovernanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Governance(GovernanceModel):
    semantic_drift_score: float = Field(ge=0, le=1)
    interceptor_action: InterceptorAction
    flags: list[GovernanceFlag] = Field(default_factory=list)


class GovernanceFinding(GovernanceModel):
    flag: GovernanceFlag
    score: float | None = Field(default=None, ge=0, le=1)
    detail: str
    offending_content: str | None = None


class GovernanceAssessment(GovernanceModel):
    """Shared internal governance result with detailed findings."""

    governance: Governance
    findings: list[GovernanceFinding] = Field(default_factory=list)

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
