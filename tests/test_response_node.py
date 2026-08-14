"""
tests/test_response_node.py

Response agent node + ResponseGovernanceNode unit tests.
Offline — no LLM, no DB. Uses the same FakeAzureClient pattern as
test_triage_node.py.
"""

import pytest

from agents.response import node as response_node_module
from agents.response.node import (
    _build_prompt,
    build_response_payload,
    response_node,
    validate_response_semantics,
)
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


def _approved_human_review_state() -> dict:
    return {
        "trace_id": "TRACE-H-1",
        "ticket_id": "TK-H-001",
        "user_id": "CUST-H-001",
        "human_review": {
            "status": "approved",
            "approved_next_agent": "refund_agent",
            "reason": "manual approval",
        },
        "policy_decision": {"decision": "manual_review"},
        "refund_result": {},
    }


def _rejected_human_review_state() -> dict:
    return {
        "trace_id": "TRACE-H-2",
        "ticket_id": "TK-H-002",
        "user_id": "CUST-H-002",
        "human_review": {
            "status": "rejected",
            "rejected_next_agent": "response_agent",
            "reason": "manual rejection",
        },
        "policy_decision": {"decision": "manual_review"},
        "refund_result": {},
    }


def _outcome_state(outcome: str) -> dict:
    if outcome == "approved":
        return {
            "policy_decision": {"decision": "approve"},
            "refund_result": {"status": "success"},
        }
    if outcome == "partial_refund":
        return {
            "policy_decision": {"decision": "partial_refund"},
            "refund_result": {"status": "success"},
        }
    if outcome == "denied":
        return {"policy_decision": {"decision": "deny"}}
    if outcome == "need_info":
        return {"policy_decision": {"decision": "request_info"}}
    if outcome == "manual_review":
        return {"policy_decision": {"decision": "manual_review"}}
    if outcome == "refund_failed":
        return {
            "policy_decision": {"decision": "approve"},
            "refund_result": {"status": "failed"},
        }
    raise AssertionError(f"unsupported test outcome: {outcome}")


# ── response_node: happy paths ────────────────────────────────────────────────

def test_response_node_approve_success(monkeypatch):
    draft = "Your refund of $79.99 has been processed. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(20, 15))])

    out = response_node(_approve_state())

    assert out["current_stage"] == "response_agent"
    assert out["final_outcome"]  == "approved"
    assert out["workflow_status"] == "completed"
    body = out["response_result"]["response"]["body"]
    assert body.endswith(draft)
    assert "Your refund request has been approved." in body
    assert out["response_result"]["content_checks"]["outcome_anchor_inserted"] is True
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


def test_response_node_after_human_approval_completes(monkeypatch):
    draft = "Our review team approved your request and your refund is being processed. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(16, 11))])

    out = response_node(_approved_human_review_state())

    assert out["final_outcome"] == "approved"
    assert out["workflow_status"] == "completed"
    assert out["response_result"]["response"]["body"].endswith(draft)


def test_response_node_after_human_rejection_completes(monkeypatch):
    draft = "Our review team completed the review and we cannot approve the refund. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(16, 11))])

    out = response_node(_rejected_human_review_state())

    assert out["final_outcome"] == "denied"
    assert out["workflow_status"] == "completed"
    assert out["response_result"]["response"]["body"].endswith(draft)


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


def test_response_node_does_not_hide_azure_failure(monkeypatch):
    _install(monkeypatch, [RuntimeError("service unavailable")])

    with pytest.raises(RuntimeError, match="Azure Response Agent request failed"):
        response_node(_approve_state())


def test_response_node_content_checks_placeholders(monkeypatch):
    """PII/tool checks remain external while semantic checks are deterministic."""
    draft = "Your refund is approved. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(10, 8))])

    out = response_node(_approve_state())
    checks = out["response_result"]["content_checks"]

    assert checks["pii_fields_detected"] == []
    assert checks["forbidden_phrases"]   == []


@pytest.mark.parametrize(
    ("outcome", "anchor"),
    [
        ("approved", "Your refund request has been approved."),
        ("partial_refund", "A partial refund has been processed successfully."),
        ("denied", "Your refund request has been denied."),
        ("need_info", "We need more information before we can complete the refund review."),
        ("manual_review", "Your refund request has been sent for human review."),
        ("refund_failed", "We could not complete your refund and will follow up."),
    ],
)
def test_every_outcome_has_a_trusted_anchor_and_prompt_requirement(outcome, anchor):
    payload = build_response_payload(_outcome_state(outcome))

    assert payload["final_outcome"] == outcome
    assert payload["outcome_anchor"] == anchor
    prompt = _build_prompt(payload)
    assert "Include this trusted outcome sentence verbatim" in prompt
    assert anchor in prompt


