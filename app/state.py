from typing import TypedDict


class AppState(TypedDict, total=False):
    # input
    user_id: str
    message: str
    buggy_db: bool
    case: dict

    # ids
    trace_id: str
    ticket_id: str

    # llm usage
    llm_input_tokens: int
    llm_output_tokens: int

    # conversation
    conversation_history: list
    awaiting_order_id: bool
    awaiting_info: bool
    clarification_question: str

    # triage outputs
    order_lookup_result: dict
    triage_output: dict

    # governance
    governance_result: dict
    content_filter_blocked: bool
    injection_flag: bool

    # policy outputs
    policy_decision: dict

    # routing
    next_agent: str

    # downstream placeholders
    refund_result: dict
    response_result: dict
    human_review: dict