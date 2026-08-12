from langgraph.graph import END, START, StateGraph

from agents.policy import build_policy_agent_graph
from agents.policy.azure import AzureJsonClient
from agents.refund.node import refund_node
from agents.triage import build_triage_agent_graph
from agents.response.governance_node import ResponseGovernanceNode
from agents.response.node import response_node
from app.mappers.policy_mapper import map_policy_handoff_to_parent_node
from app.mappers.triage_mapper import map_triage_handoff_to_parent_node
from app.state import AppState
from db.backend import DatabaseGovernanceEventRepository
from db.database import GCPRepository
from db.pipeline_store import PipelineStore, PolicyPersistenceNode

def human_approval_node(state: AppState) -> dict:
    governance_result = state.get("policy_governance_result") or state.get(
        "triage_governance_result"
    ) or state.get("governance_result", {})
    policy_decision = state.get("policy_decision", {})

    reason = "manual_review"
    if governance_result.get("status") == "block":
        reason = "governance_block"
    elif policy_decision.get("decision") == "manual_review":
        reason = "policy_manual_review"

    return {
        "human_review": {
            "status": "pending",
            "reason": reason,
        }
    }

def route_after_response_governance(state: AppState) -> str:
    result = state.get("response_governance_result") or {}
    if result.get("status") == "block":
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
    )
    policy_agent = build_policy_agent_graph(azure)
    policy_persistence = PolicyPersistenceNode(PipelineStore(cloud_repository))
    response_governance = ResponseGovernanceNode()

    builder.add_node("triage_agent", triage_agent)
    builder.add_node("policy_agent", policy_agent)
    builder.add_node("policy_persistence", policy_persistence)
    builder.add_node("refund_agent", refund_node)
    builder.add_node("response_agent", response_node)
    builder.add_node("response_governance", response_governance)
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
    builder.add_edge("policy_agent", "policy_persistence")
    builder.add_conditional_edges(
        "policy_persistence",
        map_policy_handoff_to_parent_node,
        {
            "refund_agent": "refund_agent",
            "response_agent": "response_agent",
            "human_approval": "human_approval",
        },
    )

    builder.add_edge("refund_agent", "response_agent")
    builder.add_edge("human_approval", "response_agent")
    builder.add_edge("response_agent", "response_governance")
    builder.add_conditional_edges(
        "response_governance",
        route_after_response_governance,
        {
            "human_approval": "human_approval",
            END: END,
        },
    )

    return builder.compile()
