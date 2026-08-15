from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.policy.azure import AzureJsonResult
from agents.policy.governance_node import AzurePolicyGovernanceReviewer, GovernanceNode
from agents.policy.models import PolicyReasoningResult, TokenUsage
from agents.policy.tests.factories import (
    allow_governance,
    make_input,
    make_policy_result,
    quarantine_governance,
    unavailable_context,
)
from governance import Governance, GovernanceAssessment, GovernanceFinding


class FakeAzureClient:
    def __init__(self, governance) -> None:
        self.governance = governance
        self.calls: list[dict[str, object]] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        kwargs["validate"](self.governance)
        return AzureJsonResult(
            value=self.governance,
            usage=TokenUsage(input_tokens=11, output_tokens=3),
        )


def test_azure_policy_governance_reviewer_returns_assessment_and_usage() -> None:
    client = FakeAzureClient(allow_governance())
    policy_input = make_input()

    result = AzurePolicyGovernanceReviewer(client)(
        {
            "policy_input": policy_input,
            "policy_result": make_policy_result(policy_input),
        }
    )

    assert result.value.governance.interceptor_action == "allow"
    assert result.usage.input_tokens == 11
    assert client.calls[0]["target"] == "governance assessment"


def test_azure_governance_rejects_required_case_metadata_as_pii() -> None:
    policy_input = make_input()
    assessment = GovernanceAssessment(
        governance=Governance(
            semantic_drift_score=0.0,
            interceptor_action="quarantine",
            flags=["pii_risk"],
        ),
        findings=[
            GovernanceFinding(
                flag="pii_risk",
                detail="Required routing metadata was present.",
                offending_content="TRACE-UNIT TICKET-UNIT",
                source="llm",
            )
        ],
    )

    with pytest.raises(ValueError, match="not present outside case metadata"):
        AzurePolicyGovernanceReviewer(FakeAzureClient(assessment))(
            {
                "policy_input": policy_input,
                "policy_result": make_policy_result(policy_input),
            }
        )


def test_azure_governance_requires_llm_source() -> None:
    policy_input = make_input()
    assessment = quarantine_governance()
    assessment.findings[0].source = "deterministic"

    with pytest.raises(ValueError, match="must use source=llm"):
        AzurePolicyGovernanceReviewer(FakeAzureClient(assessment))(
            {
                "policy_input": policy_input,
                "policy_result": make_policy_result(policy_input),
            }
        )


def test_azure_governance_accepts_customer_pii_evidence() -> None:
    policy_input = make_input()
    policy_input.customer_request.sanitized_text = (
        "Another customer email other.customer@example.com appeared in my account."
    )
    assessment = GovernanceAssessment(
        governance=Governance(
            semantic_drift_score=0.0,
            interceptor_action="quarantine",
            flags=["pii_risk"],
        ),
        findings=[
            GovernanceFinding(
                flag="pii_risk",
                detail="Another customer's email is visible.",
                offending_content="other.customer@example.com",
                source="llm",
            )
        ],
    )

    result = AzurePolicyGovernanceReviewer(FakeAzureClient(assessment))(
        {
            "policy_input": policy_input,
            "policy_result": make_policy_result(policy_input),
        }
    )

    assert result.value.findings[0].flag == "pii_risk"


def test_azure_governance_rejects_schema_vocabulary_as_pii() -> None:
    policy_input = make_input()
    policy_result = make_policy_result(policy_input)
    safe_internal_metadata = json.dumps(
        policy_result.decision.precedent_evidence.model_dump(mode="json"),
        ensure_ascii=False,
    )
    assessment = GovernanceAssessment(
        governance=Governance(
            semantic_drift_score=0.0,
            interceptor_action="quarantine",
            flags=["pii_risk"],
        ),
        findings=[
            GovernanceFinding(
                flag="pii_risk",
                detail="Internal workflow metadata was exposed.",
                offending_content=safe_internal_metadata,
                source="llm",
            )
        ],
    )

    with pytest.raises(ValueError, match="pii_risk must quote an actual"):
        AzurePolicyGovernanceReviewer(FakeAzureClient(assessment))(
            {
                "policy_input": policy_input,
                "policy_result": policy_result,
            }
        )


def test_policy_governance_allows_when_no_owasp_finding_exists() -> None:
    patch = GovernanceNode(
        reviewer=lambda _state: AzureJsonResult(
            value=allow_governance(),
            usage=TokenUsage(input_tokens=7, output_tokens=2),
        )
    )(_state())

    assert patch["policy_governance_result"]["status"] == "allow"
    assert patch["policy_governance_result"]["findings"] == []
    assert "governance_event_id" not in patch
    assert "governance_assessment" not in patch


