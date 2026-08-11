from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agents.policy.azure import AzureJsonResult
from agents.policy.graph import build_policy_agent_graph
from agents.policy.models import PolicyAgentOutput, PolicyReasoningResult, TokenUsage
from agents.policy.policy_node import policy_output_from_state
from agents.policy.tests.factories import allow_governance, make_input, make_policy_result
from governance import GovernanceAssessment


class FakeAzureClient:
    def __init__(self, policy_result, governance) -> None:
        self.policy_result = policy_result
        self.governance = governance
        self.calls: list[str] = []

    def generate(self, *, target, model_type, validate, **_kwargs):
        self.calls.append(target)
        value = self.policy_result if model_type is PolicyReasoningResult else self.governance
        validate(value)
        return AzureJsonResult(
            value=value,
            usage=TokenUsage(input_tokens=10, output_tokens=4),
        )


def test_policy_subgraph_has_exact_three_node_order_and_json_state() -> None:
    policy_input = make_input()
    policy_result = make_policy_result(policy_input)
    client = FakeAzureClient(policy_result, allow_governance())
    graph = build_policy_agent_graph(client)

    graph_view = graph.get_graph()
    nodes = set(graph_view.nodes) - {"__start__", "__end__"}
    edges = {(edge.source, edge.target) for edge in graph_view.edges}
    assert nodes == {"policy", "policy_governance", "policy_handoff"}
    assert edges == {
        ("__start__", "policy"),
        ("policy", "policy_governance"),
        ("policy_governance", "policy_handoff"),
        ("policy_handoff", "__end__"),
    }

    result = graph.invoke(_state(policy_input))
    json.dumps(result)
    output = policy_output_from_state(result)
    assert client.calls == ["policy reasoning result", "governance assessment"]
    assert result["policy_result"] == policy_result.model_dump(mode="json")
    assert result["policy_handoff"] == "refund"
    assert output.decision == policy_result.decision
    assert output.handoff.next_agent == "refund_agent"


def test_proposal_output_and_extended_decision_keep_exact_order() -> None:
    policy_input = make_input()
    client = FakeAzureClient(make_policy_result(policy_input), allow_governance())
    output = policy_output_from_state(
        build_policy_agent_graph(client).invoke(_state(policy_input))
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
    output = policy_output_from_state(
        build_policy_agent_graph(client).invoke(_state(policy_input))
    )

    extra = output.model_dump(mode="json")
    extra["decision"]["azure_confidence"] = 3
    with pytest.raises(ValidationError):
        PolicyAgentOutput.model_validate(extra)

    wrong_route = output.model_dump(mode="json")
    wrong_route["handoff"]["next_agent"] = "response_agent"
    with pytest.raises(ValidationError, match="require route refund_agent"):
        PolicyAgentOutput.model_validate(wrong_route)

    governance_payload = allow_governance().model_dump(mode="json")
    governance_payload["confidence"] = 3
    with pytest.raises(ValidationError):
        GovernanceAssessment.model_validate(governance_payload)


def _state(policy_input) -> dict:
    payload = policy_input.model_dump(mode="json")
    payload["case"]["goal"] = "evaluate refund eligibility"
    return {
        "trace_id": policy_input.case.trace_id,
        "ticket_id": policy_input.case.ticket_id,
        "triage_output": payload,
        "risk_flags": [],
        "llm_usage_events": [],
    }
