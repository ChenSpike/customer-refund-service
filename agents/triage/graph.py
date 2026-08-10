from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.routers.triage_router import route_after_triage
from app.state import AppState
from governance import GovernanceEventWriter

from agents.policy.azure import AzureJsonClient

from .governance_node import GovernanceNode
from .node import triage_node


def build_triage_agent_graph(
    *,
    client: AzureJsonClient | None = None,
    event_writer: GovernanceEventWriter | None = None,
):
    """Build the triage subgraph using the same AppState-native nodes as the main graph."""

    builder = StateGraph(AppState)
    builder.add_node("triage", triage_node)
    builder.add_node(
        "triage_governance",
        GovernanceNode(client=client, event_writer=event_writer),
    )

    builder.add_edge(START, "triage")
    builder.add_edge("triage", "triage_governance")
    builder.add_conditional_edges(
        "triage_governance",
        route_after_triage,
        {
            "policy": END,
            "response_agent": END,
            "human_approval": END,
        },
    )

    return builder.compile(name="triage_agent")