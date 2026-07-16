"""
Live integration test: one real graph run (Azure LLM + GCP main_db) must land
rows in tickets / workflow_runs / agent_handoffs / governance_events, matching
Derrick's shapes, with non-null token counts.

Marked `live` (excluded by default addopts). Teardown deletes this run's own
rows child-first — main_db has no append-only triggers, and the shared DB must
stay tidy for the team.
"""
import json
import os

import mysql.connector
import pytest
from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver

from db import backend

load_dotenv()


def _main_db_conn():
    return mysql.connector.connect(
        host=os.environ["GCP_MYSQL_HOST"],
        user=os.environ["GCP_MYSQL_USER"],
        password=os.environ["GCP_MYSQL_PASSWORD"],
        database=os.environ.get("GCP_MYSQL_DATABASE", "main_db"),
        connection_timeout=int(os.environ.get("GCP_MYSQL_CONNECT_TIMEOUT", "5")),
    )


def _cleanup(trace_id: str, ticket_id: str) -> None:
    """Delete this test's rows, child tables first (FK safety).

    main_db's audit_log / governance_events key on trace_id only (no ticket_id
    column there)."""
    conn = _main_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM agent_handoffs WHERE trace_id=%s OR ticket_id=%s",
                (trace_id, ticket_id))
    cur.execute("DELETE FROM governance_events WHERE trace_id=%s", (trace_id,))
    cur.execute("DELETE FROM audit_log WHERE trace_id=%s", (trace_id,))
    cur.execute("DELETE FROM workflow_runs WHERE trace_id=%s OR ticket_id=%s",
                (trace_id, ticket_id))
    cur.execute("DELETE FROM tickets WHERE ticket_id=%s", (ticket_id,))
    conn.commit()
    cur.close()
    conn.close()


@pytest.mark.live
def test_live_buggy_join_block_end_to_end(monkeypatch):
    """The ASI07 leak demo on the real shared DB: buggy JOIN leaks another
    POL customer's contact → ownership block → paused_governance, with the verdict
    row (raw offending_content) landing on main_db."""
    monkeypatch.setenv("DB_BACKEND", "mysql")
    backend.reset_backend_cache()
    if backend.active_backend() != "mysql":
        pytest.skip("GCP main_db unreachable")

    from graph import build_graph

    app = build_graph(checkpointer=MemorySaver())
    result = app.invoke(
        {"user_id": "CUST-POL-001",
         "message": "ORD-POL-001 arrived cracked, refund please.",
         "buggy_db": True},
        {"configurable": {"thread_id": "live-buggy-test"}},
    )
    trace_id, ticket_id = result["trace_id"], result["ticket_id"]

    try:
        assert result["next_agent"] == "human_approval"
        assert result["governance_result"]["failed_check"] == "ownership"

        conn = _main_db_conn()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM governance_events WHERE trace_id=%s", (trace_id,))
        gov = cur.fetchone()
        assert gov["interceptor_action"] == "block"
        assert gov["owasp_category"] == "ASI07"
        assert gov["offending_content"].startswith("CUST-POL-")  # raw leaked id
        assert gov["offending_content"] != "CUST-POL-001"        # not the requester

        cur.execute("SELECT status, current_agent FROM workflow_runs WHERE trace_id=%s",
                    (trace_id,))
        run = cur.fetchone()
        assert run == {"status": "paused_governance", "current_agent": "human_approval"}

        cur.execute("SELECT output_json FROM agent_handoffs WHERE trace_id=%s", (trace_id,))
        out = json.loads(cur.fetchone()["output_json"])
        assert out["governance_result"]["failed_check"] == "ownership"  # block-path extra
        cur.close()
        conn.close()
    finally:
        _cleanup(trace_id, ticket_id)
        backend.reset_backend_cache()


