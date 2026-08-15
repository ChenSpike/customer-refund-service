from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.policy.models import PolicyDecision, PolicyGapOrConflict
from agents.policy.policy_node import load_policy_context, validate_policy_result
from agents.policy.tests.factories import (
    make_input,
    make_match,
    make_policy,
    make_policy_result,
    make_precedent_context,
    unavailable_context,
)


def _validate(result, policy_input, precedents=None) -> None:
    validate_policy_result(
        result,
        policy_input,
        load_policy_context("v1.0"),
        precedents or unavailable_context(),
    )


def test_high_confidence_uses_complete_facts_and_clear_policy_support() -> None:
    policy_input = make_input()
    result = make_policy_result(policy_input)

    _validate(result, policy_input)

    assert result.decision.confidence == 3
    assert result.decision.confidence_level == "high"
    assert result.decision.confidence_evidence.facts_complete is True
    assert result.decision.confidence_evidence.policy_support == "clear"


def test_unavailable_precedent_memory_is_neutral_not_a_confidence_penalty() -> None:
    policy_input = make_input()
    context = unavailable_context("malformed")
    result = make_policy_result(policy_input, precedent_context=context)

    _validate(result, policy_input, context)

    assert result.decision.confidence == 3
    assert result.decision.precedent_evidence.status == "unavailable"
    assert result.decision.precedent_evidence.memory_status == "malformed"
    assert result.decision.precedent_evidence.assessment == "unavailable"
    assert "malformed" in result.decision.precedent_evidence.explanation


def test_moderate_confidence_requires_minor_interpretive_ambiguity() -> None:
    policy_input = make_input()
    result = make_policy_result(
        policy_input,
        policy_support="minor_ambiguity",
        minor_ambiguities=["The narrative suggests a secondary issue not represented in structured facts."],
    )

    _validate(result, policy_input)

    assert result.decision.confidence == 2
    assert result.decision.confidence_level == "moderate"
    assert result.decision.type == "approve"


def test_mixed_precedents_produce_moderate_confidence() -> None:
    policy_input = make_input()
    context = make_precedent_context(["approve", "deny"])
    result = make_policy_result(
        policy_input,
        precedent_context=context,
        precedent_matches=[make_match(1), make_match(2)],
    )

    _validate(result, policy_input, context)

    evidence = result.decision.precedent_evidence
    assert result.decision.confidence == 2
    assert evidence.assessment == "mixed"
    assert (evidence.support_count, evidence.contradiction_count) == (1, 1)


def test_policy_conflict_produces_low_confidence_manual_review() -> None:
    policy_input = make_input()
    result = make_policy_result(
        policy_input,
        decision_type="manual_review",
        gaps=[PolicyGapOrConflict(type="policy_conflict", detail="The request conflicts with linked facts.")],
        comparison_decision="approve",
    )

    _validate(result, policy_input)

    assert result.decision.confidence == 1
    assert result.decision.confidence_level == "low"
    assert result.decision.type == "manual_review"


def test_strong_precedent_disagreement_produces_low_confidence_manual_review() -> None:
    policy_input = make_input()
    context = make_precedent_context(["deny", "deny", "approve"])
    result = make_policy_result(
        policy_input,
        decision_type="manual_review",
        precedent_context=context,
        precedent_matches=[make_match(1), make_match(2), make_match(3)],
        comparison_decision="approve",
    )

    _validate(result, policy_input, context)

    assert result.decision.confidence == 1
    assert result.decision.precedent_evidence.assessment == "strongly_disagrees"
    assert result.decision.type == "manual_review"


def test_missing_essential_fact_produces_insufficient_request_info() -> None:
    policy_input = make_input(refund_reason=None)
    result = make_policy_result(
        policy_input,
        decision_type="request_info",
        policies=[make_policy("R-REQUEST-MISSING-FACTS", "requires_review")],
        required_fact_paths=["customer_request.refund_reason"],
        comparison_decision=None,
    )

    _validate(result, policy_input)

    assert result.decision.confidence == 0
    assert result.decision.confidence_level == "insufficient"
    assert result.decision.confidence_evidence.essential_fact_paths_missing == [
        "customer_request.refund_reason"
    ]
    assert result.decision.type == "request_info"


def test_no_relevant_policy_produces_insufficient_manual_review() -> None:
    policy_input = make_input()
    result = make_policy_result(
        policy_input,
        decision_type="manual_review",
        policies=[],
        supporting_rule_ids=[],
        required_fact_paths=[],
        evidence_items=[],
        comparison_decision=None,
    )

    _validate(result, policy_input)

    assert result.decision.confidence == 0
    assert result.decision.confidence_evidence.policy_support == "none"
    assert result.decision.type == "manual_review"


def test_clear_review_rule_can_be_high_confidence() -> None:
    policy_input = make_input()
    result = make_policy_result(
        policy_input,
        decision_type="manual_review",
        policies=[make_policy("R-REVIEW-HIGH-VALUE", "requires_review")],
        comparison_decision=None,
    )

    _validate(result, policy_input)

    assert result.decision.confidence == 3
    assert result.decision.confidence_level == "high"


def test_numeric_zero_is_a_present_fact() -> None:
    policy_input = make_input(requested_amount=0.0)
    result = make_policy_result(
        policy_input,
        decision_type="manual_review",
        policies=[make_policy("R-REVIEW-HIGH-VALUE", "requires_review")],
        required_fact_paths=[
            "customer_request.requested_amount",
            "order_facts.prior_refund_total",
        ],
        comparison_decision=None,
    )

    _validate(result, policy_input)

    assert result.decision.confidence == 3
    assert result.decision.confidence_evidence.facts_complete is True


def test_confidence_is_structurally_discrete() -> None:
    payload = make_policy_result(make_input()).decision.model_dump(mode="json")
    payload["confidence"] = 2.5

    with pytest.raises(ValidationError):
        PolicyDecision.model_validate(payload)


def test_python_rejects_inconsistent_decision_without_rewriting() -> None:
    policy_input = make_input()
    result = make_policy_result(
        policy_input,
        decision_type="manual_review",
        policy_support="weak",
        comparison_decision="approve",
    )
    result.decision.type = "approve"

    with pytest.raises(ValueError, match="low confidence requires manual_review"):
        _validate(result, policy_input)
    assert result.decision.type == "approve"
