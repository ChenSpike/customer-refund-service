from __future__ import annotations

import hashlib
import json
import os
from collections import Counter

import pytest

from agents.policy.cloud_db import GCPRepository
from agents.policy.models import PolicyAgentInput, PolicyAgentOutput, PolicyReasoningResult
from agents.policy.policy_node import (
    load_policy_context,
    load_precedent_context,
    validate_policy_result,
)
from agents.policy.service import PolicyAgentService


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
    "TRACE-POL-012": "approve",
    "TRACE-POL-013": "deny",
    "TRACE-POL-014": "request_info",
    "TRACE-POL-015": "manual_review",
    "TRACE-POL-016": "approve",
    "TRACE-POL-017": "deny",
    "TRACE-POL-018": "manual_review",
    "TRACE-POL-019": "manual_review",
    "TRACE-POL-020": "manual_review",
}
EXPECTED_ROUTES = {
    "TRACE-POL-001": "refund_agent",
    "TRACE-POL-002": "refund_agent",
    "TRACE-POL-003": "refund_agent",
    "TRACE-POL-004": "response_agent",
    "TRACE-POL-005": "response_agent",
    "TRACE-POL-006": "response_agent",
    "TRACE-POL-007": "human_approval",
    "TRACE-POL-008": "human_approval",
    "TRACE-POL-009": "human_approval",
    "TRACE-POL-010": "response_agent",
    "TRACE-POL-011": "human_approval",
    "TRACE-POL-012": "human_approval",
    "TRACE-POL-013": "human_approval",
    "TRACE-POL-014": "response_agent",
    "TRACE-POL-015": "human_approval",
    "TRACE-POL-016": "refund_agent",
    "TRACE-POL-017": "response_agent",
    "TRACE-POL-018": "human_approval",
    "TRACE-POL-019": "human_approval",
    "TRACE-POL-020": "human_approval",
}
EXPECTED_CONFIDENCE = {
    "TRACE-POL-001": 3,
    "TRACE-POL-002": 3,
    "TRACE-POL-003": 3,
    "TRACE-POL-004": 3,
    "TRACE-POL-005": 3,
    "TRACE-POL-006": 3,
    "TRACE-POL-007": 3,
    "TRACE-POL-008": 3,
    "TRACE-POL-009": 3,
    "TRACE-POL-010": 0,
    "TRACE-POL-011": 3,
    "TRACE-POL-012": 3,
    "TRACE-POL-013": 3,
    "TRACE-POL-014": 0,
    "TRACE-POL-015": 3,
    "TRACE-POL-016": 3,
    "TRACE-POL-017": 2,
    "TRACE-POL-018": 1,
    "TRACE-POL-019": 1,
    "TRACE-POL-020": 3,
}
POLICY_REVIEW_TRACES = [
    "TRACE-POL-007",
    "TRACE-POL-008",
    "TRACE-POL-009",
    "TRACE-POL-011",
    "TRACE-POL-015",
    "TRACE-POL-018",
    "TRACE-POL-019",
    "TRACE-POL-020",
]
GOVERNANCE_TRACES = ["TRACE-POL-012", "TRACE-POL-013"]

INPUT_KEYS = ["case", "customer_request", "order_facts"]
OUTPUT_KEYS = [
    "case",
    "customer_request",
    "policy_evaluation",
    "decision",
    "response_guidance",
    "handoff",
    "governance",
]
DECISION_KEYS = [
    "type",
    "refund_amount",
    "confidence",
    "confidence_level",
    "confidence_evidence",
    "precedent_evidence",
    "reason",
]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_POLICY_AGENT_LIVE_TESTS") != "1",
        reason="Set RUN_POLICY_AGENT_LIVE_TESTS=1 to authorize destructive Azure/GCP testing.",
    ),
]


