from typing import Any, Protocol

from governance.models import GovernanceAssessment, GovernanceCheckResult, GovernanceFinding, GovernanceStatement


class DeterministicGovernanceChecker(Protocol):
    def __call__(self, state: dict[str, Any]) -> GovernanceCheckResult:
        """Run a deterministic governance check against the current state."""


class LlmGovernanceReviewer(Protocol):
    def __call__(self, state: dict[str, Any]) -> Any:
        """Run an LLM-backed governance review against the current state."""


def build_check_result_payload(stage: str, findings: list[GovernanceCheckResult]) -> dict[str, Any]:
    blocked = [item for item in findings if item.status == "block"]
    return {
        "stage": stage,
        "status": "block" if blocked else "allow",
        "findings": [item.model_dump(mode="json") for item in blocked],
        "all_checks": [item.model_dump(mode="json") for item in findings],
    }


def build_statement_from_check_results(
    *,
    trace_id: str,
    agent: str,
    stage: str,
    findings: list[GovernanceCheckResult],
) -> GovernanceStatement:
    blocked = [item for item in findings if item.status == "block"]
    status = "block" if blocked else "allow"
    return GovernanceStatement(
        trace_id=trace_id,
        agent=agent,
        stage=stage,
        status=status,
        summary=_statement_summary(stage, status, blocked),
        findings=[_finding_from_check(item) for item in blocked],
    )


def build_statement_from_assessment(
    *,
    trace_id: str,
    agent: str,
    stage: str,
    assessment: GovernanceAssessment,
) -> GovernanceStatement:
    status = "block" if assessment.findings else "allow"
    return GovernanceStatement(
        trace_id=trace_id,
        agent=agent,
        stage=stage,
        status=status,
        summary=_assessment_summary(stage, assessment),
        findings=assessment.findings,
    )


def merge_assessment_with_check_results(
    assessment: GovernanceAssessment,
    check_results: list[GovernanceCheckResult],
) -> GovernanceAssessment:
    deterministic = {
        item.name: _finding_from_check(item)
        for item in check_results
        if item.status == "block"
    }
    merged = {finding.flag: finding for finding in assessment.findings}
    merged.update(deterministic)
    flag_order = ("semantic_drift", "forbidden_tool", "pii_risk")
    findings = [merged[flag] for flag in flag_order if flag in merged]
    return GovernanceAssessment.model_validate(
        {
            "governance": {
                "semantic_drift_score": assessment.governance.semantic_drift_score,
                "interceptor_action": "quarantine" if findings else "allow",
                "flags": [finding.flag for finding in findings],
            },
            "findings": [finding.model_dump(mode="json") for finding in findings],
        }
    )


def _findings_from_checks(check_results: list[GovernanceCheckResult]) -> list[GovernanceFinding]:
    return [_finding_from_check(item) for item in check_results]


def _finding_from_check(item: GovernanceCheckResult) -> GovernanceFinding:
    return GovernanceFinding(
        flag=item.name,
        score=_evidence_score(item.evidence),
        detail=item.detail or item.name,
        offending_content=_offending_content(item.evidence),
        source=item.source,
    )


def _evidence_score(evidence: dict[str, object]) -> float | None:
    score = evidence.get("score")
    return float(score) if isinstance(score, (int, float)) else None


def _offending_content(evidence: dict[str, object]) -> str | None:
    for key in ("offending_content", "pattern"):
        value = evidence.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _statement_summary(stage: str, status: str, blocked: list[GovernanceCheckResult]) -> str:
    if status == "allow":
        return f"{stage} governance passed"
    flags = ", ".join(item.name for item in blocked)
    return f"{stage} governance blocked: {flags}"


def _assessment_summary(stage: str, assessment: GovernanceAssessment) -> str:
    if not assessment.findings:
        return f"{stage} governance passed"
    flags = ", ".join(finding.flag for finding in assessment.findings)
    return f"{stage} governance blocked: {flags}"
