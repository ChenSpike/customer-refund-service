"""
tests/test_response_node.py

Response agent node + ResponseGovernanceNode unit tests.
Offline — no LLM, no DB. Uses the same FakeAzureClient pattern as
test_triage_node.py.
"""

import pytest

from agents.response import node as response_node_module
from agents.response.node import response_node
from agents.response.governance_node import ResponseGovernanceNode
from governance import BaseGovernanceNode
from tests.fakes import FakeAzureClient, FakeResponse, MessageItem


# ── helpers ───────────────────────────────────────────────────────────────────

def _install(monkeypatch, queue):
    monkeypatch.setattr(response_node_module, "client", FakeAzureClient(queue))


def _approve_state(draft="Your refund has been processed. Customer Support Team") -> dict:
    return {
        "trace_id":  "TRACE-1",
        "ticket_id": "TK-001",
        "user_id":   "CUST-001",
        "policy_decision": {
            "decision":              "approve",
            "refund_amount":         79.99,
            "reason":                "Defective item within 30 days.",
            "customer_safe_summary": "We have approved your refund.",
            "missing_info_to_request": [],
        },
        "refund_result": {
            "status":   "success",
            "amount":   79.99,
            "currency": "USD",
            "message":  "Refund processed successfully.",
        },
    }


def _deny_state() -> dict:
    return {
        "trace_id":  "TRACE-2",
        "ticket_id": "TK-002",
        "user_id":   "CUST-002",
        "policy_decision": {
            "decision":              "deny",
            "refund_amount":         0.0,
            "reason":                "Outside 30-day return window.",
            "customer_safe_summary": "We cannot process this refund.",
            "missing_info_to_request": [],
        },
        "refund_result": {},
    }


def _need_info_state() -> dict:
    return {
        "trace_id":             "TRACE-3",
        "ticket_id":            "TK-003",
        "user_id":              "CUST-003",
        "user_action_required": True,
        "clarification_question": "Could you please provide your order ID?",
        "policy_decision": {},
        "refund_result":   {},
    }


# ── response_node: happy paths ────────────────────────────────────────────────

def test_response_node_approve_success(monkeypatch):
    draft = "Your refund of $79.99 has been processed. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(20, 15))])

    out = response_node(_approve_state())

    assert out["current_stage"] == "response_agent"
    assert out["final_outcome"]  == "approved"
    assert out["workflow_status"] == "completed"
    body = out["response_result"]["response"]["body"]
    assert body == draft
    assert out["response_result"]["response"]["tone"] == "empathetic"
    assert out["llm_input_tokens"]  == 20
    assert out["llm_output_tokens"] == 15


def test_response_node_deny(monkeypatch):
    draft = "Unfortunately we cannot process your refund. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(15, 10))])

    out = response_node(_deny_state())

    assert out["final_outcome"]  == "denied"
    assert out["workflow_status"] == "completed"
    assert out["response_result"]["response"]["tone"] == "formal"


def test_response_node_need_info_no_llm_call(monkeypatch):
    """Missing order ID path — LLM is still called to phrase the question politely."""
    draft = "Could you please share your order ID so we can help?"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(10, 8))])

    out = response_node(_need_info_state())

    assert out["final_outcome"]  == "need_info"
    assert out["workflow_status"] == "waiting_user"


def test_response_node_partial_refund(monkeypatch):
    draft = "We have approved a partial refund of $40.00. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(18, 12))])

    state = {
        "trace_id": "TRACE-4", "ticket_id": "TK-004", "user_id": "CUST-004",
        "policy_decision": {
            "decision": "partial_refund", "refund_amount": 40.0,
            "reason": "Item used but returned within window.",
            "customer_safe_summary": "Partial refund approved.",
            "missing_info_to_request": [],
        },
        "refund_result": {"status": "success", "amount": 40.0},
    }
    out = response_node(state)

    assert out["final_outcome"]  == "partial_refund"
    assert out["workflow_status"] == "completed"
    assert out["response_result"]["response"]["tone"] == "neutral"


def test_response_node_refund_failed(monkeypatch):
    draft = "We apologise — our team will follow up within 24 hours. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(12, 10))])

    state = {
        "trace_id": "TRACE-5", "ticket_id": "TK-005", "user_id": "CUST-005",
        "policy_decision": {"decision": "approve", "refund_amount": 79.99,
                            "customer_safe_summary": "", "missing_info_to_request": []},
        "refund_result": {"status": "failed", "amount": 79.99},
    }
    out = response_node(state)

    assert out["final_outcome"]  == "refund_failed"
    assert out["workflow_status"] == "completed"


def test_response_node_token_usage_events(monkeypatch):
    draft = "Your refund has been processed. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(20, 15))])

    out = response_node(_approve_state())

    assert out["llm_usage_events"] == [
        {"agent": "response_agent", "input_tokens": 20, "output_tokens": 15}
    ]


