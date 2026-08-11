from __future__ import annotations

import json

import pytest

from agents.policy.azure import (
    POLICY_AGENT_DIR,
    REPO_ROOT,
    AzureJsonClient,
    AzureJsonRepair,
    AzurePolicyRepair,
    _Attempt,
    _apply_json_repair,
    _strict_json_format,
)
from agents.policy.models import (
    PolicyEvidenceItem,
    PolicyGapOrConflict,
    PolicyReasoningResult,
    TokenUsage,
)
from agents.policy.policy_node import (
    _policy_input_message,
    load_policy_context,
    validate_policy_result,
)
from agents.policy.tests.factories import (
    make_input,
    make_match,
    make_policy,
    make_policy_result,
    make_precedent_context,
    unavailable_context,
)


class RepeatingAzureClient(AzureJsonClient):
    def __init__(self, content: str, repair_content: str | None = None) -> None:
        self.content = content
        self.repair_content = repair_content or json.dumps(
            {
                "confidence_correction": {
                    "confidence": 3,
                    "confidence_level": "high",
                    "confidence_evidence": json.loads(content)["decision"]["confidence_evidence"],
                    "precedent_evidence": json.loads(content)["decision"]["precedent_evidence"],
                },
                "replacements": [
                    {
                        "path": "/decision/reason",
                        "value_json": json.dumps("Azure repair left the invalid reference unchanged."),
                    }
                ],
            }
        )
        self.request_count = 0
        self.instructions: list[str] = []
        self.inputs: list[str] = []

    def _request(
        self,
        instructions: str,
        input_text: str,
        *,
        model_type,
        repair: bool = False,
    ) -> _Attempt:
        self.request_count += 1
        self.instructions.append(instructions)
        self.inputs.append(input_text)
        return _Attempt(
            content=self.repair_content if repair else self.content,
            usage=TokenUsage(input_tokens=5, output_tokens=2),
        )


def test_azure_environment_root_is_the_repository_root() -> None:
    assert REPO_ROOT == POLICY_AGENT_DIR.parents[1]
    assert (REPO_ROOT / "agents" / "policy") == POLICY_AGENT_DIR


def _validate(result, policy_input, precedents=None) -> None:
    validate_policy_result(
        result,
        policy_input,
        load_policy_context("v1.0"),
        precedents or unavailable_context(),
    )


def test_unknown_precedent_id_uses_repair_then_fails_validation() -> None:
    policy_input = make_input()
    context = make_precedent_context(["approve"])
    result = make_policy_result(
        policy_input,
        precedent_context=context,
        precedent_matches=[make_match(99)],
    )
    client = RepeatingAzureClient(result.model_dump_json())

    with pytest.raises(RuntimeError, match="after repair"):
        client.generate(
            target="policy reasoning result",
            instructions="test",
            input_text="test",
            model_type=PolicyReasoningResult,
            validate=lambda value: _validate(value, policy_input, context),
        )

    assert client.request_count == 2
    repair_payload = json.loads(client.inputs[1])
    assert repair_payload["original_instructions"] == "test"
    assert repair_payload["authoritative_confidence"]["expected_confidence"] == 3


def test_unknown_rule_and_invalid_fact_path_fail_validation() -> None:
    policy_input = make_input()

    unknown_rule = make_policy_result(policy_input)
    unknown_rule.policy_evaluation.matched_policies[0].policy_id = "R-INVENTED"
    unknown_rule.evidence_manifest.applicable_rule_ids[0] = "R-INVENTED"
    unknown_rule.evidence_manifest.supporting_rule_ids[0] = "R-INVENTED"
    unknown_rule.evidence_manifest.evidence_items[0].policy_id = "R-INVENTED"
    with pytest.raises(ValueError, match="unknown refund policy IDs"):
        _validate(unknown_rule, policy_input)

    invalid_path = make_policy_result(policy_input)
    invalid_path.evidence_manifest.required_fact_paths[0] = "case.trace_id"
    invalid_path.evidence_manifest.evidence_items[0].fact_path = "case.trace_id"
    with pytest.raises(ValueError, match="invalid Proposal fact paths"):
        _validate(invalid_path, policy_input)


def test_applicable_nondecisive_rule_may_have_supporting_evidence() -> None:
    policy_input = make_input()
    policies = [
        make_policy("R-APPROVE-DAMAGED-30D", "supports_approval"),
        make_policy("R-DENY-DUPLICATE", "supports_denial"),
    ]
    result = make_policy_result(
        policy_input,
        decision_type="deny",
        policies=policies,
        supporting_rule_ids=["R-DENY-DUPLICATE"],
        required_fact_paths=[
            "customer_request.refund_reason",
            "order_facts.prior_refund_total",
        ],
        evidence_items=[
            PolicyEvidenceItem(
                evidence_id="E-APPROVAL",
                policy_id="R-APPROVE-DAMAGED-30D",
                fact_path="customer_request.refund_reason",
                assessment="supports",
                explanation="The reason supports an otherwise applicable approval rule.",
            ),
            PolicyEvidenceItem(
                evidence_id="E-DENIAL",
                policy_id="R-DENY-DUPLICATE",
                fact_path="order_facts.prior_refund_total",
                assessment="supports",
                explanation="The prior refund supports the precedence denial.",
            ),
        ],
        comparison_decision="deny",
    )

    _validate(result, policy_input)


def test_required_facts_and_classified_evidence_are_independent_sets() -> None:
    policy_input = make_input()
    result = make_policy_result(
        policy_input,
        required_fact_paths=[
            "customer_request.refund_reason",
            "customer_request.requested_amount",
            "order_facts.amount_paid",
        ],
        evidence_items=[
            PolicyEvidenceItem(
                evidence_id="E-1",
                policy_id="R-APPROVE-DAMAGED-30D",
                fact_path="customer_request.refund_reason",
                assessment="supports",
                explanation="The damaged reason supports the approval rule.",
            )
        ],
    )

    _validate(result, policy_input)


