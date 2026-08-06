from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from governance import GovernanceAssessment

from .azure import AzureJsonClient
from .governance_node import GovernanceNode
from .models import (
    PolicyAgentInput,
    PolicyAgentOutput,
    PolicyReasoningResult,
    PrecedentContext,
    TokenUsage,
)
from .policy_node import PolicyReasoningNode
from .state_adapter import assemble_policy_output


class PolicyGraphInput(TypedDict):
    policy_input: PolicyAgentInput


class PolicyGraphOutput(TypedDict):
    policy_output: PolicyAgentOutput
    policy_result: PolicyReasoningResult
    precedent_context: PrecedentContext
    governance_assessment: GovernanceAssessment
    policy_usage: TokenUsage
    governance_usage: TokenUsage
    usage: TokenUsage


class PolicyGraphState(PolicyGraphInput, PolicyGraphOutput, total=False):
    pass


def build_policy_agent_graph(client: AzureJsonClient | None = None):
    """Build a compiled subgraph that can be mounted as a parent graph node."""

    azure = client or AzureJsonClient.from_env()
    builder = StateGraph(
        PolicyGraphState,
        input_schema=PolicyGraphInput,
        output_schema=PolicyGraphOutput,
    )
    policy_node = PolicyReasoningNode(azure)
    policy_governance_node = GovernanceNode(azure)

    def governance_step(state: PolicyGraphState) -> dict:
        result = policy_governance_node(state)
        assessment = GovernanceAssessment.model_validate(result["governance_assessment"])
        policy_result = PolicyReasoningResult.model_validate(state["policy_result"])
        policy_usage = TokenUsage.model_validate(state["policy_usage"])
        governance_usage = TokenUsage.model_validate(result["governance_usage"])
        return {
            "governance_assessment": assessment,
            "policy_output": assemble_policy_output(policy_result, assessment),
            "governance_usage": governance_usage,
            "usage": policy_usage.add(governance_usage),
        }

    builder.add_node("policy_reasoning", policy_node)
    builder.add_node("policy_governance", governance_step)
    builder.add_edge(START, "policy_reasoning")
    builder.add_edge("policy_reasoning", "policy_governance")
    builder.add_edge("policy_governance", END)
    return builder.compile(name="policy_agent")
