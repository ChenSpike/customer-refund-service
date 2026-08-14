import copy
import json
from datetime import datetime, timezone

import pytest

from dashboard_app.repository import DashboardRepositoryError
from dashboard_app.service import DashboardDataError, DashboardService, build_case_detail


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _bundle(trace_id: str = "demo01") -> dict:
    triage_output = {
        "triage_output": {
            "customer_request": {
                "sanitized_text": "My demo item arrived damaged.",
                "refund_reason": "damaged",
                "requested_amount": 80.0,
                "currency": "USD",
            },
            "order_facts": {
                "order_id": "ORD-DEMO-01",
                "product_type": "Electronics",
                "purchase_date": "2026-08-01",
                "item_status": "damaged",
                "amount_paid": 80.0,
                "prior_refund_total": 0.0,
            },
        },
        "triage_governance_result": {"status": "allow"},
        "triage_handoff": "policy",
    }
    policy_output = {
        "case": {"trace_id": trace_id, "ticket_id": "TICKET-DEMO-01", "policy_version_used": "v1.0"},
        "customer_request": triage_output["triage_output"]["customer_request"],
        "order_facts": triage_output["triage_output"]["order_facts"],
        "policy_evaluation": {
            "matched_policies": [
                {"policy_id": "POL-RET-01", "rule_summary": "Damaged item is refundable.", "effect": "approve"}
            ],
            "gaps_or_conflicts": [],
        },
        "decision": {
            "type": "approve",
            "refund_amount": 80.0,
            "confidence_level": "high",
            "reason": "Order facts meet the damaged-item rule.",
        },
        "governance": {"semantic_drift_score": 0.0, "interceptor_action": "allow", "flags": []},
        "handoff": {"next_agent": "refund_agent", "reason": "Approved"},
    }
    response_output = {
        "response_result": {
            "final_outcome": "approved",
            "workflow_status": "completed",
            "response": {"body": "Your refund was processed."},
        },
        "response_handoff": "end",
    }
    return {
        "workflow": {
            "trace_id": trace_id,
            "ticket_id": "TICKET-DEMO-01",
            "status": "completed",
            "current_agent": "completed",
            "policy_version": "v1.0",
            "started_at": NOW,
            "updated_at": NOW,
        },
        "ticket": {
            "ticket_id": "TICKET-DEMO-01",
            "customer_id": "CUST-DEMO-01",
            "raw_text": "My demo item arrived damaged.",
            "sanitized_text": None,
            "refund_reason": None,
            "requested_amount": None,
            "currency": "USD",
            "updated_at": NOW,
        },
        "customer": {"customer_id": "CUST-DEMO-01", "full_name": "Demo 01", "email": "demo01@example.test"},
        "orders": [
            {
                "order_id": "ORD-DEMO-01",
                "customer_id": "CUST-DEMO-01",
                "product_type": "Electronics",
                "purchase_date": NOW,
                "item_status": "damaged",
                "amount_paid": 80.0,
                "prior_refund_total": 0.0,
            }
        ],
        "handoffs": [
            {"handoff_id": "1", "from_agent": "triage_agent", "output_json": json.dumps(triage_output), "created_at": NOW},
            {"handoff_id": "2", "from_agent": "policy_agent", "output_json": json.dumps(policy_output), "created_at": NOW},
            {"handoff_id": "3", "from_agent": "response_agent", "output_json": json.dumps(response_output), "created_at": NOW},
        ],
        "governance_events": [
            {
                "event_id": "GOV-1",
                "trace_id": trace_id,
                "agent": "triage_agent",
                "owasp_category": "ASI00",
                "trigger_score": None,
                "interceptor_action": "allow",
                "flags_json": json.dumps({"summary": "Passed"}),
                "created_at": NOW,
            }
        ],
        "approvals": [],
        "refunds": [
            {
                "transaction_id": "RF-DEMO-01",
                "trace_id": trace_id,
                "status": "issued",
                "amount": 80.0,
                "currency": "USD",
                "external_ref": "DEMO",
                "created_at": NOW,
            }
        ],
        "audit_log": [
            {
                "log_id": 1,
                "trace_id": trace_id,
                "event_type": "policy_agent_evaluated",
                "agent": "policy_agent",
                "payload_json": json.dumps({"output": policy_output}),
                "created_at": NOW,
            }
        ],
        "policy_reviews": [],
    }


class FakeRepository:
    def __init__(self, bundles: list[dict]) -> None:
        self.database_name = "final"
        self.bundles = bundles

    def list_case_bundles(self, limit: int = 200) -> list[dict]:
        return self.bundles[:limit]

    def get_case_bundle(self, trace_id: str):
        return next((bundle for bundle in self.bundles if bundle["workflow"]["trace_id"] == trace_id), None)

    def query_audit(self, **_filters):
        return [row for bundle in self.bundles for row in bundle["audit_log"]]

    def query_governance(self, **_filters):
        return [row for bundle in self.bundles for row in bundle["governance_events"]]

    def pending_approvals(self, limit: int = 100):
        return [row for bundle in self.bundles for row in bundle["approvals"] if row["status"] == "pending"][:limit]


