from __future__ import annotations

import pytest
from langgraph.graph import END, START, StateGraph

from app.state import AppState
from governance import GovernanceAssessment

from agents.policy.azure import AzureJsonResult
from agents.policy.governance_node import GovernanceNode
from agents.policy.models import PolicyReasoningResult, TokenUsage
from agents.policy.policy_node import AppStatePolicyNode, policy_output_from_state, policy_usage_from_state
from agents.policy.routing import route_policy
from agents.policy.tests.factories import (
    allow_governance,
    make_input,
    make_policy,
    make_policy_result,
    quarantine_governance,
)


class FakeAzureClient:
    def __init__(
        self,
        policy_result: PolicyReasoningResult,
        governance: GovernanceAssessment,
    ) -> None:
        self.policy_result = policy_result
        self.governance = governance
        self.calls: list[str] = []

    def generate(self, *, target, model_type, validate, **_kwargs):
        self.calls.append(target)
        value = self.policy_result if model_type is PolicyReasoningResult else self.governance
        validate(value)
        return AzureJsonResult(
            value=value,
            usage=TokenUsage(input_tokens=13, output_tokens=5),
        )


def test_direct_policy_nodes_return_business_patches_and_usage_events() -> None:
    policy_input = make_input()
    client = FakeAzureClient(make_policy_result(policy_input), allow_governance())
    policy_node = AppStatePolicyNode(client)
    governance_node = GovernanceNode(client=client)
    initial = _app_state(policy_input)

    policy_patch = policy_node(initial)
    after_policy = initial | policy_patch
    governance_patch = governance_node(after_policy)
    final_state = after_policy | governance_patch

    assert client.calls == ["policy reasoning result", "governance assessment"]
    assert set(policy_patch) == {
        "current_stage",
        "policy_result",
        "policy_decision",
        "policy_context",
        "llm_input_tokens",
        "llm_output_tokens",
        "llm_usage_events",
    }
    assert policy_patch["policy_decision"]["decision"] == "approve"
    assert policy_patch["policy_context"]["policy_version_used"] == "v1.0"
    assert policy_patch["llm_usage_events"] == [
        {
            "agent": "policy_agent",
            "stage": "policy_reasoning",
            "input_tokens": 13,
            "output_tokens": 5,
        }
    ]
    assert {
        "current_stage",
        "policy_governance_result",
        "risk_flags",
        "llm_input_tokens",
        "llm_output_tokens",
        "llm_usage_events",
    }.issubset(governance_patch)
    assert governance_patch["policy_governance_result"]["status"] == "allow"
    assert governance_patch["risk_flags"] == []
    assert governance_patch["llm_usage_events"][0]["stage"] == "policy_governance"
    assert route_policy(final_state["policy_decision"]["decision"], final_state["policy_governance_result"]["status"]) == "refund_agent"
    assert policy_output_from_state(final_state).handoff.next_agent == "refund_agent"


def test_direct_policy_nodes_mount_as_policy_and_policy_governance_in_parent_graph() -> None:
    policy_input = make_input()
    client = FakeAzureClient(make_policy_result(policy_input), allow_governance())
    builder = StateGraph(AppState)
    builder.add_node("policy", AppStatePolicyNode(client))
    builder.add_node("policy_governance", GovernanceNode(client=client))
    builder.add_edge(START, "policy")
    builder.add_edge("policy", "policy_governance")
    builder.add_conditional_edges(
        "policy_governance",
        lambda state: route_policy(
            state["policy_decision"]["decision"],
            state["policy_governance_result"]["status"],
        ),
        {
            "refund_agent": END,
            "response_agent": END,
            "human_approval": END,
        },
    )
    graph = builder.compile()

    result = graph.invoke(_app_state(policy_input))

    assert set(graph.get_graph().nodes) - {"__start__", "__end__"} == {
        "policy",
        "policy_governance",
    }
    assert result["current_stage"] == "policy_governance"
    assert result["policy_result"].decision.type == "approve"
    assert result["policy_decision"]["decision"] == "approve"
    assert result["policy_governance_result"]["status"] == "allow"
    assert result["llm_input_tokens"] == 26
    assert result["llm_output_tokens"] == 10
    assert [event["stage"] for event in result["llm_usage_events"]] == [
        "policy_reasoning",
        "policy_governance",
    ]
    assert policy_usage_from_state(result) == TokenUsage(
        input_tokens=26,
        output_tokens=10,
    )


