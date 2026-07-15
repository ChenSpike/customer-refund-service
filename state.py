from typing import TypedDict, Optional


class TriageState(TypedDict, total=False):
    # --- Input ---
    # Two accepted shapes (see agents.triage_agent._inputs):
    #   flat:   user_id / message / buggy_db at top level
    #   nested: case = {"user_id", "message", "buggy_db"}  (Derrick's harness)
    user_id: str
    message: str
    buggy_db: bool  # toggle for demo scenario 2
    case: dict      # optional nested input shape

    # --- Correlation IDs (generated at run start so ALL audit events correlate) ---
    # ticket_id persists across turns of one support ticket. trace_id is minted
    # on the first turn; with a checkpointer it persists across turns too (one
    # workflow_runs row per ticket in practice).
    trace_id: str
    ticket_id: str

    # --- LLM usage (accumulated across the run's Azure calls; written to
    #     agent_handoffs.input_tokens / output_tokens) ---
    llm_input_tokens: int
    llm_output_tokens: int

    # --- Conversation state ---
    # True when the agent needs more info from the user before it can proceed
    awaiting_order_id: bool
    clarification_question: str  # the question to send back to the user
    # Accumulated Responses API items (everything except the system prompt)
    # Persisted across turns so the agent remembers prior context
    conversation_history: list

    # --- Tool output ---
    # Raw result from Order_Database_Lookup, inspected by the governance interceptor
    order_lookup_result: dict

    # --- Triage output (= Policy Agent input schema) ---
    triage_output: dict

    # --- Governance ---
    governance_result: dict
    # Set by triage_node when Azure's content filter blocks the message
    # (prompt injection / jailbreak) — routes straight to human_approval.
    content_filter_blocked: bool
    # Prompt-injection / safety flag for the ticket (shared tickets.injection_flag).
    injection_flag: bool

    # --- Pipeline routing ---
    next_agent: str  # "policy_agent" | "human_approval" | "end"

    # --- Downstream node outputs (teammate-owned) ---
    # NOTE: LangGraph drops keys not declared on the state schema, so any field
    # a downstream node writes MUST be declared here or it vanishes silently.
    # Real shapes are defined by their owners; placeholders for now.
    policy_decision: dict  # Derrick's Policy Agent output
    human_review: dict     # human-in-the-loop review output
