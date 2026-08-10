from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.mappers.triage_mapper import resolve_triage_handoff
from app.state import AppState
from governance import GovernanceEventWriter

from agents.policy.azure import AzureJsonClient

from .governance_node import GovernanceNode
from .node import triage_node


def triage_handoff_node(state: AppState) -> dict:
    return {"triage_handoff": resolve_triage_handoff(state)}


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
    builder.add_node("triage_handoff", triage_handoff_node)

    builder.add_edge(START, "triage")
    builder.add_edge("triage", "triage_governance")
    builder.add_edge("triage_governance", "triage_handoff")
    builder.add_edge("triage_handoff", END)

    return builder.compile(name="triage_agent")