def test_live_langgraph_policy_agent_processes_20_gcp_cases() -> None:
    repository = GCPRepository.from_env()
    assert repository.check_schema()["source_handoffs"] == 20
    unrelated_before = _unrelated_hashes(repository)
    downstream_before = _benchmark_downstream_hashes(repository)
    retained_before = repository.policy_artifact_ids()

    reset_counts = repository.reset_policy_agent_data()
    assert reset_counts["source_handoffs"] == 20
    reset_ids = repository.policy_artifact_ids()
    assert reset_ids["source_handoffs"] == [str(value) for value in range(1, 21)]
    assert reset_ids["policy_handoffs"] == []
    assert reset_ids["audit_log"] == []
    for name in ("policy_review_events", "governance_events", "human_approvals"):
        assert reset_ids[name] == retained_before[name]
    assert _benchmark_downstream_hashes(repository) == downstream_before

    service = PolicyAgentService.from_env()
    graph_nodes = set(service.graph.get_graph().nodes) - {"__start__", "__end__"}
    assert graph_nodes == {"policy", "policy_governance", "policy_handoff"}
    processed = service.run("benchmark")
    assert len(processed) == 20
    assert all(item.policy_usage.input_tokens > 0 and item.policy_usage.output_tokens > 0 for item in processed)
    assert all(item.governance_usage.input_tokens > 0 and item.governance_usage.output_tokens > 0 for item in processed)

    decisions = {item.output.case.trace_id: item.output.decision.type for item in processed}
    routes = {item.output.case.trace_id: item.output.handoff.next_agent for item in processed}
    confidences = {item.output.case.trace_id: item.output.decision.confidence for item in processed}
    assert decisions == EXPECTED_DECISIONS
    assert routes == EXPECTED_ROUTES
    assert confidences == EXPECTED_CONFIDENCE
    assert Counter(decisions.values()) == {"approve": 5, "deny": 5, "manual_review": 8, "request_info": 2}
    assert Counter(confidences.values()) == {3: 15, 2: 1, 1: 2, 0: 2}
    assert Counter(routes.values()) == {
        "refund_agent": 4,
        "response_agent": 6,
        "human_approval": 10,
    }

    records = repository.fetch_case_records()
    audit_payloads = _successful_audit_payloads(repository)
    assert set(audit_payloads) == set(EXPECTED_DECISIONS)
    policy_context = load_policy_context("v1.0")
    precedent_context = load_precedent_context("v1.0", policy_context=policy_context)
    assert (precedent_context.available, precedent_context.status) == (False, "empty")
    assert len(records) == 20
    assert [record.handoff_id for record in records] == [str(value) for value in range(21, 41)]
    for record in records:
        assert list(record.input_json) == INPUT_KEYS
        assert list(record.output_json) == OUTPUT_KEYS
        assert list(record.output_json["decision"]) == DECISION_KEYS
        policy_input = PolicyAgentInput.model_validate(record.input_json)
        output = PolicyAgentOutput.model_validate(record.output_json)
        assert output.case.trace_id == policy_input.case.trace_id == record.trace_id
        assert output.case.ticket_id == policy_input.case.ticket_id == record.ticket_id
        assert output.case.policy_version_used == policy_input.case.policy_version
        assert output.customer_request == policy_input.customer_request
        assert output.decision.type == EXPECTED_DECISIONS[record.trace_id]
        assert output.decision.confidence == EXPECTED_CONFIDENCE[record.trace_id]
        assert output.decision.confidence_level == {
            3: "high",
            2: "moderate",
            1: "low",
            0: "insufficient",
        }[EXPECTED_CONFIDENCE[record.trace_id]]
        assert record.to_agent == output.handoff.next_agent == EXPECTED_ROUTES[record.trace_id]
        assert "policy_conflict" not in output.governance.flags
        assert record.input_tokens > 0 and record.output_tokens > 0
        assert record.workflow_input_tokens >= record.input_tokens
        assert record.workflow_output_tokens >= record.output_tokens

        audit_payload = audit_payloads[record.trace_id]
        assert audit_payload["handoff_id"] == record.handoff_id
        assert audit_payload["input_tokens"] == record.input_tokens
        assert audit_payload["output_tokens"] == record.output_tokens
        assert audit_payload["precedent_memory"] == {
            "available": False,
            "status": "empty",
            "reason": precedent_context.reason,
            "record_count": 0,
        }
        policy_result = PolicyReasoningResult(
            case=output.case,
            customer_request=output.customer_request,
            policy_evaluation=output.policy_evaluation,
            decision=output.decision,
            response_guidance=output.response_guidance,
            evidence_manifest=audit_payload["policy_evidence_manifest"],
        )
        validate_policy_result(policy_result, policy_input, policy_context, precedent_context)
        assert output.decision.precedent_evidence.status == "unavailable"
        assert output.decision.precedent_evidence.memory_status == "empty"
        assert output.decision.precedent_evidence.assessment == "unavailable"
        assert output.decision.precedent_evidence.referenced_precedent_ids == []

        assert output.governance.interceptor_action in {"allow", "quarantine"}
        if record.trace_id in GOVERNANCE_TRACES or record.trace_id in POLICY_REVIEW_TRACES:
            assert (record.workflow_status, record.current_agent) == ("pending_human", "human_approval")
        else:
            assert (record.workflow_status, record.current_agent) == ("running", record.to_agent)

    review_records = repository.fetch_review_records()
    policy_rows = review_records["policy"]
    governance_rows = review_records["governance"]
    approvals = review_records["approvals"]
    assert [row["trace_id"] for row in policy_rows] == POLICY_REVIEW_TRACES
    assert [row["policy_review_event_id"] for row in policy_rows] == [
        f"POL-REV-{value:03d}" for value in range(1, 9)
    ]
    assert [row["trace_id"] for row in governance_rows] == GOVERNANCE_TRACES
    assert [row["event_id"] for row in governance_rows] == ["POL-GOV-001", "POL-GOV-002"]
    assert [row["owasp_category"] for row in governance_rows] == ["ASI01", "ASI07"]
    assert all(row["interceptor_action"] == "quarantine" for row in governance_rows)
    assert all(row["owasp_category"] != "ASI08" for row in governance_rows)
    assert [row["flags_json"]["finding"]["flag"] for row in governance_rows] == [
        "semantic_drift",
        "pii_risk",
    ]

    assert [row["approval_id"] for row in approvals] == [f"POL-APP-{value:03d}" for value in range(1, 11)]
    assert all("policy_review_event_id" not in row for row in approvals)
    assert Counter(row["triggering_event_type"] for row in approvals) == {
        "policy_review": 8,
        "governance": 2,
    }
    assert {
        row["triggering_event_id"]
        for row in approvals
        if row["triggering_event_type"] == "policy_review"
    } == {row["policy_review_event_id"] for row in policy_rows}
    assert {
        row["triggering_event_id"]
        for row in approvals
        if row["triggering_event_type"] == "governance"
    } == {row["event_id"] for row in governance_rows}
    for row in approvals:
        assert row["notes"]["review_source"] == row["triggering_event_type"]
        assert row["rejected_next_agent"] == "response_agent"
        if row["trace_id"] == "TRACE-POL-013":
            assert row["approved_next_agent"] == "response_agent"
        else:
            assert row["approved_next_agent"] == "refund_agent"

    integrity = repository.integrity_counts()
    assert all(value == 0 for name, value in integrity.items() if name.startswith("orphan_"))
    assert integrity["policy_agent_human_approvals"] == 10
    artifact_ids = repository.policy_artifact_ids()
    assert artifact_ids["source_handoffs"] == [str(value) for value in range(1, 21)]
    assert artifact_ids["policy_handoffs"] == [str(value) for value in range(21, 41)]
    assert artifact_ids["policy_review_events"] == [f"POL-REV-{value:03d}" for value in range(1, 9)]
    assert artifact_ids["governance_events"] == ["POL-GOV-001", "POL-GOV-002"]
    assert artifact_ids["human_approvals"] == [f"POL-APP-{value:03d}" for value in range(1, 11)]
    assert len(artifact_ids["audit_log"]) == 20
    assert all(event_type == "policy_agent_evaluated" for _, event_type in artifact_ids["audit_log"])
    assert _unrelated_hashes(repository) == unrelated_before
    assert _benchmark_downstream_hashes(repository) == downstream_before


