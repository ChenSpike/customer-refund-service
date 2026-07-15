"""
LangGraph assembly for the Customer Refund Service (Jenny's slice + stubs).

Flow:

    START ─▶ triage ─┬─(awaiting_order_id)─────────────▶ END   (ask the user, re-run next turn)
                     └─(triage_output ready)─▶ governance ─┬─allow─▶ policy_agent ─▶ END
                                                           └─block─▶ human_approval ─▶ END

`triage_node` and `intercept_triage_output` are the real implementations.
`policy_agent_node` (Derrick) and `human_approval_node` are stubs here so the
graph compiles and runs end-to-end; swap them for the real nodes at integration.

Multi-turn: the graph ends after a clarification (no interrupt node). The app
re-invokes with the user's reply on the same `thread_id` (= ticket_id); the
checkpointer restores conversation_history / trace_id / ticket_id.
"""
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agents.triage_agent import triage_node
from governance.interceptor import intercept_triage_output
from state import TriageState


# ── Teammate stubs (replace at integration) ──────────────────────────────────

def policy_agent_node(state: TriageState) -> dict:
    """STUB — Derrick's Policy Agent. Consumes state['triage_output']."""
    return {"policy_decision": {"status": "stub", "note": "policy_agent not wired yet"}}


def human_approval_node(state: TriageState) -> dict:
    """STUB — human-in-the-loop review. Consumes state['governance_result']."""
    gr = state.get("governance_result", {})
    return {"human_review": {"status": "pending",
                             "reason": gr.get("failed_check", "unknown")}}


# ── Routing ──────────────────────────────────────────────────────────────────

def route_after_triage(state: TriageState) -> str:
    """
    - content_filter_blocked → triage already produced a block verdict
      (Azure filtered a prompt injection); go straight to human review.
    - awaiting_order_id (Case A/B) → end this turn, return the question to the user.
    - otherwise (Case C) → governance. Sending an unfinished turn to governance
      would fail schema_validation on an empty order_lookup_result.
    """
    if state.get("content_filter_blocked"):
        return "human_approval"
    return END if state.get("awaiting_order_id") else "governance"


# ── Graph assembly ───────────────────────────────────────────────────────────

def build_graph(checkpointer=None):
    """Compile the pipeline. Pass a checkpointer (e.g. MemorySaver()) to keep
    conversation state across turns on a thread_id."""
    graph = StateGraph(TriageState)
    graph.add_node("triage", triage_node)
    graph.add_node("governance", intercept_triage_output)
    graph.add_node("policy_agent", policy_agent_node)
    graph.add_node("human_approval", human_approval_node)

    graph.add_edge(START, "triage")
    graph.add_conditional_edges(
        "triage", route_after_triage,
        {"governance": "governance", "human_approval": "human_approval", END: END},
    )
    graph.add_conditional_edges(
        "governance", lambda s: s["next_agent"],
        {"policy_agent": "policy_agent", "human_approval": "human_approval"},
    )
    graph.add_edge("policy_agent", END)
    graph.add_edge("human_approval", END)

    return graph.compile(checkpointer=checkpointer)


# ── Demo runner (real LLM + real backend) ────────────────────────────────────

if __name__ == "__main__":
    import uuid

    from db.seed import seed

    seed()  # ensure the local SQLite fallback is populated
    app = build_graph(checkpointer=MemorySaver())

    # thread_id ties multiple turns of one ticket together.
    # POL IDs: on the mysql backend, tickets.customer_id must reference an
    # existing main_db customer; on the sqlite fallback these IDs don't exist
    # locally, so this smoke script is meant for the GCP path.
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = app.invoke(
        {"user_id": "CUST-POL-001", "message": "My order ORD-POL-001 arrived broken."},
        cfg,
    )
    print("next_agent      :", result.get("next_agent"))
    print("governance      :", result.get("governance_result", {}).get("status"))
    print("refund_reason   :",
          result.get("triage_output", {}).get("customer_request", {}).get("refund_reason"))
    print("policy_decision :", result.get("policy_decision"))
