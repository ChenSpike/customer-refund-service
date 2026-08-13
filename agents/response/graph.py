from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.state import AppState
from db.pipeline_store import PipelineStore, ResponsePersistenceNode

from .governance_node import ResponseGovernanceNode
from .node import response_node


def response_handoff_node(state: AppState) -> dict:
    governance = state.get("response_governance_result") or {}
    handoff = "human_review" if governance.get("status") == "block" else "end"
    return {"response_handoff": handoff}


def build_response_agent_graph(*, store: PipelineStore | None = None):
    builder = StateGraph(AppState)
    builder.add_node("response", response_node)
    builder.add_node("response_governance", ResponseGovernanceNode())
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