def test_case_detail_reads_nested_triage_and_canonical_policy_output():
    detail = build_case_detail(_bundle())

    assert detail["traceId"] == "demo01"
    assert detail["status"] == "auto_approved"
    assert detail["request"]["requestedAmount"] == 80.0
    assert detail["reason"] == "damaged"
    assert detail["order"]["orderId"] == "ORD-DEMO-01"
    assert detail["policy"]["decision"]["type"] == "Approve"
    assert detail["refund"]["transactionId"] == "RF-DEMO-01"
    assert detail["readOnly"] is True


def test_governance_block_with_typed_pending_approval_is_quarantined():
    bundle = copy.deepcopy(_bundle("demo07"))
    bundle["refunds"] = []
    bundle["handoffs"] = bundle["handoffs"][:2]
    policy = json.loads(bundle["handoffs"][1]["output_json"])
    policy["decision"]["type"] = "manual_review"
    policy["governance"] = {"semantic_drift_score": 0.91, "interceptor_action": "quarantine", "flags": ["pii_risk"]}
    policy["handoff"] = {"next_agent": "human_approval", "reason": "ASI07 review"}
    bundle["handoffs"][1]["output_json"] = json.dumps(policy)
    bundle["workflow"].update({"status": "pending_human", "current_agent": "human_approval"})
    bundle["governance_events"] = [
        {
            "event_id": "POL-GOV-007",
            "trace_id": "demo07",
            "agent": "policy_agent",
            "owasp_category": "ASI07",
            "trigger_score": 0.91,
            "interceptor_action": "quarantine",
            "flags_json": json.dumps({"finding": {"flag": "pii_risk", "detail": "Foreign customer data"}}),
            "offending_content": "redacted@example.test",
            "created_at": NOW,
        }
    ]
    bundle["approvals"] = [
        {
            "approval_id": "POL-APP-007",
            "trace_id": "demo07",
            "triggering_event_id": "POL-GOV-007",
            "triggering_event_type": "governance",
            "reason": "ASI07 review",
            "status": "pending",
            "approved_next_agent": "refund_agent",
            "rejected_next_agent": "response_agent",
            "notes": json.dumps({"review_source": "governance"}),
            "created_at": NOW,
        }
    ]

    detail = build_case_detail(bundle)

    assert detail["status"] == "quarantined"
    assert detail["riskTag"] == {"code": "ASI07", "label": "Data Leakage"}
    assert detail["pendingApprovalId"] == "POL-APP-007"
    assert detail["governance"]["triggerScore"] == 0.91
    assert detail["pipeline"][4]["state"] == "blocked"
    assert detail["pipeline"][5]["state"] == "current"


def test_service_metrics_and_read_endpoints_use_only_fake_repository():
    service = DashboardService(FakeRepository([_bundle("demo01"), _bundle("demo02")]))

    assert [case["traceId"] for case in service.list_cases()] == ["demo01", "demo02"]
    assert service.get_case("demo02")["customer"] == "Demo 01"
    metrics = service.metrics()
    assert metrics["statusBreakdown"] == [{"status": "auto_approved", "count": 2}]
    assert metrics["secondaryStats"][0] == {"label": "Total Cases", "value": "2"}
    assert service.audit()[0]["category"] == "Policy"
    assert service.governance()[0]["riskLabel"] == "No finding"


def test_resolved_manual_review_uses_persisted_final_outcome():
    bundle = _bundle("demo09")
    policy = json.loads(bundle["handoffs"][1]["output_json"])
    policy["decision"]["type"] = "manual_review"
    bundle["handoffs"][1]["output_json"] = json.dumps(policy)
    bundle["approvals"] = [
        {
            "approval_id": "POL-APP-009",
            "trace_id": "demo09",
            "triggering_event_type": "policy_review",
            "status": "approved",
            "notes": "Approved after checking the receipt.",
            "created_at": NOW,
            "resolved_at": NOW,
        }
    ]

    detail = build_case_detail(bundle)

    assert detail["status"] == "human_approved"
    assert detail["statusSource"] == "Human Approval"
    assert detail["approvals"][0]["notesPayload"] == {"text": "Approved after checking the receipt."}


def test_resolved_human_approval_is_excluded_from_automated_throughput():
    automated = _bundle("demo01")
    human = _bundle("demo09")
    human["approvals"] = [
        {
            "approval_id": "POL-APP-009",
            "trace_id": "demo09",
            "triggering_event_type": "policy_review",
            "status": "approved",
            "notes": "Approved by a reviewer.",
            "created_at": NOW,
            "resolved_at": NOW,
        }
    ]

    metrics = DashboardService(FakeRepository([automated, human])).metrics()

    assert metrics["primaryStats"][0] == {
        "label": "Automated Throughput",
        "value": "50%",
        "detail": "1 of 2 cases completed without human review",
    }
    assert metrics["statusBreakdown"] == [
        {"status": "human_approved", "count": 1},
        {"status": "auto_approved", "count": 1},
    ]


def test_dashboard_service_rejects_a_repository_without_the_final_database():
    repository = FakeRepository([_bundle()])
    repository.database_name = "main_db"

    with pytest.raises(DashboardRepositoryError, match="restricted to the final database"):
        DashboardService(repository)


def test_invalid_persisted_json_fails_loudly_instead_of_showing_empty_data():
    bundle = _bundle()
    bundle["handoffs"][1]["output_json"] = "not-json"

    with pytest.raises(DashboardDataError, match="invalid JSON"):
        build_case_detail(bundle)
