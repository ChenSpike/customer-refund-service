from __future__ import annotations

from collections import Counter

from policy_agent.cloud_db import GCPRepository
from policy_agent.models import PolicyAgentInput, PolicyAgentOutput
from policy_agent.service import PolicyAgentService


EXPECTED_DECISIONS = {
    "TRACE-POL-001": "approve",
    "TRACE-POL-002": "approve",
    "TRACE-POL-003": "approve",
    "TRACE-POL-004": "deny",
    "TRACE-POL-005": "deny",
    "TRACE-POL-006": "deny",
    "TRACE-POL-007": "manual_review",
    "TRACE-POL-008": "manual_review",
    "TRACE-POL-009": "manual_review",
    "TRACE-POL-010": "request_info",
    "TRACE-POL-011": "manual_review",
    "TRACE-POL-012": "manual_review",
    "TRACE-POL-013": "manual_review",
    "TRACE-POL-014": "request_info",
    "TRACE-POL-015": "manual_review",
    "TRACE-POL-016": "approve",
    "TRACE-POL-017": "deny",
    "TRACE-POL-018": "manual_review",
    "TRACE-POL-019": "manual_review",
    "TRACE-POL-020": "manual_review",
}

INPUT_KEYS = ["case", "customer_request", "order_facts"]
INPUT_CASE_KEYS = ["trace_id", "ticket_id", "policy_version"]
CUSTOMER_KEYS = ["sanitized_text", "refund_reason", "requested_amount", "currency"]
ORDER_KEYS = ["order_id", "product_type", "purchase_date", "item_status", "amount_paid", "prior_refund_total"]
OUTPUT_KEYS = [
    "case",
    "customer_request",
    "policy_evaluation",
    "decision",
    "response_guidance",
    "handoff",
    "governance",
]


def test_live_azure_policy_agent_processes_20_gcp_cases() -> None:
    repository = GCPRepository.from_env()
    reset_counts = repository.reset_policy_agent_data()
    assert reset_counts["source_handoffs"] == 20
    baseline_ids = repository.policy_artifact_ids()
    assert baseline_ids == {
        "source_handoffs": [str(value) for value in range(1, 21)],
        "policy_handoffs": [],
        "governance_events": [],
        "human_approvals": [],
        "audit_log": [],
    }

    service = PolicyAgentService.from_env()
    processed = service.run("all")
    assert len(processed) == 20

    decisions = {item.output.case.trace_id: item.output.decision.type for item in processed}
    assert decisions == EXPECTED_DECISIONS
    assert Counter(decisions.values()) == {
        "approve": 4,
        "deny": 4,
        "manual_review": 10,
        "request_info": 2,
    }

    records = repository.fetch_case_records()
    assert len(records) == 20
    assert len({record.trace_id for record in records}) == 20
    assert [record.handoff_id for record in records] == [str(value) for value in range(21, 41)]

    for record in records:
        assert list(record.input_json) == INPUT_KEYS
        assert list(record.input_json["case"]) == INPUT_CASE_KEYS
        assert list(record.input_json["customer_request"]) == CUSTOMER_KEYS
        assert list(record.input_json["order_facts"]) == ORDER_KEYS
        assert list(record.output_json) == OUTPUT_KEYS

        policy_input = PolicyAgentInput.model_validate(record.input_json)
        output = PolicyAgentOutput.model_validate(record.output_json)
        assert output.case.trace_id == policy_input.case.trace_id == record.trace_id
        assert output.case.ticket_id == policy_input.case.ticket_id == record.ticket_id
        assert output.case.policy_version_used == policy_input.case.policy_version
        assert output.customer_request == policy_input.customer_request
        assert output.decision.type == EXPECTED_DECISIONS[record.trace_id]
        assert record.to_agent == output.handoff.next_agent

        expected_route = {
            "approve": "response_agent",
            "deny": "response_agent",
            "partial_refund": "response_agent",
            "manual_review": "human_approval",
            "request_info": "triage_agent",
        }[output.decision.type]
        assert record.to_agent == expected_route
        assert record.input_tokens > 0
        assert record.output_tokens > 0
        assert record.workflow_input_tokens >= record.input_tokens
        assert record.workflow_output_tokens >= record.output_tokens

        if output.governance.interceptor_action == "block":
            assert (record.workflow_status, record.current_agent) == ("paused_governance", "human_approval")
        elif record.to_agent == "human_approval":
            assert (record.workflow_status, record.current_agent) == ("pending_human", "human_approval")
        else:
            assert (record.workflow_status, record.current_agent) == ("running", record.to_agent)

    integrity = repository.integrity_counts()
    assert integrity["orphan_audit_log"] == 0
    assert integrity["orphan_governance_events"] == 0
    assert integrity["orphan_human_approvals"] == 0
    assert integrity["human_approvals"] == 10

    artifact_ids = repository.policy_artifact_ids()
    assert artifact_ids["source_handoffs"] == [str(value) for value in range(1, 21)]
    assert artifact_ids["policy_handoffs"] == [str(value) for value in range(21, 41)]
    assert artifact_ids["human_approvals"] == [f"POL-APP-{value:03d}" for value in range(1, 11)]
    assert artifact_ids["governance_events"] == [
        f"POL-GOV-{value:03d}" for value in range(1, len(artifact_ids["governance_events"]) + 1)
    ]
    assert artifact_ids["audit_log"] == [
        (value, "policy_agent_evaluated") for value in range(1, 21)
    ]
