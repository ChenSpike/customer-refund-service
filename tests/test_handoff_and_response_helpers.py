import pytest

from agents.triage.routing import route_triage
from agents.response.node import build_response_payload


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "governance_status": "block",
                "user_action_required": False,
                "has_triage_output": True,
            },
            "human_review",
        ),
        (
            {
                "governance_status": "allow",
                "user_action_required": True,
                "has_triage_output": True,
            },
            "response",
        ),
        (
            {
                "governance_status": "allow",
                "user_action_required": False,
                "has_triage_output": True,
            },
            "policy",
        ),
        (
            {
                "governance_status": "allow",
                "user_action_required": False,
                "has_triage_output": False,
            },
            "response",
        ),
    ],
)
def test_route_triage_matrix(kwargs, expected):
    assert route_triage(**kwargs) == expected


def test_build_response_payload_for_need_info():
    payload = build_response_payload(
        {
            "user_action_required": True,
            "clarification_question": "Please share your order ID.",
        }
    )

    assert payload == {
        "message": "Please share your order ID.",
        "final_outcome": "need_info",
        "workflow_status": "waiting_user",
        "outcome_anchor": "We need more information before we can complete the refund review.",
        "required_information": ["Please share your order ID."],
    }


def test_build_response_payload_for_refund_success():
    payload = build_response_payload(
        {
            "refund_result": {
                "status": "success",
                "message": "Refund completed.",
            },
            "policy_decision": {"decision": "approve"},
        }
    )

    assert payload == {
        "message": "Refund completed.",
        "final_outcome": "approved",
        "workflow_status": "completed",
        "outcome_anchor": "Your refund request has been approved.",
    }


def test_build_response_payload_carries_policy_customer_safe_summary():
    payload = build_response_payload(
        {
            "refund_result": {"status": "success", "message": "Refund completed."},
            "policy_decision": {"decision": "approve"},
            "policy_context": {
                "response_guidance": {
                    "customer_safe_summary": "Your eligible refund was approved.",
                    "missing_info_to_request": [],
                }
            },
        }
    )

    assert payload["required_safe_summary"] == "Your eligible refund was approved."


def test_build_response_payload_for_manual_review():
    payload = build_response_payload(
        {
            "policy_decision": {"decision": "manual_review"},
        }
    )

    assert payload == {
        "message": "A specialist needs to review the details before we can continue.",
        "final_outcome": "manual_review",
        "workflow_status": "waiting_human",
        "outcome_anchor": "Your case has been sent for human review.",
    }
