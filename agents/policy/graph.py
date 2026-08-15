from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.mappers.policy_mapper import determine_policy_handoff
from app.state import AppState
from db.pipeline_store import PipelineStore, PolicyPersistenceNode

from .azure import AzureJsonClient
from .governance_node import GovernanceNode
from .policy_node import AppStatePolicyNode


class PolicyGraphInput(TypedDict):
    trace_id: str
    ticket_id: str
    triage_output: dict[str, Any]
    # LangGraph filters parent state through this input schema before entering
    # the subgraph.  The continuation fence therefore has to be declared here,
    # not only on AppState, or Policy persistence silently becomes an ordinary
    # rerun and can overwrite the initial Policy handoff.
    request_context: dict[str, Any]


def policy_handoff_node(state: AppState) -> dict:
    handoff = determine_policy_handoff(state)
    patch = {"policy_handoff": handoff}
    if handoff == "human_review":
        patch.update(
            human_review_required=True,
            workflow_status="waiting_human",
        )
    return patch


def build_policy_agent_graph(
    client: AzureJsonClient | None = None,
    *,
    store: PipelineStore | None = None,
):
    """Build the policy subgraph using the same AppState-native nodes as the main graph."""

    azure = client or AzureJsonClient.from_env()
    builder = StateGraph(AppState, input_schema=PolicyGraphInput)
    policy_node = AppStatePolicyNode(azure)
    policy_governance_node = GovernanceNode(client=azure)

    builder.add_node("policy", policy_node)
    builder.add_node("policy_governance", policy_governance_node)
    builder.add_node("policy_handoff", policy_handoff_node)
    builder.add_node(
        "policy_persistence",
        PolicyPersistenceNode(store or PipelineStore.from_env()),
    )
    builder.add_edge(START, "policy")
    builder.add_edge("policy", "policy_governance")
    builder.add_edge("policy_governance", "policy_handoff")
    builder.add_edge("policy_handoff", "policy_persistence")
    builder.add_edge("policy_persistence", END)
    return builder.compile(name="policy_agent")
