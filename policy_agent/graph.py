from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .azure import AzureJsonClient
from .governance_node import GovernanceNode
from .models import (
    GovernanceAssessment,
    PolicyAgentInput,
    PolicyAgentOutput,
    PolicyReasoningResult,
    PrecedentContext,
    TokenUsage,
)
from .policy_node import PolicyReasoningNode


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
    builder.add_node("policy_reasoning", PolicyReasoningNode(azure))
    builder.add_node("governance", GovernanceNode(azure))
    builder.add_edge(START, "policy_reasoning")
    builder.add_edge("policy_reasoning", "governance")
    builder.add_edge("governance", END)
    return builder.compile(name="policy_agent")
