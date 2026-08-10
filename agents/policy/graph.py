from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.routers.policy_router import route_after_policy
from app.state import AppState
from governance import GovernanceEventWriter

from .azure import AzureJsonClient
from .governance_node import GovernanceNode
from .policy_node import AppStatePolicyNode


def build_policy_agent_graph(
    client: AzureJsonClient | None = None,
    *,
    event_writer: GovernanceEventWriter | None = None,
):
    """Build the policy subgraph using the same AppState-native nodes as the main graph."""

    azure = client or AzureJsonClient.from_env()
    builder = StateGraph(AppState)
    policy_node = AppStatePolicyNode(azure)
    policy_governance_node = GovernanceNode(client=azure, event_writer=event_writer)

    builder.add_node("policy", policy_node)
    builder.add_node("policy_governance", policy_governance_node)
    builder.add_edge(START, "policy")
    builder.add_edge("policy", "policy_governance")
    builder.add_conditional_edges(
        "policy_governance",
        route_after_policy,
        {
            "refund_agent": END,
            "response_agent": END,
            "human_approval": END,
        },
    )
    return builder.compile(name="policy_agent")
