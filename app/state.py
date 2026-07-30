import operator
from typing import Annotated, TypedDict


class AppState(TypedDict, total=False):
    # input
    user_id: str
    message: str
    request_context: dict
    buggy_db: bool

    # ids
    trace_id: str
    ticket_id: str
    current_stage: str
    workflow_status: str

    # llm usage
    llm_input_tokens: Annotated[int, operator.add]
    llm_output_tokens: Annotated[int, operator.add]

    # conversation
    conversation_history: list
    missing_fields: list[str]
    user_action_required: bool
    human_review_required: bool
    final_outcome: str
    requested_order_id: str
    clarification_question: str

    # triage outputs
    order_lookup_result: dict
    triage_output: dict

    # governance
    governance_result: dict
    risk_flags: dict

    # policy outputs
    policy_decision: dict
    policy_context: dict

    # downstream placeholders
    refund_result: dict
    response_result: dict
    human_review: dict

    # observability
    errors: Annotated[list[dict], operator.add]
    audit_trail: Annotated[list[dict], operator.add]
    snapshots: Annotated[list[dict], operator.add]