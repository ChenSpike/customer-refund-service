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
from db.pipeline_store import PipelineStore, RefundPersistenceNode


_REVIEW_STAGE_ALIASES = {
    "triage": "triage",
    "triage_agent": "triage",
    "triage_governance": "triage",
    "policy": "policy",
    "policy_agent": "policy",
    "policy_governance": "policy",
    "policy_review": "policy",
    "response": "response",
    "response_agent": "response",
    "response_governance": "response",
}


def _review_trigger_stage(state: AppState) -> str:
    """Return the strict repository stage for the routed human-review node."""

    raw_stage = str(state.get("review_trigger_stage") or "").strip().lower()
    normalized_stage = raw_stage.replace("-", "_").replace(" ", "_")
    explicit_stage = _REVIEW_STAGE_ALIASES.get(normalized_stage)
    if explicit_stage is not None:
        return explicit_stage

    # Prefer the latest persisted/semantic route.  Response can contain an
    # earlier Policy decision, so its route must be considered first.
    route_evidence = (
        (
            "response",
            state.get("response_persistence_result"),
            state.get("response_handoff"),
        ),
        (
            "policy",
            state.get("policy_persistence_result"),
            state.get("policy_handoff"),
        ),
        (
            "triage",
            state.get("triage_persistence_result"),
            state.get("triage_handoff"),
        ),
    )
    for stage, persisted_route, semantic_route in route_evidence:
        if (
            isinstance(persisted_route, dict)
            and persisted_route.get("next_agent") == "human_approval"
        ) or semantic_route in {"human_review", "human_approval"}:
            return stage

    policy_decision = state.get("policy_decision") or {}
    if isinstance(policy_decision, dict):
        decision = str(
            policy_decision.get("decision") or policy_decision.get("type") or ""
        ).strip().lower()
        if decision == "manual_review":
            return "policy"

    raise ValueError(
        "review_trigger_stage must identify triage, policy, or response human review"
    )


class HumanApprovalNode:
    def __init__(self, repository: GCPRepository) -> None:
        self.repository = repository

    def __call__(self, state: AppState) -> dict:
        reason = state.get("review_trigger_reason") or "manual_review"
        stage = _review_trigger_stage(state)
        approval_id = self.repository.ensure_human_approval(
            trace_id=str(state.get("trace_id") or ""),
            reason=reason,
            stage=stage,
            policy_decision=state.get("policy_decision") or {},
        )
        return {
            "current_stage": "human_approval",
            "review_trigger_stage": stage,
            "human_review": {
                "approval_id": approval_id,
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


def route_after_human_approval(state: AppState) -> str:
    # A response-governance block has already generated and persisted the draft.
    # Ending here prevents the old response -> human -> response recursion loop.
    if state.get("review_trigger_stage") == "response":
        return END
    return "response_agent"

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
    response_agent = build_response_agent_graph(
        client=azure,
        store=PipelineStore(cloud_repository),
        event_writer=governance_repository,
    )
    refund_persistence = RefundPersistenceNode(PipelineStore(cloud_repository))

    builder.add_node("triage_agent", triage_agent)
    builder.add_node("policy_agent", policy_agent)
    builder.add_node("refund_agent", refund_node)
    builder.add_node("refund_persistence", refund_persistence)
    builder.add_node("response_agent", response_agent)
    builder.add_node("human_approval", HumanApprovalNode(cloud_repository))

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

    builder.add_edge("refund_agent", "refund_persistence")
    builder.add_edge("refund_persistence", "response_agent")
    builder.add_conditional_edges(
        "human_approval",
        route_after_human_approval,
        {
            "response_agent": "response_agent",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "response_agent",
        route_after_response_persistence,
        {
            "human_approval": "human_approval",
            END: END,
        },
    )

    return builder.compile()
