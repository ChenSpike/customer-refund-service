from __future__ import annotations

from governance import GovernanceStatement

from agents.policy.azure import AzureJsonResult
from agents.policy.governance_node import AzurePolicyGovernanceReviewer, GovernanceNode
from agents.policy.models import TokenUsage
from agents.policy.tests.factories import allow_governance, make_input, make_policy_result, quarantine_governance


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


class RecordingWriter:
    def __init__(self) -> None:
        self.saved: list[GovernanceStatement] = []

    def save_event(self, statement: GovernanceStatement) -> str:
        self.saved.append(statement)
        return "gov-evt-1"


def _state():
    policy_input = make_input()
    return {
        "policy_input": policy_input,
        "policy_result": make_policy_result(policy_input),
        "policy_decision": {"reason": ""},
    }


def test_azure_policy_governance_reviewer_returns_assessment_and_usage() -> None:
    client = FakeAzureClient(allow_governance())

    result = AzurePolicyGovernanceReviewer(client)(_state())

    assert result.value.governance.interceptor_action == "allow"
    assert result.usage.input_tokens == 11
    assert client.calls[0]["target"] == "governance assessment"


def test_policy_governance_node_persists_blocked_assessment_via_shared_writer() -> None:
    writer = RecordingWriter()
    node = GovernanceNode(reviewer=lambda _state: AzureJsonResult(
        value=quarantine_governance(),
        usage=TokenUsage(input_tokens=7, output_tokens=2),
    ), event_writer=writer)

    patch = node(_state())

    assert patch["governance_event_id"] == "gov-evt-1"
    assert patch["governance_assessment"].governance.interceptor_action == "quarantine"
    assert writer.saved[0].agent == "policy_agent"
    assert writer.saved[0].stage == "policy_governance"
    assert writer.saved[0].status == "block"
    assert writer.saved[0].findings[0].source == "llm"


def test_policy_governance_node_merges_deterministic_findings_into_assessment_and_statement() -> None:
    writer = RecordingWriter()
    state = _state()
    state["policy_decision"] = {"reason": "I already issued the refund and checked the database directly."}
    node = GovernanceNode(
        reviewer=lambda _state: AzureJsonResult(
            value=allow_governance(),
            usage=TokenUsage(input_tokens=5, output_tokens=1),
        ),
        event_writer=writer,
    )

    patch = node(state)

    assert patch["governance_assessment"].findings[0].flag == "forbidden_tool"
    assert patch["governance_assessment"].findings[0].source == "deterministic"
    assert writer.saved[0].status == "block"
    assert writer.saved[0].findings[0].flag == "forbidden_tool"