def test_policy_usage_ignores_other_agents_and_rejects_duplicate_policy_events() -> None:
    state = {
        "llm_usage_events": [
            {
                "agent": "triage_agent",
                "stage": "triage",
                "input_tokens": 100,
                "output_tokens": 50,
            },
            {
                "agent": "policy_agent",
                "stage": "policy_reasoning",
                "input_tokens": 13,
                "output_tokens": 5,
            },
            {
                "agent": "policy_agent",
                "stage": "policy_governance",
                "input_tokens": 17,
                "output_tokens": 7,
            },
        ]
    }

    assert policy_usage_from_state(state) == TokenUsage(
        input_tokens=30,
        output_tokens=12,
    )

    state["llm_usage_events"].append(dict(state["llm_usage_events"][1]))
    with pytest.raises(ValueError, match="exactly one"):
        policy_usage_from_state(state)


def test_triage_only_fields_are_removed_and_identity_is_required() -> None:
    policy_input = make_input()
    client = FakeAzureClient(make_policy_result(policy_input), allow_governance())
    node = AppStatePolicyNode(client)
    state = _app_state(policy_input)

    patch = node(state)

    assert patch["policy_context"]["policy_version_used"] == "v1.0"
    assert client.calls == ["policy reasoning result"]

    state["trace_id"] = "TRACE-MISMATCH"
    with pytest.raises(ValueError, match="trace_id must match"):
        node(state)
    assert client.calls == ["policy reasoning result"]


def test_policy_governance_rejects_tampered_policy_state_before_azure() -> None:
    policy_input = make_input()
    client = FakeAzureClient(make_policy_result(policy_input), allow_governance())
    policy_node = AppStatePolicyNode(client)
    governance_node = GovernanceNode(client=client)
    state = _app_state(policy_input)
    state.update(policy_node(state))
    state["policy_decision"]["refund_amount"] = 0

    patch = governance_node(state)

    assert patch["policy_governance_result"]["status"] == "block"
    assert patch["risk_flags"][0]["flag"] == "forbidden_tool"
    assert client.calls == ["policy reasoning result", "governance assessment"]


def test_governance_block_preserves_policy_and_adds_stage_specific_findings() -> None:
    policy_input = make_input()
    policy_result = make_policy_result(policy_input)
    client = FakeAzureClient(policy_result, quarantine_governance())
    policy_node = AppStatePolicyNode(client)
    governance_node = GovernanceNode(client=client)
    state = _app_state(policy_input)
    state.update(policy_node(state))
    decision_before = dict(state["policy_decision"])
    context_before = dict(state["policy_context"])

    governance_patch = governance_node(state)
    state.update(governance_patch)

    assert "policy_decision" not in governance_patch
    assert "policy_context" not in governance_patch
    assert state["policy_decision"] == decision_before
    assert state["policy_context"] == context_before
    assert governance_patch["policy_governance_result"]["status"] == "block"
    assert governance_patch["risk_flags"][0]["stage"] == "policy"
    assert governance_patch["human_review_required"] is True
    assert governance_patch["workflow_status"] == "waiting_human"
    assert governance_patch["governance_assessment"].governance.interceptor_action == "quarantine"
    assert governance_patch["governance_assessment"].findings[0].flag == "semantic_drift"
    assert route_policy(state["policy_decision"]["decision"], governance_patch["policy_governance_result"]["status"]) == "human_approval"


@pytest.mark.parametrize(
    ("decision", "status", "expected"),
    [
        ("approve", "allow", "refund_agent"),
        ("partial_refund", "allow", "refund_agent"),
        ("deny", "allow", "response_agent"),
        ("request_info", "allow", "response_agent"),
        ("manual_review", "allow", "human_approval"),
        ("approve", "block", "human_approval"),
    ],
)
def test_parent_route_matrix(decision: str, status: str, expected: str) -> None:
    assert route_policy(decision, status) == expected


def test_request_info_proposal_handoff_uses_response_agent() -> None:
    policy_input = make_input(refund_reason=None)
    policy_result = make_policy_result(
        policy_input,
        decision_type="request_info",
        policies=[make_policy("R-REQUEST-MISSING-FACTS", "requires_review")],
        required_fact_paths=["customer_request.refund_reason"],
        comparison_decision=None,
    )
    client = FakeAzureClient(policy_result, allow_governance())
    policy_node = AppStatePolicyNode(client)
    governance_node = GovernanceNode(client=client)
    state = _app_state(policy_input)
    state.update(policy_node(state))
    state.update(governance_node(state))

    assert state["policy_decision"]["decision"] == "request_info"
    assert route_policy(state["policy_decision"]["decision"], state["policy_governance_result"]["status"]) == "response_agent"


def _app_state(policy_input) -> dict:
    return {
        "trace_id": policy_input.case.trace_id,
        "ticket_id": policy_input.case.ticket_id,
        "triage_output": policy_input.model_dump(mode="json"),
        "current_stage": "triage_governance",
        "workflow_status": "running",
    }