def test_sanitized_customer_narrative_is_an_allowed_policy_fact() -> None:
    policy_input = make_input()
    result = make_policy_result(
        policy_input,
        required_fact_paths=["customer_request.sanitized_text"],
    )

    _validate(result, policy_input)


def test_python_rejects_discrete_confidence_mismatch_without_rewriting() -> None:
    policy_input = make_input()
    result = make_policy_result(policy_input)
    result.decision.confidence = 2
    result.decision.confidence_level = "moderate"

    with pytest.raises(ValueError, match="confidence must be discrete score 3"):
        _validate(result, policy_input)
    assert result.decision.confidence == 2


def test_confidence_evidence_must_match_structural_fact_presence() -> None:
    policy_input = make_input(refund_reason=None)
    result = make_policy_result(
        policy_input,
        decision_type="request_info",
        policies=[make_policy("R-REQUEST-MISSING-FACTS", "requires_review")],
        required_fact_paths=["customer_request.refund_reason"],
        comparison_decision=None,
    )
    result.decision.confidence_evidence.essential_fact_paths_missing = []
    result.decision.confidence_evidence.facts_complete = True

    with pytest.raises(ValueError, match="essential_fact_paths_missing"):
        _validate(result, policy_input)


def test_missing_fact_rule_accepts_conflict_evidence_and_human_readable_path_guidance() -> None:
    policy_input = make_input(refund_reason=None)
    result = make_policy_result(
        policy_input,
        decision_type="request_info",
        policies=[make_policy("R-REQUEST-MISSING-FACTS", "requires_review")],
        required_fact_paths=["customer_request.refund_reason"],
        comparison_decision=None,
    )
    result.evidence_manifest.evidence_items[0].assessment = "conflicts"
    result.response_guidance.missing_info_to_request = [
        "Please provide customer_request.refund_reason so the refund policy can be evaluated."
    ]

    _validate(result, policy_input)


def test_refund_amount_cannot_exceed_requested_or_unpaid_value() -> None:
    policy_input = make_input(requested_amount=100.0)
    result = make_policy_result(policy_input)
    result.decision.refund_amount = 101.0

    with pytest.raises(ValueError, match="cannot exceed"):
        _validate(result, policy_input)


def test_missing_fact_gap_cannot_be_invented_for_present_facts() -> None:
    policy_input = make_input()
    result = make_policy_result(policy_input)
    result.policy_evaluation.gaps_or_conflicts.append(
        PolicyGapOrConflict(type="missing_fact", detail="A required fact is allegedly missing.")
    )

    with pytest.raises(ValueError, match="actually missing required fact"):
        _validate(result, policy_input)


def test_azure_input_includes_authoritative_structural_fact_presence() -> None:
    policy_input = make_input(refund_reason=None, requested_amount=0.0)

    payload = json.loads(_policy_input_message(policy_input, unavailable_context()).split("\n", 1)[1])

    presence = payload["validated_fact_presence"]
    assert presence["customer_request.refund_reason"] is False
    assert presence["customer_request.requested_amount"] is True
    assert presence["order_facts.prior_refund_total"] is True


def test_azure_format_uses_strict_discrete_confidence_schema() -> None:
    response_format = _strict_json_format(PolicyReasoningResult)

    assert response_format["type"] == "json_schema"
    assert response_format["strict"] is True
    schema = response_format["schema"]
    assert schema["required"] == list(schema["properties"])
    confidence_levels = schema["properties"]["decision"]["anyOf"]
    assert [branch["properties"]["confidence"]["const"] for branch in confidence_levels] == [3, 2, 1, 0]
    assert [branch["properties"]["confidence_level"]["const"] for branch in confidence_levels] == [
        "high",
        "moderate",
        "low",
        "insufficient",
    ]
    for definition in schema["$defs"].values():
        if "properties" in definition:
            assert definition["required"] == list(definition["properties"])
            assert definition["additionalProperties"] is False


def test_azure_repair_applies_only_returned_existing_path_replacements() -> None:
    invalid = make_policy_result(make_input()).model_dump_json()
    repair = AzureJsonRepair.model_validate(
        {
            "replacements": [
                {"path": "/decision/confidence", "value_json": "2"},
                {"path": "/decision/confidence_level", "value_json": '"moderate"'},
            ]
        }
    )

    repaired = json.loads(_apply_json_repair(invalid, repair))

    assert repaired["decision"]["confidence"] == 2
    assert repaired["decision"]["confidence_level"] == "moderate"
    assert repaired["case"]["trace_id"] == "TRACE-UNIT"


def test_policy_repair_replaces_complete_confidence_evidence() -> None:
    result = make_policy_result(make_input())
    payload = result.model_dump(mode="json")
    evidence = payload["decision"]["confidence_evidence"]
    evidence["policy_support"] = "minor_ambiguity"
    evidence["minor_ambiguities"] = ["A minor ambiguity remains."]
    repair = AzurePolicyRepair.model_validate(
        {
            "confidence_correction": {
                "confidence": 2,
                "confidence_level": "moderate",
                "confidence_evidence": evidence,
                "precedent_evidence": payload["decision"]["precedent_evidence"],
            },
            "replacements": [],
        }
    )

    repaired = json.loads(_apply_json_repair(result.model_dump_json(), repair))

    assert repaired["decision"]["confidence"] == 2
    assert repaired["decision"]["confidence_evidence"]["policy_support"] == "minor_ambiguity"