def test_response_node_content_checks_placeholders(monkeypatch):
    """content_checks are placeholders — governance node validates externally."""
    draft = "Your refund is approved. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(10, 8))])

    out = response_node(_approve_state())
    checks = out["response_result"]["content_checks"]

    assert checks["pii_fields_detected"] == []
    assert checks["forbidden_phrases"]   == []


# ── ResponseGovernanceNode ────────────────────────────────────────────────────

def test_is_base_governance_node():
    assert isinstance(ResponseGovernanceNode(), BaseGovernanceNode)


def test_governance_node_allow_on_clean_draft(monkeypatch):
    draft = "Your refund has been processed. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(20, 15))])

    state = response_node(_approve_state())
    state.update({"trace_id": "TRACE-1", "ticket_id": "TK-001", "user_id": "CUST-001"})

    result = ResponseGovernanceNode()(state)

    assert result["response_governance_result"]["status"] == "allow"
    assert result["current_stage"] == "response_governance"
    assert len(result["audit_trail"]) == 1


def test_governance_node_blocks_on_tool_invocation(monkeypatch):
    """ASI02 — tool-invocation language in draft body triggers forbidden_tool block."""
    malicious = "I will call refund_issuer( order_id=ORD-001 ) now. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(malicious)], usage=(10, 8))])

    state = response_node(_approve_state())
    state.update({"trace_id": "TRACE-1", "ticket_id": "TK-001", "user_id": "CUST-001"})

    result = ResponseGovernanceNode()(state)
    gov    = result["response_governance_result"]

    assert gov["status"] == "block"
    assert any(f["name"] == "forbidden_tool" for f in gov["findings"])


def test_governance_node_blocks_on_pii_in_draft(monkeypatch):
    """ASI07 — email address in draft body triggers pii_risk block."""
    pii_draft = "We noticed a claim from bob@example.com on the same order. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(pii_draft)], usage=(10, 8))])

    state = response_node(_approve_state())
    state.update({"trace_id": "TRACE-1", "ticket_id": "TK-001", "user_id": "CUST-001"})

    result = ResponseGovernanceNode()(state)
    gov    = result["response_governance_result"]

    assert gov["status"] == "block"
    assert any(f["name"] == "pii_risk" for f in gov["findings"])


def test_governance_node_writes_to_response_governance_result_not_governance_result(monkeypatch):
    """Ensures response governance does not overwrite triage/policy governance results."""
    draft = "Your refund is approved. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(10, 8))])

    state = response_node(_approve_state())
    state.update({
        "trace_id": "TRACE-1", "ticket_id": "TK-001", "user_id": "CUST-001",
        "triage_governance_result":  {"status": "allow", "stage": "triage"},
        "policy_governance_result":  {"status": "allow", "stage": "policy"},
    })

    result = ResponseGovernanceNode()(state)

    assert "response_governance_result" in result
    assert "governance_result" not in result
    assert "triage_governance_result" not in result
    assert "policy_governance_result" not in result


# ── checkers directly ─────────────────────────────────────────────────────────

def test_check_response_tool_misuse_allow_on_empty():
    from governance.checkers import check_response_tool_misuse
    assert check_response_tool_misuse({}).status == "allow"


def test_check_response_tool_misuse_allow_on_clean():
    from governance.checkers import check_response_tool_misuse
    state = {"response_result": {"response": {"body": "Your refund is approved."}}}
    assert check_response_tool_misuse(state).status == "allow"


def test_check_response_tool_misuse_block_on_tool_language():
    from governance.checkers import check_response_tool_misuse
    state = {"response_result": {"response": {"body": "I will call refund_issuer( order_id=ORD-001 )"}}}
    result = check_response_tool_misuse(state)
    assert result.status == "block"
    assert result.name == "forbidden_tool"


def test_check_response_pii_allow_on_empty():
    from governance.checkers import check_response_pii
    assert check_response_pii({}).status == "allow"


def test_check_response_pii_allow_on_clean():
    from governance.checkers import check_response_pii
    state = {"response_result": {"response": {"body": "Your refund has been processed."}}}
    assert check_response_pii(state).status == "allow"


def test_check_response_pii_block_on_email():
    from governance.checkers import check_response_pii
    state = {"response_result": {"response": {"body": "We also cc'd bob@example.com on this."}}}
    result = check_response_pii(state)
    assert result.status == "block"
    assert result.name == "pii_risk"
    assert result.evidence["email"] == "bob@example.com"


def test_check_response_pii_block_on_phone():
    from governance.checkers import check_response_pii
    state = {"response_result": {"response": {"body": "Call us at 415-555-0198 to confirm."}}}
    result = check_response_pii(state)
    assert result.status == "block"
    assert result.name == "pii_risk"
    assert "phone" in result.evidence
