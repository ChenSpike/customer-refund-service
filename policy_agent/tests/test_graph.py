from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from policy_agent.azure import AzureJsonResult
from policy_agent.graph import build_policy_agent_graph
from policy_agent.models import (
    GovernanceAssessment,
    PolicyAgentOutput,
    PolicyGapOrConflict,
    PolicyReasoningResult,
    TokenUsage,
)
from policy_agent.tests.factories import (
    allow_governance,
    make_input,
    make_policy,
    make_policy_result,
)


class FakeAzureClient:
    def __init__(self, policy_result: PolicyReasoningResult, governance: GovernanceAssessment) -> None:
        self.policy_result = policy_result
        self.governance = governance
        self.calls: list[str] = []

    def generate(self, *, target, model_type, validate, **_kwargs):
        self.calls.append(target)
        value = self.policy_result if model_type is PolicyReasoningResult else self.governance
        validate(value)
        return AzureJsonResult(value=value, usage=TokenUsage(input_tokens=10, output_tokens=4))


def test_graph_has_exact_two_node_order_and_preserves_policy_result() -> None:
    policy_input = make_input()
    policy_result = make_policy_result(policy_input)
    client = FakeAzureClient(policy_result, allow_governance())
    graph = build_policy_agent_graph(client)

    graph_view = graph.get_graph()
    nodes = set(graph_view.nodes) - {"__start__", "__end__"}
    edges = {(edge.source, edge.target) for edge in graph_view.edges}
    assert nodes == {"policy_reasoning", "governance"}
    assert edges == {
        ("__start__", "policy_reasoning"),
        ("policy_reasoning", "governance"),
        ("governance", "__end__"),
    }

    result = graph.invoke({"policy_input": policy_input})
    output = PolicyAgentOutput.model_validate(result["policy_output"])

    assert client.calls == ["policy reasoning result", "governance assessment"]
    assert result["policy_result"] == policy_result
    assert output.decision == policy_result.decision
    assert output.handoff.next_agent == "refund_agent"
    assert result["usage"] == TokenUsage(input_tokens=20, output_tokens=8)
    assert result["precedent_context"].status == "empty"
    assert "confidence" not in json.dumps(GovernanceAssessment.model_json_schema())
    assert "handoff" not in json.dumps(GovernanceAssessment.model_json_schema())


@pytest.mark.parametrize(
    ("result_kind", "expected_decision", "expected_route"),
    [
        ("missing", "request_info", "response_agent"),
        ("review", "manual_review", "human_approval"),
    ],
)
def test_policy_decisions_route_to_the_required_agent(
    result_kind: str,
    expected_decision: str,
    expected_route: str,
) -> None:
    if result_kind == "missing":
        policy_input = make_input(refund_reason=None)
        policy_result = make_policy_result(
            policy_input,
            decision_type="request_info",
            policies=[make_policy("R-REQUEST-MISSING-FACTS", "requires_review")],
            required_fact_paths=["customer_request.refund_reason"],
            comparison_decision=None,
        )
    else:
        policy_input = make_input()
        policy_result = make_policy_result(
            policy_input,
            decision_type="manual_review",
            policies=[make_policy("R-REVIEW-HIGH-VALUE", "requires_review")],
            comparison_decision=None,
        )
    client = FakeAzureClient(policy_result, allow_governance())

    output = PolicyAgentOutput.model_validate(
        build_policy_agent_graph(client).invoke({"policy_input": policy_input})["policy_output"]
    )

    assert output.decision.type == expected_decision
    assert output.handoff.next_agent == expected_route


def test_proposal_output_and_extended_decision_keep_exact_order() -> None:
    policy_input = make_input()
    client = FakeAzureClient(make_policy_result(policy_input), allow_governance())
    output = PolicyAgentOutput.model_validate(
        build_policy_agent_graph(client).invoke({"policy_input": policy_input})["policy_output"]
    )
    payload = output.model_dump(mode="json")

    assert list(payload) == [
        "case",
        "customer_request",
        "policy_evaluation",
        "decision",
        "response_guidance",
        "handoff",
        "governance",
    ]
    assert list(payload["decision"]) == [
        "type",
        "refund_amount",
        "confidence",
        "confidence_level",
        "confidence_evidence",
        "precedent_evidence",
        "reason",
    ]
    assert "evidence_manifest" not in payload


def test_output_and_governance_models_reject_extra_fields_and_wrong_routes() -> None:
    policy_input = make_input()
    client = FakeAzureClient(make_policy_result(policy_input), allow_governance())
    output = PolicyAgentOutput.model_validate(
        build_policy_agent_graph(client).invoke({"policy_input": policy_input})["policy_output"]
    )

    output_payload = output.model_dump(mode="json")
    output_payload["decision"]["azure_confidence"] = 3
    with pytest.raises(ValidationError):
        PolicyAgentOutput.model_validate(output_payload)

    wrong_route = output.model_dump(mode="json")
    wrong_route["handoff"]["next_agent"] = "response_agent"
    with pytest.raises(ValidationError, match="require route refund_agent"):
        PolicyAgentOutput.model_validate(wrong_route)

    governance_payload = allow_governance().model_dump(mode="json")
    governance_payload["confidence"] = 3
    with pytest.raises(ValidationError):
        GovernanceAssessment.model_validate(governance_payload)

    blocked_payload = allow_governance().model_dump(mode="json")
    blocked_payload["governance"]["interceptor_action"] = "block"
    with pytest.raises(ValidationError):
        GovernanceAssessment.model_validate(blocked_payload)
