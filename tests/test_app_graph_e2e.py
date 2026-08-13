from __future__ import annotations

import app.graph as app_graph
from agents.policy.tests.factories import make_input, make_policy_result
from tests.test_policy_persistence import FakeAzureClient, RecordingRepository


_ORIGINAL_BUILD_POLICY_AGENT_GRAPH = app_graph.build_policy_agent_graph


def _response_completed(_state):
    return {
        "current_stage": "response_persistence",
        "response_result": {
            "response": {"body": "Your refund has been processed.", "tone": "empathetic"},
            "final_outcome": "approved",
            "workflow_status": "completed",
        },
        "response_governance_result": {"status": "allow", "findings": []},
        "response_handoff": "end",
        "response_persistence_result": {"trace_id": "TRACE-E2E-001", "next_agent": "end"},
        "final_outcome": "approved",
        "workflow_status": "completed",
    }


def _response_need_info(state):
    return {
        "current_stage": "response_persistence",
        "response_result": {
            "response": {"body": state["clarification_question"], "tone": "empathetic"},
            "final_outcome": "need_info",
            "workflow_status": "waiting_user",
        },
        "response_governance_result": {"status": "allow", "findings": []},
        "response_handoff": "end",
        "response_persistence_result": {
            "handoff_id": "RESP-HANDOFF-002",
            "trace_id": state["trace_id"],
            "next_agent": "end",
        },
        "final_outcome": "need_info",
        "workflow_status": "waiting_user",
    }


def _triage_to_human_approval(_state):
    return {
        "trace_id": "TRACE-E2E-003",
        "ticket_id": "TICKET-E2E-003",
        "triage_output": {},
        "triage_governance_result": {"status": "block", "findings": [{"name": "data_leakage"}]},
        "triage_handoff": "human_review",
        "triage_persistence_result": {
            "handoff_id": "TRIAGE-HANDOFF-003",
            "trace_id": "TRACE-E2E-003",
            "next_agent": "human_approval",
        },
        "review_trigger_stage": "triage",
        "review_trigger_reason": "governance_block",
    }


def _response_after_human_review(_state):
    return {
        "current_stage": "response_persistence",
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
            "handoff_id": "RESP-HANDOFF-003",
            "trace_id": "TRACE-E2E-003",
            "next_agent": "end",
        },
        "final_outcome": "approved",
        "workflow_status": "completed",
    }


def _triage_to_policy(_state):
    policy_input = make_input()
    return {
        "trace_id": policy_input.case.trace_id,
        "ticket_id": policy_input.case.ticket_id,
        "triage_output": policy_input.model_dump(mode="json"),
        "triage_governance_result": {"status": "allow", "findings": []},
        "triage_handoff": "policy",
        "triage_persistence_result": {
            "handoff_id": "TRIAGE-HANDOFF-001",
            "trace_id": policy_input.case.trace_id,
            "next_agent": "policy",
        },
        "requested_order_id": policy_input.order_facts.order_id,
        "order_lookup_result": {
            "order_id": policy_input.order_facts.order_id,
            "currency": policy_input.customer_request.currency,
        },
    }


def _triage_to_response(_state):
    return {
        "trace_id": "TRACE-E2E-002",
        "ticket_id": "TICKET-E2E-002",
        "triage_output": {},
        "triage_governance_result": {"status": "allow", "findings": []},
        "triage_handoff": "response",
        "triage_persistence_result": {
            "handoff_id": "TRIAGE-HANDOFF-002",
            "trace_id": "TRACE-E2E-002",
            "next_agent": "response_agent",
        },
        "user_action_required": True,
        "clarification_question": "Could you share your order ID?",
    }


def test_app_graph_e2e_policy_refund_response_path(monkeypatch) -> None:
    policy_input = make_input()
    repository = RecordingRepository()

    monkeypatch.setattr(app_graph, "build_triage_agent_graph", lambda **_kwargs: _triage_to_policy)
    monkeypatch.setattr(
        app_graph,
        "build_policy_agent_graph",
        lambda _client, **_kwargs: _ORIGINAL_BUILD_POLICY_AGENT_GRAPH(
            FakeAzureClient(make_policy_result(policy_input)),
            **_kwargs,
        ),
    )
    monkeypatch.setattr(
        app_graph,
        "refund_node",
        lambda _state: {
            "current_stage": "refund_agent",
            "refund_result": {"status": "success", "message": "Refund completed."},
            "final_outcome": "approved",
            "workflow_status": "running",
        },
    )
    monkeypatch.setattr(
        app_graph,
        "build_response_agent_graph",
        lambda **_kwargs: _response_completed,
    )

    graph = app_graph.build_graph(client=object(), repository=repository)

    result = graph.invoke({"message": "My item arrived damaged"})

    assert result["triage_handoff"] == "policy"
    assert result["triage_persistence_result"]["next_agent"] == "policy"
    assert result["policy_handoff"] == "refund"
    assert result["policy_persistence_result"]["next_agent"] == "refund_agent"
    assert result["refund_result"]["status"] == "success"
    assert result["response_governance_result"]["status"] == "allow"
    assert result["workflow_status"] == "completed"
    assert len(repository.persisted) == 1


def test_app_graph_e2e_triage_response_path(monkeypatch) -> None:
    repository = RecordingRepository()

    monkeypatch.setattr(app_graph, "build_triage_agent_graph", lambda **_kwargs: _triage_to_response)
    monkeypatch.setattr(
        app_graph,
        "build_policy_agent_graph",
        lambda _client, **_kwargs: _ORIGINAL_BUILD_POLICY_AGENT_GRAPH(
            FakeAzureClient(make_policy_result(make_input())),
            **_kwargs,
        ),
    )
    monkeypatch.setattr(
        app_graph,
        "build_response_agent_graph",
        lambda **_kwargs: _response_need_info,
    )

    graph = app_graph.build_graph(client=object(), repository=repository)

    result = graph.invoke({"message": "I need help"})

    assert result["triage_handoff"] == "response"
    assert result["triage_persistence_result"]["next_agent"] == "response_agent"
    assert "policy_persistence_result" not in result
    assert result["response_result"]["final_outcome"] == "need_info"
    assert result["response_governance_result"]["status"] == "allow"
    assert result["workflow_status"] == "waiting_user"
    assert repository.persisted == []


def test_app_graph_e2e_human_approval_returns_to_response(monkeypatch) -> None:
    repository = RecordingRepository()

    monkeypatch.setattr(app_graph, "build_triage_agent_graph", lambda **_kwargs: _triage_to_human_approval)
    monkeypatch.setattr(
        app_graph,
        "build_policy_agent_graph",
        lambda _client, **_kwargs: _ORIGINAL_BUILD_POLICY_AGENT_GRAPH(
            FakeAzureClient(make_policy_result(make_input())),
            **_kwargs,
        ),
    )
    monkeypatch.setattr(
        app_graph,
        "build_response_agent_graph",
        lambda **_kwargs: _response_after_human_review,
    )

    graph = app_graph.build_graph(client=object(), repository=repository)

    result = graph.invoke({"message": "Please review this case"})

    assert result["human_review"]["status"] == "approved"
    assert result["response_result"]["final_outcome"] == "approved"
    assert result["response_persistence_result"]["next_agent"] == "end"
    assert result["workflow_status"] == "completed"