@pytest.mark.parametrize(
    "outcome",
    ["approved", "partial_refund", "denied", "need_info", "manual_review", "refund_failed"],
)
def test_anchor_alone_is_semantically_valid_for_each_outcome(monkeypatch, outcome):
    payload = build_response_payload(_outcome_state(outcome))
    anchor = payload["outcome_anchor"]
    _install(monkeypatch, [FakeResponse([MessageItem(anchor)], usage=(10, 8))])

    out = response_node(_outcome_state(outcome))
    checks = out["response_result"]["content_checks"]

    assert out["final_outcome"] == outcome
    assert out["response_result"]["response"]["body"] == anchor
    assert checks["outcome_anchor_inserted"] is False
    assert checks["outcome_anchor_reflected"] is True
    assert checks["decision_reflected"] is True
    assert checks["semantic_errors"] == []


def test_missing_anchor_is_inserted_immediately_after_greeting(monkeypatch):
    draft = "Hello,\n\nThanks for reaching out. Your request is pending human review."
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(10, 8))])

    out = response_node(_outcome_state("manual_review"))
    body = out["response_result"]["response"]["body"]
    checks = out["response_result"]["content_checks"]

    assert body.startswith(
        "Hello,\n\nYour refund request has been sent for human review.\n\nThanks"
    )
    assert checks["outcome_anchor_inserted"] is True
    assert checks["outcome_anchor_reflected"] is True
    assert checks["decision_reflected"] is True


def test_inserted_anchor_does_not_hide_contradictory_azure_prose(monkeypatch):
    draft = (
        "Hello,\n\nGood news: your refund has been approved and will be issued. "
        "Customer Support Team"
    )
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(10, 8))])

    out = response_node(_outcome_state("manual_review"))
    body = out["response_result"]["response"]["body"]
    checks = out["response_result"]["content_checks"]

    assert "Your refund request has been sent for human review." in body
    assert "your refund has been approved" in body
    assert checks["outcome_anchor_reflected"] is True
    assert checks["decision_reflected"] is False
    assert checks["semantic_errors"]


def test_inserted_anchor_does_not_bypass_safe_summary_or_required_info(monkeypatch):
    deny_state = _deny_state()
    _install(
        monkeypatch,
        [FakeResponse([MessageItem("Hello,\n\nWe cannot process your refund.")], usage=(10, 8))],
    )
    denied = response_node(deny_state)["response_result"]["content_checks"]
    assert denied["outcome_anchor_reflected"] is True
    assert denied["safe_summary_reflected"] is False

    info_state = {
        "policy_decision": {
            "decision": "request_info",
            "missing_info_to_request": ["photo of the damaged item"],
        }
    }
    _install(
        monkeypatch,
        [FakeResponse([MessageItem("Hello,\n\nPlease provide more details.")], usage=(10, 8))],
    )
    need_info = response_node(info_state)["response_result"]["content_checks"]
    assert need_info["outcome_anchor_reflected"] is True
    assert need_info["missing_info_requested"] is False


@pytest.mark.parametrize(
    ("body", "payload"),
    [
        (
            "Your refund has been processed. Customer Support Team",
            {"final_outcome": "approved"},
        ),
        (
            "We are unable to approve the refund. Customer Support Team",
            {"final_outcome": "denied"},
        ),
        (
            "Could you please share your order ID? Customer Support Team",
            {"final_outcome": "need_info", "required_information": ["order ID"]},
        ),
        (
            "Your request is pending human review. Customer Support Team",
            {"final_outcome": "manual_review"},
        ),
    ],
)
def test_semantic_validation_accepts_each_supported_outcome(body, payload):
    checks = validate_response_semantics(body, payload)
    assert checks["decision_reflected"] is True
    assert checks["missing_info_requested"] is True
    assert checks["safe_summary_reflected"] is True
    assert checks["semantic_errors"] == []


