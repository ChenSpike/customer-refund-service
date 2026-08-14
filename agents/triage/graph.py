from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.mappers.triage_mapper import determine_triage_handoff
from app.state import AppState
from db.pipeline_store import PipelineStore, TriagePersistenceNode
from governance import GovernanceEventWriter
from tools.azure_client import deployment_for

from agents.policy.azure import AzureJsonClient

from .governance_node import GovernanceNode
from .node import TriageNode, triage_node


def triage_handoff_node(state: AppState) -> dict:
    return {"triage_handoff": determine_triage_handoff(state)}


def build_triage_agent_graph(
    *,
    client: AzureJsonClient | None = None,
    event_writer: GovernanceEventWriter | None = None,
    store: PipelineStore | None = None,
):
    """Build the triage subgraph using the same AppState-native nodes as the main graph."""

    builder = StateGraph(AppState)
    triage = (
        TriageNode(responses_client=client.client, model=deployment_for("triage"))
        if client is not None
        else triage_node
    )
    builder.add_node("triage", triage)
    builder.add_node(
        "triage_governance",
        GovernanceNode(client=client, event_writer=event_writer),
    )
    builder.add_node("triage_handoff", triage_handoff_node)
    builder.add_node(
        "triage_persistence",
        TriagePersistenceNode(store or PipelineStore.from_env()),
    )

    builder.add_edge(START, "triage")
    builder.add_edge("triage", "triage_governance")
    builder.add_edge("triage_governance", "triage_handoff")
    builder.add_edge("triage_handoff", "triage_persistence")
    builder.add_edge("triage_persistence", END)

    return builder.compile(name="triage_agent")