def test_deterministic_tool_misuse_is_a_genuine_owasp_block() -> None:
    state = _state(reason="I already issued the refund and checked the database directly.")
    patch = GovernanceNode(
        reviewer=lambda _state: AzureJsonResult(
            value=allow_governance(),
            usage=TokenUsage(input_tokens=5, output_tokens=1),
        )
    )(state)

    finding = patch["policy_governance_result"]["findings"][0]
    assert patch["policy_governance_result"]["status"] == "block"
    assert finding["flag"] == "forbidden_tool"
    assert finding["source"] == "deterministic"
    assert finding["offending_content"] == "i already issued the refund"


def test_explicit_empty_checker_set_is_respected() -> None:
    state = _state(reason="I already issued the refund and checked the database directly.")
    patch = GovernanceNode(
        reviewer=lambda _state: AzureJsonResult(
            value=allow_governance(),
            usage=TokenUsage(input_tokens=5, output_tokens=1),
        ),
        checkers=(),
    )(state)

    assert patch["policy_governance_result"]["status"] == "allow"
    assert patch["policy_governance_result"]["findings"] == []


def test_deterministic_finding_overrides_duplicate_llm_finding() -> None:
    llm = quarantine_governance("forbidden_tool")
    state = _state(reason="The refund has been processed.")
    patch = GovernanceNode(
        reviewer=lambda _state: AzureJsonResult(
            value=llm,
            usage=TokenUsage(input_tokens=5, output_tokens=1),
        )
    )(state)

    findings = patch["policy_governance_result"]["findings"]
    assert len(findings) == 1
    assert findings[0]["source"] == "deterministic"


def test_merged_findings_use_consistent_owasp_order() -> None:
    llm = GovernanceAssessment(
        governance=Governance(
            semantic_drift_score=0.9,
            interceptor_action="quarantine",
            flags=["pii_risk", "semantic_drift"],
        ),
        findings=[
            GovernanceFinding(flag="pii_risk", detail="PII leak", source="llm"),
            GovernanceFinding(flag="semantic_drift", detail="Prompt injection", source="llm"),
        ],
    )
    state = _state(reason="Tool executed the refund.")
    patch = GovernanceNode(
        reviewer=lambda _state: AzureJsonResult(
            value=llm,
            usage=TokenUsage(input_tokens=5, output_tokens=1),
        )
    )(state)

    assert patch["policy_governance_result"]["flags"] == [
        "semantic_drift",
        "forbidden_tool",
        "pii_risk",
    ]


def test_invalid_reviewer_value_fails_without_governance_patch() -> None:
    node = GovernanceNode(
        reviewer=lambda _state: SimpleNamespace(
            value={"not": "validated"},
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )
    )

    with pytest.raises((AttributeError, TypeError)):
        node(_state())


def _state(*, reason: str | None = None) -> dict:
    policy_input = make_input()
    policy_result = make_policy_result(policy_input)
    if reason is not None:
        policy_result.decision.reason = reason
    precedents = unavailable_context()
    payload = policy_input.model_dump(mode="json")
    payload["case"]["goal"] = "evaluate refund eligibility"
    return {
        "trace_id": policy_input.case.trace_id,
        "ticket_id": policy_input.case.ticket_id,
        "triage_output": payload,
        "policy_result": policy_result.model_dump(mode="json"),
        "policy_decision": {
            "decision": policy_result.decision.type,
            "refund_amount": policy_result.decision.refund_amount,
            "confidence": policy_result.decision.confidence,
            "confidence_level": policy_result.decision.confidence_level,
            "confidence_evidence": policy_result.decision.confidence_evidence.model_dump(mode="json"),
            "precedent_evidence": policy_result.decision.precedent_evidence.model_dump(mode="json"),
            "reason": policy_result.decision.reason,
        },
        "policy_context": {
            "policy_version_used": policy_result.case.policy_version_used,
            "policy_evaluation": policy_result.policy_evaluation.model_dump(mode="json"),
            "response_guidance": policy_result.response_guidance.model_dump(mode="json"),
            "evidence_manifest": policy_result.evidence_manifest.model_dump(mode="json"),
            "precedent_context": precedents.model_dump(mode="json"),
        },
        "risk_flags": [],
        "llm_usage_events": [],
    }