def test_semantic_validation_requires_customer_safe_summary():
    checks = validate_response_semantics(
        "Unfortunately, we cannot process your refund. Customer Support Team",
        {
            "final_outcome": "denied",
            "required_safe_summary": "We cannot process this refund.",
        },
    )

    assert checks["decision_reflected"] is True
    assert checks["safe_summary_reflected"] is False
    assert "customer-safe summary" in checks["semantic_errors"][-1]


def test_demo07_live_manual_review_negated_approval_is_not_positive():
    body = """Hello,

Thanks for reaching out. I understand you’re looking for an update on your refund, and I’m sorry for any inconvenience.

Your request has been sent for human review. Your request needs manual review because the order already received a partial refund and the remaining refund eligibility must be reviewed against the unpaid order value.

At this stage, no additional refund has been approved yet. A specialist will review the remaining eligibility and follow up once the assessment is complete. We appreciate your patience while this is being checked.

If you have any additional details you’d like us to consider, please reply to this email.

Best regards,
Customer Support Team"""

    checks = validate_response_semantics(body, {"final_outcome": "manual_review"})

    assert checks["decision_reflected"] is True
    assert checks["semantic_errors"] == []


def test_demo20_live_review_completion_is_not_refund_completion():
    body = """Hello,

Thanks for reaching out about your refund. I understand it’s frustrating to wait for an update.

Your request has been sent for human review. Your request needs manual review because the order is marked as returned and you are asking about refund status. A reviewer can confirm the refund state for this returned order.

At this time, we’re waiting for that review to be completed before we can confirm the refund status. We appreciate your patience, and we’ll make sure the case is checked as soon as possible.

If you have any other questions in the meantime, feel free to reply to this email.

Best regards,""" + "  \nCustomer Support Team"

    checks = validate_response_semantics(body, {"final_outcome": "manual_review"})

    assert checks["decision_reflected"] is True
    assert checks["semantic_errors"] == []


def test_demo18_live_active_refund_negations_are_not_approvals():
    body = """Hello,

Thank you for reaching out. We understand this situation may be frustrating, and we appreciate your patience.

Your request has been sent for human review. We need a manual review because your message indicates the order number may be incorrect and mentions a different order ID than the linked order record. We have not issued a refund based on this review.

At this time, no refund has been processed. A member of our team will review the details and follow up if any additional information is needed or once the review is complete.

Thank you for your understanding.

Customer Support Team"""

    checks = validate_response_semantics(body, {"final_outcome": "manual_review"})

    assert checks["decision_reflected"] is True
    assert checks["semantic_errors"] == []


def test_demo19_live_active_refund_negations_are_not_approvals():
    body = """Hello,

Thanks for contacting us. We understand this situation is frustrating, and we appreciate your patience while we review it.

Your request has been sent for human review. Your request needs manual review because the delivery claim does not match the current order record. We have not issued a refund based on this policy check.

At this time, no refund has been issued. A specialist will review the details and follow up once the review is complete. If any additional information is needed, we’ll reach out to you directly.

Thank you for your understanding.

Best regards,
Customer Support Team"""

    checks = validate_response_semantics(body, {"final_outcome": "manual_review"})

    assert checks["decision_reflected"] is True
    assert checks["semantic_errors"] == []


def test_demo13_live_deferred_decision_is_not_a_denial():
    body = """Hello,

Thanks for reaching out. We understand waiting on a refund update can be frustrating.

At this time, your request has been sent for human review. This means a specialist will take a closer look at the details of your case before the next step is taken. While we’re not able to provide a final refund decision just yet, please know that your request is actively being reviewed.

We appreciate your patience and understanding while this process is completed. If any additional information is needed, you’ll be contacted directly.

Best regards,""" + "  \nCustomer Support Team"

    checks = validate_response_semantics(body, {"final_outcome": "manual_review"})

    assert checks["decision_reflected"] is True
    assert checks["semantic_errors"] == []


def test_demo14_live_approve_or_deny_deferral_is_not_a_denial():
    body = """Hello,

Thank you for reaching out about your refund request. We need a bit more information before we can evaluate the refund request.

At the moment, we’re unable to approve or deny the case because some essential details are missing. To help us continue, please reply with the following:

- The reason for the refund
- The amount you are requesting for the refund

Once we receive this information, we’ll review your request as quickly as possible and update you on the outcome.

We appreciate your patience and look forward to helping you resolve this.

Best regards,
Customer Support Team"""

    checks = validate_response_semantics(body, {"final_outcome": "need_info"})

    assert checks["decision_reflected"] is True
    assert checks["missing_info_requested"] is True
    assert checks["semantic_errors"] == []