@pytest.mark.live
def test_live_multi_turn_single_ticket(monkeypatch):
    """Two-turn conversation on one thread: clarification, then completion.
    One ticket row backfilled, tokens accumulated across both turns."""
    monkeypatch.setenv("DB_BACKEND", "mysql")
    backend.reset_backend_cache()
    if backend.active_backend() != "mysql":
        pytest.skip("GCP main_db unreachable")

    from graph import build_graph

    app = build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "live-multiturn-test"}}
    turn1 = app.invoke(
        {"user_id": "CUST-POL-002",
         "message": "My headphones never arrived, I want my money back."},
        cfg,
    )
    assert turn1["awaiting_order_id"] is True

    turn2 = app.invoke(
        {"user_id": "CUST-POL-002", "message": "It's ORD-POL-002."}, cfg)
    trace_id, ticket_id = turn2["trace_id"], turn2["ticket_id"]

    try:
        assert turn2["ticket_id"] == turn1["ticket_id"]  # one ticket, two turns
        assert turn2["triage_output"]["customer_request"]["refund_reason"] \
            == "not_delivered_within_timeframe"
        # tokens include turn 1's call too
        assert turn2["llm_input_tokens"] > turn1["llm_input_tokens"] > 0

        conn = _main_db_conn()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) AS n FROM tickets WHERE ticket_id=%s", (ticket_id,))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT status, raw_text FROM tickets WHERE ticket_id=%s", (ticket_id,))
        ticket = cur.fetchone()
        assert ticket["status"] == "triaged"
        assert "headphones" in ticket["raw_text"]  # raw_text = FIRST message
        cur.execute("SELECT input_tokens FROM agent_handoffs WHERE trace_id=%s", (trace_id,))
        assert cur.fetchone()["input_tokens"] == turn2["llm_input_tokens"]
        cur.close()
        conn.close()
    finally:
        _cleanup(trace_id, ticket_id)
        backend.reset_backend_cache()


@pytest.mark.live
def test_live_end_to_end_writes_main_db_rows(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "mysql")
    backend.reset_backend_cache()
    if backend.active_backend() != "mysql":
        pytest.skip("GCP main_db unreachable")

    from graph import build_graph

    app = build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "live-pipeline-test"}}
    result = app.invoke(
        {"user_id": "CUST-POL-001",
         "message": "My order ORD-POL-001 arrived damaged, please refund."},
        cfg,
    )
    trace_id, ticket_id = result["trace_id"], result["ticket_id"]

    try:
        assert result["next_agent"] == "policy_agent"

        conn = _main_db_conn()
        cur = conn.cursor(dictionary=True)

        cur.execute("SELECT * FROM tickets WHERE ticket_id=%s", (ticket_id,))
        ticket = cur.fetchone()
        assert ticket is not None
        assert ticket["status"] == "triaged"
        assert ticket["refund_reason"] == "damaged"
        assert ticket["customer_id"] == "CUST-POL-001"

        cur.execute("SELECT * FROM workflow_runs WHERE trace_id=%s", (trace_id,))
        run = cur.fetchone()
        assert run["status"] == "running"
        assert run["current_agent"] == "policy_agent"
        assert run["policy_version"] == "v1.0"

        cur.execute("SELECT * FROM agent_handoffs WHERE trace_id=%s", (trace_id,))
        handoff = cur.fetchone()
        assert handoff["from_agent"] == "triage_agent"
        assert handoff["to_agent"] == "policy_agent"
        out = json.loads(handoff["output_json"])
        assert set(out) == {"case", "order_facts", "customer_request",
                            "awaiting_order_id", "conversation_history",
                            "clarification_question"}  # Derrick's shape (allow path)
        assert out["order_facts"]["order_id"] == "ORD-POL-001"
        assert handoff["input_tokens"] and handoff["input_tokens"] > 0
        assert handoff["output_tokens"] and handoff["output_tokens"] > 0

        cur.execute("SELECT * FROM governance_events WHERE trace_id=%s", (trace_id,))
        gov = cur.fetchone()
        assert gov["interceptor_action"] == "allow"
        assert gov["owasp_category"] == "ASI07"

        cur.execute("SELECT COUNT(*) AS n FROM audit_log WHERE trace_id=%s", (trace_id,))
        assert cur.fetchone()["n"] >= 6  # run_started ... handoff_ready

        cur.close()
        conn.close()
    finally:
        _cleanup(trace_id, ticket_id)
        backend.reset_backend_cache()
