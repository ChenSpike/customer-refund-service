from langgraph.graph import END, START, StateGraph

from agents.policy import build_policy_agent_graph
from agents.policy.azure import AzureJsonClient
from agents.refund.node import refund_node
from agents.response import build_response_agent_graph
from agents.triage import build_triage_agent_graph
from app.mappers.policy_mapper import map_policy_handoff_to_parent_node
from app.mappers.triage_mapper import map_triage_handoff_to_parent_node
from app.state import AppState
from db.backend import DatabaseGovernanceEventRepository
from db.database import GCPRepository
from db.pipeline_store import PipelineStore

def human_approval_node(state: AppState) -> dict:
    reason = state.get("review_trigger_reason") or "manual_review"
    stage = state.get("review_trigger_stage") or "unknown"

    return {
        "current_stage": "human_approval",
        "human_review": {
            "status": "pending",
            "reason": reason,
            "stage": stage,
        },
        "human_review_required": True,
        "final_outcome": "manual_review",
        "workflow_status": "waiting_human",
    }

def route_after_response_persistence(state: AppState) -> str:
    result = state.get("response_persistence_result") or {}
    next_agent = result.get("next_agent", "end")
    if next_agent == "human_approval":
        return "human_approval"
    return END

def build_graph(
    *,
    client: AzureJsonClient | None = None,
    repository: GCPRepository | None = None,
):
    builder = StateGraph(AppState)
    cloud_repository = repository or GCPRepository.from_env()
    governance_repository = DatabaseGovernanceEventRepository(cloud_repository)
    azure = client or AzureJsonClient.from_env()
    triage_agent = build_triage_agent_graph(
        client=azure,
        event_writer=governance_repository,
        store=PipelineStore(cloud_repository),
    )
    policy_agent = build_policy_agent_graph(
        azure,
        store=PipelineStore(cloud_repository),
    )
    response_agent = build_response_agent_graph(store=PipelineStore(cloud_repository))

    builder.add_node("triage_agent", triage_agent)
    builder.add_node("policy_agent", policy_agent)
    builder.add_node("refund_agent", refund_node)
    builder.add_node("response_agent", response_agent)
    builder.add_node("human_approval", human_approval_node)

    builder.add_edge(START, "triage_agent")
    builder.add_conditional_edges(
        "triage_agent",
        map_triage_handoff_to_parent_node,
        {
            "policy": "policy_agent",
            "response_agent": "response_agent",
            "human_approval": "human_approval",
        },
    )
    builder.add_conditional_edges(
        "policy_agent",
        map_policy_handoff_to_parent_node,
        {
            "refund_agent": "refund_agent",
            "response_agent": "response_agent",
            "human_approval": "human_approval",
        },
    )

    builder.add_edge("refund_agent", "response_agent")
    builder.add_edge("human_approval", "response_agent")
    builder.add_conditional_edges(
        "response_agent",
        route_after_response_persistence,
        {
            "human_approval": "human_approval",
            END: END,
        },
    )

    return builder.compile()