@pytest.mark.parametrize(
    "body",
    [
        "Your refund request has been denied. Customer Support Team",
        "This request is declined. Customer Support Team",
        "This purchase is not eligible for a refund. Customer Support Team",
        "We are unable to approve an additional refund. Customer Support Team",
        "The refund will not be issued. Customer Support Team",
        "The policy outcome is denial. Customer Support Team",
    ],
)
def test_structural_denial_matcher_keeps_final_negative_claims(body):
    checks = validate_response_semantics(body, {"final_outcome": "denied"})

    assert checks["decision_reflected"] is True
    assert checks["semantic_errors"] == []


@pytest.mark.parametrize(
    "body",
    [
        "We have approved your refund. Customer Support Team",
        "Your refund will be issued shortly. Customer Support Team",
        "We approved a partial refund. Customer Support Team",
        "The refund has now been processed successfully. Customer Support Team",
    ],
)
def test_structural_approval_matcher_keeps_affirmative_refund_claims(body):
    checks = validate_response_semantics(body, {"final_outcome": "approved"})

    assert checks["decision_reflected"] is True
    assert checks["semantic_errors"] == []


def test_manual_review_still_blocks_a_separate_positive_approval_claim():
    body = (
        "Your request is under manual review. We have not issued a refund based on the preliminary review. "
        "However, your refund has now been approved and will be issued. Customer Support Team"
    )

    checks = validate_response_semantics(body, {"final_outcome": "manual_review"})

    assert checks["decision_reflected"] is False
    assert "manual_review" in checks["semantic_errors"][0]


# ── ResponseGovernanceNode ────────────────────────────────────────────────────

def test_is_base_governance_node():
    assert isinstance(ResponseGovernanceNode(), BaseGovernanceNode)


def test_governance_node_allow_on_clean_draft(monkeypatch):
    draft = "We have approved your refund. Your refund has been processed. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(20, 15))])

    state = response_node(_approve_state())
    state.update({"trace_id": "TRACE-1", "ticket_id": "TK-001", "user_id": "CUST-001"})

    result = ResponseGovernanceNode()(state)

    assert result["response_governance_result"]["status"] == "allow"
    assert result["current_stage"] == "response_governance"
    assert len(result["audit_trail"]) == 1


def test_governance_quarantines_approval_sounding_denial(monkeypatch):
    draft = "Good news — your refund has been approved and will be issued. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(20, 15))])

    state = response_node(_deny_state())
    state.update({"trace_id": "TRACE-2", "ticket_id": "TK-002", "user_id": "CUST-002"})
    result = ResponseGovernanceNode()(state)

    governance = result["response_governance_result"]
    assert governance["status"] == "block"
    assert any(finding["name"] == "semantic_drift" for finding in governance["findings"])
    assert result["human_review_required"] is True
    assert result["workflow_status"] == "waiting_human"
    assert result["response_result"]["workflow_status"] == "waiting_human"
    assert result["response_result"]["delivery_status"] == "quarantined"


def test_governance_allow_preserves_existing_pending_human_review(monkeypatch):
    draft = "Your request is awaiting human review. Customer Support Team"
    _install(monkeypatch, [FakeResponse([MessageItem(draft)], usage=(20, 15))])

    state = response_node({
        "trace_id": "TRACE-1",
        "ticket_id": "TK-001",
        "user_id": "CUST-001",
        "policy_decision": {"decision": "manual_review"},
        "human_review_required": True,
        "human_review": {"status": "pending", "approval_id": "APP-1"},
        "workflow_status": "waiting_human",
    })
    state.update({
        "trace_id": "TRACE-1",
        "ticket_id": "TK-001",
        "user_id": "CUST-001",
        "human_review_required": True,
        "human_review": {"status": "pending", "approval_id": "APP-1"},
        "workflow_status": "waiting_human",
        "review_trigger_stage": "policy",
        "review_trigger_reason": "manual_review",
    })

    result = ResponseGovernanceNode()(state)

    assert result["response_governance_result"]["status"] == "allow"
    assert result["human_review_required"] is True
    assert result["workflow_status"] == "waiting_human"
    assert result["review_trigger_stage"] == "policy"
    assert result["review_trigger_reason"] == "manual_review"


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