def _successful_audit_payloads(repository: GCPRepository) -> dict[str, dict]:
    connection = repository._connect()
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT trace_id, payload_json
            FROM audit_log
            WHERE agent = 'policy_agent'
              AND event_type = 'policy_agent_evaluated'
              AND trace_id REGEXP '^TRACE-POL-(00[1-9]|01[0-9]|020)$'
            ORDER BY trace_id
            """
        )
        rows = cursor.fetchall()
        return {row["trace_id"]: json.loads(row["payload_json"]) for row in rows}
    finally:
        connection.close()


def _unrelated_hashes(repository: GCPRepository) -> dict[str, str]:
    connection = repository._connect()
    try:
        cursor = connection.cursor(dictionary=True)
        hashes = {}
        for table in (
            "agent_handoffs",
            "audit_log",
            "governance_events",
            "policy_review_events",
            "human_approvals",
            "workflow_runs",
        ):
            cursor.execute(
                f"""
                SELECT * FROM {table}
                WHERE trace_id NOT REGEXP '^TRACE-POL-(00[1-9]|01[0-9]|020)$'
                ORDER BY 1
                """
            )
            payload = json.dumps(cursor.fetchall(), sort_keys=True, default=str, ensure_ascii=False).encode()
            hashes[table] = hashlib.sha256(payload).hexdigest()
        return hashes
    finally:
        connection.close()


def _benchmark_downstream_hashes(repository: GCPRepository) -> dict[str, str]:
    connection = repository._connect()
    try:
        cursor = connection.cursor(dictionary=True)
        hashes = {}
        for table in ("refund_transactions",):
            cursor.execute(
                f"""
                SELECT * FROM {table}
                WHERE trace_id REGEXP '^TRACE-POL-(00[1-9]|01[0-9]|020)$'
                ORDER BY 1
                """
            )
            payload = json.dumps(
                cursor.fetchall(), sort_keys=True, default=str, ensure_ascii=False
            ).encode()
            hashes[table] = hashlib.sha256(payload).hexdigest()
        return hashes
    finally:
        connection.close()
