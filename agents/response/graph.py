from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.state import AppState
from agents.policy.azure import AzureJsonClient
from db.pipeline_store import PipelineStore, ResponsePersistenceNode
from governance import GovernanceEventWriter
from tools.azure_client import deployment_for

from .governance_node import ResponseGovernanceNode
from .node import ResponseNode, response_node


def response_handoff_node(state: AppState) -> dict:
    governance = state.get("response_governance_result") or {}
    handoff = "human_review" if governance.get("status") == "block" else "end"
    return {"response_handoff": handoff}


def build_response_agent_graph(
    *,
    client: AzureJsonClient | None = None,
    store: PipelineStore | None = None,
    event_writer: GovernanceEventWriter | None = None,
):
    builder = StateGraph(AppState)
    response = (
        ResponseNode(responses_client=client.client, model=deployment_for("response"))
        if client is not None
        else response_node
    )
    builder.add_node("response", response)
    builder.add_node("response_governance", ResponseGovernanceNode(event_writer=event_writer))
    builder.add_node("response_handoff", response_handoff_node)
    builder.add_node(
        "response_persistence",
        ResponsePersistenceNode(store or PipelineStore.from_env()),
    )

    builder.add_edge(START, "response")
    builder.add_edge("response", "response_governance")
    builder.add_edge("response_governance", "response_handoff")
    builder.add_edge("response_handoff", "response_persistence")
    builder.add_edge("response_persistence", END)

    return builder.compile(name="response_agent")
