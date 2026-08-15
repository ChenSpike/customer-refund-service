from __future__ import annotations

import os
import uuid

import pytest

import app.graph as app_graph
from db.database import GCPRepository


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_POLICY_AGENT_LIVE_TESTS") != "1",
        reason="Set RUN_POLICY_AGENT_LIVE_TESTS=1 to authorize destructive Azure/GCP testing.",
    ),
]


def _seed_workflow(repository: GCPRepository, *, trace_id: str, ticket_id: str, message: str) -> None:
    connection = repository._connect()
    try:
        connection.start_transaction()
        cursor = connection.cursor()
        cursor.execute("DELETE FROM audit_log WHERE trace_id = %s", (trace_id,))
        cursor.execute("DELETE FROM agent_handoffs WHERE trace_id = %s", (trace_id,))
        cursor.execute("DELETE FROM governance_events WHERE trace_id = %s", (trace_id,))
        cursor.execute("DELETE FROM human_approvals WHERE trace_id = %s", (trace_id,))
        cursor.execute("DELETE FROM policy_review_events WHERE trace_id = %s", (trace_id,))
        cursor.execute("DELETE FROM workflow_runs WHERE trace_id = %s", (trace_id,))
        cursor.execute("DELETE FROM tickets WHERE ticket_id = %s", (ticket_id,))
        cursor.execute(
            """
            INSERT INTO tickets (ticket_id, customer_id, raw_text)
            VALUES (%s, %s, %s)
            """,
            (ticket_id, "CUST-POL-001", message),
        )
        cursor.execute(
            """
            INSERT INTO workflow_runs (trace_id, ticket_id, status, current_agent, policy_version)
            VALUES (%s, %s, 'running', 'triage_agent', 'v1.0')
            """,
            (trace_id, ticket_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _fetch_handoffs(repository: GCPRepository, trace_id: str) -> list[tuple[str, str, str, str]]:
    connection = repository._connect()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT handoff_id, from_agent, to_agent, ticket_id
            FROM agent_handoffs
            WHERE trace_id = %s
            ORDER BY CAST(handoff_id AS UNSIGNED), created_at
            """,
            (trace_id,),
        )
        return list(cursor.fetchall())
    finally:
        connection.close()


def _fetch_audit_events(repository: GCPRepository, trace_id: str) -> list[tuple[str, str]]:
    connection = repository._connect()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT agent, event_type
            FROM audit_log
            WHERE trace_id = %s
            ORDER BY log_id
            """,
            (trace_id,),
        )
        return list(cursor.fetchall())
    finally:
        connection.close()


def _fetch_workflow(repository: GCPRepository, trace_id: str) -> tuple[str, str]:
    connection = repository._connect()
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT status, current_agent FROM workflow_runs WHERE trace_id = %s",
            (trace_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


def test_live_triage_to_response_persists_backend_rows(monkeypatch) -> None:
    repository = GCPRepository.from_env()
    trace_id = f"TRACE-LIVE-TRIAGE-{uuid.uuid4().hex[:8].upper()}"
    ticket_id = f"TICKET-LIVE-TRIAGE-{uuid.uuid4().hex[:8].upper()}"
    message = "I need help with my refund but I forgot my order ID."
    _seed_workflow(repository, trace_id=trace_id, ticket_id=ticket_id, message=message)

    monkeypatch.setattr(
        app_graph,
        "build_triage_agent_graph",
        lambda **_kwargs: lambda _state: {
            "trace_id": trace_id,
            "ticket_id": ticket_id,
            "message": message,
            "user_id": "CUST-POL-001",
            "triage_output": {},
            "triage_governance_result": {"status": "allow", "findings": []},
            "triage_handoff": "response",
            "triage_persistence_result": {
                "handoff_id": "stub-overwritten",
                "trace_id": trace_id,
                "next_agent": "response_agent",
            },
            "user_action_required": True,
            "clarification_question": "Could you share your order ID?",
        },
    )
    monkeypatch.setattr(
        app_graph,
        "build_response_agent_graph",
        lambda **_kwargs: lambda _state: {
            "trace_id": trace_id,
            "ticket_id": ticket_id,
            "message": message,
            "user_id": "CUST-POL-001",
            "response_result": {
                "response": {"body": "Could you share your order ID?", "tone": "empathetic"},
                "final_outcome": "need_info",
                "workflow_status": "waiting_user",
            },
            "response_governance_result": {"status": "allow", "findings": []},
            "response_handoff": "end",
            "response_persistence_result": {
                "handoff_id": "stub-overwritten",
                "trace_id": trace_id,
                "next_agent": "end",
            },
            "workflow_status": "waiting_user",
            "final_outcome": "need_info",
        },
    )

    result = app_graph.build_graph(client=object(), repository=repository).invoke({"message": message})

    handoffs = _fetch_handoffs(repository, trace_id)
    assert [(from_agent, to_agent, stored_ticket_id) for _, from_agent, to_agent, stored_ticket_id in handoffs] == [
        ("triage_agent", "response_agent", ticket_id),
        ("response_agent", "end", ticket_id),
    ]
    assert result["triage_persistence_result"]["handoff_id"] == handoffs[0][0]
    assert result["response_persistence_result"]["handoff_id"] == handoffs[1][0]
    assert _fetch_audit_events(repository, trace_id) == [
        ("triage_agent", "triage_agent_evaluated"),
        ("response_agent", "response_agent_evaluated"),
    ]
    assert _fetch_workflow(repository, trace_id) == ("waiting_user", "triage_agent")


def test_live_triage_human_review_then_response_persists_backend_rows(monkeypatch) -> None:
    repository = GCPRepository.from_env()
    trace_id = f"TRACE-LIVE-HUMAN-{uuid.uuid4().hex[:8].upper()}"
    ticket_id = f"TICKET-LIVE-HUMAN-{uuid.uuid4().hex[:8].upper()}"
    message = "Please review this refund case manually."
    _seed_workflow(repository, trace_id=trace_id, ticket_id=ticket_id, message=message)

    monkeypatch.setattr(
        app_graph,
        "build_triage_agent_graph",
        lambda **_kwargs: lambda _state: {
            "trace_id": trace_id,
            "ticket_id": ticket_id,
            "message": message,
            "user_id": "CUST-POL-001",
            "triage_output": {},
            "triage_governance_result": {"status": "block", "findings": [{"flag": "pii_risk"}]},
            "triage_handoff": "human_review",
            "triage_persistence_result": {
                "handoff_id": "stub-overwritten",
                "trace_id": trace_id,
                "next_agent": "human_approval",
            },
            "review_trigger_stage": "triage",
            "review_trigger_reason": "governance_block",
        },
    )
    monkeypatch.setattr(
        app_graph,
        "build_response_agent_graph",
        lambda **_kwargs: lambda _state: {
            "trace_id": trace_id,
            "ticket_id": ticket_id,
            "message": message,
            "user_id": "CUST-POL-001",
            "human_review": {
                "status": "approved",
                "approved_next_agent": "refund_agent",
                "reason": "manual approval",
            },
            "response_result": {
                "response": {
                    "body": "Our review team approved your request and your refund is being processed.",
                    "tone": "empathetic",
                },
                "final_outcome": "approved",
                "workflow_status": "completed",
            },
            "response_governance_result": {"status": "allow", "findings": []},
            "response_handoff": "end",
            "response_persistence_result": {
                "handoff_id": "stub-overwritten",
                "trace_id": trace_id,
                "next_agent": "end",
            },
            "workflow_status": "completed",
            "final_outcome": "approved",
        },
    )

    result = app_graph.build_graph(client=object(), repository=repository).invoke({"message": message})

    handoffs = _fetch_handoffs(repository, trace_id)
    assert [(from_agent, to_agent, stored_ticket_id) for _, from_agent, to_agent, stored_ticket_id in handoffs] == [
        ("triage_agent", "human_approval", ticket_id),
        ("response_agent", "end", ticket_id),
    ]
    assert result["triage_persistence_result"]["handoff_id"] == handoffs[0][0]
    assert result["response_persistence_result"]["handoff_id"] == handoffs[1][0]
    assert _fetch_audit_events(repository, trace_id) == [
        ("triage_agent", "triage_agent_evaluated"),
        ("response_agent", "response_agent_evaluated"),
    ]
    assert _fetch_workflow(repository, trace_id) == ("completed", "completed")
