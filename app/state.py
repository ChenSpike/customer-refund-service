import operator
from typing import Annotated, Any, TypedDict


class GovernanceResult(TypedDict, total=False):
    stage: str
    status: str
    findings: list[dict[str, Any]]
    all_checks: list[dict[str, Any]]
    flags: list[str]


class AppState(TypedDict, total=False):
    # input
    user_id: str
    message: str
    request_context: dict[str, Any]

    # ids
    trace_id: str
    ticket_id: str
    current_stage: str
    workflow_status: str

    # llm usage
    llm_input_tokens: Annotated[int, operator.add]
    llm_output_tokens: Annotated[int, operator.add]
    llm_usage_events: Annotated[list[dict[str, Any]], operator.add]

    # conversation
    conversation_history: list[dict[str, Any]]
    missing_fields: list[str]
    user_action_required: bool
    human_review_required: bool
    final_outcome: str
    requested_order_id: str
    clarification_question: str
    review_trigger_stage: str
    review_trigger_reason: str

    # triage outputs
    order_lookup_result: dict[str, Any]
    triage_output: dict[str, Any]
    triage_handoff: str
    triage_persistence_result: dict[str, Any]

    # governance
    triage_governance_result: GovernanceResult
    policy_governance_result: GovernanceResult
    response_governance_result: GovernanceResult
    risk_flags: Annotated[list[dict[str, Any]], operator.add]

    # policy outputs
    policy_result: dict[str, Any]
    policy_decision: dict[str, Any]
    policy_context: dict[str, Any]
    policy_handoff: str
    policy_persistence_result: dict[str, Any]

    # downstream placeholders
    refund_result: dict[str, Any]
    response_result: dict[str, Any]
    response_handoff: str
    response_persistence_result: dict[str, Any]
    human_review: dict[str, Any]

    # observability
    errors: Annotated[list[dict[str, Any]], operator.add]
    audit_trail: Annotated[list[dict[str, Any]], operator.add]
    snapshots: Annotated[list[dict[str, Any]], operator.add]

