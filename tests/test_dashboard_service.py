import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from dashboard_app.repository import DashboardRepositoryError
from dashboard_app.service import (
    DashboardDataError,
    DashboardService,
    build_case_detail,
    normalize_approval_row,
    _iso,
    _latest_handoff,
    _relative_time,
)


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def test_latest_handoff_prefers_customer_continuation_on_timestamp_tie() -> None:
    timestamp = "2026-08-14T12:00:00"
    rows = [
        {
            "handoff_id": "HANDOFF-A",
            "from_agent": "triage_agent",
            "created_at": timestamp,
            "input_json": {
                "_continuation": {
                    "type": "customer_followup",
                    "claim_token": "token",
                }
            },
        },
        {
            "handoff_id": "HANDOFF-Z",
            "from_agent": "triage_agent",
            "created_at": timestamp,
            "input_json": {},
        },
    ]

    assert _latest_handoff(rows, "triage_agent")["handoff_id"] == "HANDOFF-A"


def _approval_marker(*, sequence: int = 41, attempt: int = 1) -> dict:
    return {
        "type": "human_approval",
        "approval_id": "POL-APP-007",
        "claim_token": f"claim-{attempt}",
        "attempt": attempt,
        "sequence": sequence,
    }


def test_latest_handoff_prefers_global_continuation_sequence_before_timestamp() -> None:
    marker = _approval_marker(sequence=41)
    rows = [
        {
            "handoff_id": "HANDOFF-Z",
            "from_agent": "response_agent",
            "created_at": "2026-08-14T12:00:01",
            "input_json": {},
            "output_json": {},
        },
        {
            "handoff_id": "HANDOFF-A",
            "from_agent": "response_agent",
            # A clock/timestamp tie or skew cannot make baseline state current.
            "created_at": "2026-08-14T12:00:00",
            "input_json": {"_continuation": marker},
            "output_json": {"_continuation": marker},
        },
    ]

    assert _latest_handoff(rows, "response_agent")["handoff_id"] == "HANDOFF-A"


def test_latest_handoff_uses_attempt_then_primary_key_for_same_second() -> None:
    timestamp = "2026-08-14T12:00:00"
    rows = [
        {
            "handoff_id": "HANDOFF-Z",
            "from_agent": "response_agent",
            "created_at": timestamp,
            "input_json": {"_continuation": _approval_marker(attempt=1)},
            "output_json": {"_continuation": _approval_marker(attempt=1)},
        },
        {
            "handoff_id": "HANDOFF-A",
            "from_agent": "response_agent",
            "created_at": timestamp,
            "input_json": {"_continuation": _approval_marker(attempt=2)},
            "output_json": {"_continuation": _approval_marker(attempt=2)},
        },
        {
            "handoff_id": "HANDOFF-B",
            "from_agent": "response_agent",
            "created_at": timestamp,
            "input_json": {"_continuation": _approval_marker(attempt=2)},
            "output_json": {"_continuation": _approval_marker(attempt=2)},
        },
    ]

    assert _latest_handoff(rows, "response_agent")["handoff_id"] == "HANDOFF-B"


def test_latest_handoff_rejects_disagreeing_input_and_output_markers() -> None:
    row = {
        "handoff_id": "HANDOFF-A",
        "from_agent": "response_agent",
        "created_at": NOW,
        "input_json": {"_continuation": _approval_marker(attempt=1)},
        "output_json": {"_continuation": _approval_marker(attempt=2)},
    }

    with pytest.raises(DashboardDataError, match="continuation markers disagree"):
        _latest_handoff([row], "response_agent")


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
            "response": {
                "channel": "email",
                "subject_line": "Your refund update",
                "body": "Your refund was processed.",
                "tone": "empathetic",
                "word_count": 4,
            },
            "content_checks": {
                "decision_reflected": True,
                "missing_info_requested": True,
                "safe_summary_reflected": True,
                "outcome_anchor_reflected": True,
                "pii_fields_detected": [],
                "forbidden_phrases": [],
            },
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
    assert detail["customerResponse"]["body"] == "Your refund was processed."
    assert detail["customerResponse"]["subjectLine"] == "Your refund update"
    assert detail["customerResponse"]["contentChecks"]["decision_reflected"] is True
    assert detail["policy"]["decision"]["type"] == "Approve"
    assert detail["refund"]["transactionId"] == "RF-DEMO-01"
    assert detail["policyDecision"] == "approve"
    assert detail["responseOutcome"] == "approved"
    assert "decisionOutcome" not in detail
    assert detail["executionOutcome"] == "refund_issued"
    assert detail["readOnly"] is True


def test_case_detail_projects_latest_human_approval_cycle_and_keeps_full_timeline():
    bundle = copy.deepcopy(_bundle("demo07"))
    marker = _approval_marker(sequence=41, attempt=2)
    timestamp = "2026-08-14T12:00:00"

    initial_response = json.loads(bundle["handoffs"][2]["output_json"])
    initial_response["response_result"].update(
        {
            "final_outcome": "manual_review",
            "workflow_status": "pending_human",
        }
    )
    initial_response["response_result"]["response"]["body"] = "Waiting for human review."
    bundle["handoffs"][2].update(
        {
            "handoff_id": "HANDOFF-Z",
            "created_at": timestamp,
            "output_json": json.dumps(initial_response),
        }
    )

    final_response = copy.deepcopy(initial_response)
    final_response["response_result"].update(
        {
            "final_outcome": "partial_refund",
            "workflow_status": "completed",
        }
    )
    final_response["response_result"]["response"]["body"] = "Your reviewed refund was issued."
    final_response["_continuation"] = marker
    bundle["handoffs"].append(
        {
            "handoff_id": "HANDOFF-A",
            "from_agent": "response_agent",
            "created_at": timestamp,
            "input_json": json.dumps({"_continuation": marker}),
            "output_json": json.dumps(final_response),
        }
    )
    bundle["approvals"] = [
        {
            "approval_id": "POL-APP-007",
            "trace_id": "demo07",
            "triggering_event_type": "policy_review",
            "status": "approved",
            "notes": "Approved after reviewing the evidence.",
            "created_at": NOW,
            "resolved_at": NOW,
        }
    ]
    bundle["governance_events"] = [
        {
            "event_id": "GOV-A",
            "trace_id": "demo07",
            "agent": "response_agent",
            "owasp_category": "ASI00",
            "trigger_score": 0.0,
            "interceptor_action": "allow",
            "flags_json": json.dumps({"summary": "Passed", "_continuation": marker}),
            "created_at": timestamp,
        },
        {
            "event_id": "GOV-Z",
            "trace_id": "demo07",
            "agent": "response_agent",
            "owasp_category": "ASI08",
            "trigger_score": 0.9,
            "interceptor_action": "block",
            "flags_json": json.dumps({"finding": {"flag": "excessive_autonomy"}}),
            "created_at": timestamp,
        },
    ]
    bundle["audit_log"] = [
        {
            "log_id": 3,
            "trace_id": "demo07",
            "event_type": "response_agent_evaluated",
            "agent": "response_agent",
            "payload_json": json.dumps({"output": initial_response}),
            "created_at": timestamp,
        },
        {
            "log_id": 41,
            "trace_id": "demo07",
            "event_type": "human_approval_continuation_claimed",
            "agent": "human_approval",
            "payload_json": json.dumps(
                {
                    "approval_id": marker["approval_id"],
                    "claim_token": marker["claim_token"],
                    "attempt": marker["attempt"],
                }
            ),
            "created_at": timestamp,
        },
        {
            "log_id": 48,
            "trace_id": "demo07",
            "event_type": "response_agent_evaluated",
            "agent": "response_agent",
            "payload_json": json.dumps(
                {"output": final_response, "_continuation": marker}
            ),
            "created_at": timestamp,
        },
    ]

    detail = build_case_detail(bundle)

    assert detail["status"] == "human_approved"
    assert detail["customerResponse"]["body"] == "Your reviewed refund was issued."
    assert detail["finalOutcome"] == "partial_refund"
    assert detail["governance"]["action"] == "allow"
    assert detail["riskTag"] is None
    assert len(detail["governanceEvents"]) == 2
    assert [row["event_id"] for row in detail["governanceEvents"]] == ["GOV-Z", "GOV-A"]
    assert "continuation" not in detail["governanceEvents"][0]
    assert detail["governanceEvents"][1]["continuation"] == marker
    assert [row["event_type"] for row in detail["notes"]] == [
        "response_agent_evaluated",
        "human_approval_continuation_claimed",
        "response_agent_evaluated",
    ]
    assert "continuation" not in detail["notes"][0]
    assert detail["notes"][1]["continuation"] == marker
    assert detail["notes"][2]["continuation"] == marker
    assert detail["notes"][0]["text"] == (
        "Response recorded that the case is waiting for human review."
    )
    assert detail["notes"][2]["text"] == "Final Response generated after human approval."
    assert all(note["time"] and note["text"] for note in detail["notes"])
    assert detail["pipeline"][5]["state"] == "done"
    assert detail["pipeline"][7]["state"] == "done"


def test_case_detail_prefers_successful_human_continuation_over_stale_failed_workflow() -> None:
    bundle = copy.deepcopy(_bundle("demo07"))
    bundle["workflow"]["status"] = "failed"

    final_response = json.loads(bundle["handoffs"][2]["output_json"])
    final_response["response_result"].update(
        {
            "final_outcome": "partial_refund",
            "workflow_status": "completed",
        }
    )
    bundle["handoffs"][2]["output_json"] = json.dumps(final_response)
    bundle["approvals"] = [
        {
            "approval_id": "POL-APP-007",
            "trace_id": "demo07",
            "triggering_event_type": "policy_review",
            "status": "approved",
            "notes": "Approved after reviewing the evidence.",
            "created_at": NOW,
            "resolved_at": NOW,
        }
    ]
    bundle["refunds"] = [
        {
            "transaction_id": "RF-DEMO-07",
            "trace_id": "demo07",
            "amount": 40.0,
            "currency": "USD",
            "status": "issued",
            "external_ref": "ext-demo07",
            "created_at": NOW,
        }
    ]

    detail = build_case_detail(bundle)

    assert detail["status"] == "human_approved"
    assert detail["refund"]["status"] == "issued"


def test_case_timeline_uses_numeric_audit_primary_key_on_same_second() -> None:
    bundle = _bundle("demo01")
    timestamp = "2026-08-14T12:00:00"
    bundle["audit_log"] = [
        {
            "log_id": 10,
            "trace_id": "demo01",
            "event_type": "response_agent_evaluated",
            "agent": "response_agent",
            "payload_json": "{}",
            "created_at": timestamp,
        },
        {
            "log_id": 9,
            "trace_id": "demo01",
            "event_type": "policy_agent_evaluated",
            "agent": "policy_agent",
            "payload_json": "{}",
            "created_at": timestamp,
        },
    ]

    detail = build_case_detail(bundle)

    assert [row["log_id"] for row in detail["notes"]] == [9, 10]


def test_case_timeline_keeps_parent_then_child_approval_lifecycle_order() -> None:
    bundle = _bundle("demo07")
    timestamp = "2026-08-14T12:00:00"
    parent = {
        **_approval_marker(sequence=100, attempt=1),
        "approval_id": "APP-Z-PARENT",
        "claim_token": "claim-parent",
    }
    child = {
        **_approval_marker(sequence=115, attempt=1),
        "approval_id": "APP-A-CHILD",
        "claim_token": "claim-child",
    }
    bundle["audit_log"] = [
        {
            "log_id": 110,
            "trace_id": "demo07",
            "event_type": "human_approval_resolved",
            "agent": "human_approval",
            "payload_json": json.dumps(
                {"approval_id": child["approval_id"], "decision": "approve"}
            ),
            "created_at": timestamp,
        },
        {
            "log_id": 105,
            "trace_id": "demo07",
            "event_type": "human_approval_continued",
            "agent": "human_approval",
            "payload_json": json.dumps(
                {"approval_id": parent["approval_id"], "_continuation": parent}
            ),
            "created_at": timestamp,
        },
        {
            "log_id": 115,
            "trace_id": "demo07",
            "event_type": "human_approval_continuation_claimed",
            "agent": "human_approval",
            "payload_json": json.dumps(
                {
                    "approval_id": child["approval_id"],
                    "claim_token": child["claim_token"],
                    "attempt": child["attempt"],
                }
            ),
            "created_at": timestamp,
        },
        {
            "log_id": 100,
            "trace_id": "demo07",
            "event_type": "human_approval_continuation_claimed",
            "agent": "human_approval",
            "payload_json": json.dumps(
                {
                    "approval_id": parent["approval_id"],
                    "claim_token": parent["claim_token"],
                    "attempt": parent["attempt"],
                }
            ),
            "created_at": timestamp,
        },
    ]

    detail = build_case_detail(bundle)

    assert [row["log_id"] for row in detail["notes"]] == [100, 105, 110, 115]
    assert [row["event_type"] for row in detail["notes"]] == [
        "human_approval_continuation_claimed",
        "human_approval_continued",
        "human_approval_resolved",
        "human_approval_continuation_claimed",
    ]
    assert detail["notes"][1]["continuation"] == parent
    assert "continuation" not in detail["notes"][2]
    assert detail["notes"][3]["continuation"] == child


def test_newer_policy_allow_clears_historical_governance_and_review_projection() -> None:
    bundle = copy.deepcopy(_bundle("demo07"))
    marker = _approval_marker(sequence=51, attempt=1)
    timestamp = "2026-08-14T12:00:00"

    initial_policy = json.loads(bundle["handoffs"][1]["output_json"])
    initial_policy["decision"].update(
        {"type": "manual_review", "reason": "A reviewer must inspect the evidence."}
    )
    initial_policy["policy_evaluation"]["gaps_or_conflicts"] = [
        {"type": "low_confidence", "detail": "Initial evidence was incomplete."}
    ]
    initial_policy["governance"] = {
        "semantic_drift_score": 0.91,
        "interceptor_action": "block",
        "flags": ["semantic_drift"],
    }
    bundle["handoffs"][1].update(
        {
            "handoff_id": "POLICY-Z",
            "created_at": timestamp,
            "output_json": json.dumps(initial_policy),
        }
    )

    final_policy = copy.deepcopy(initial_policy)
    final_policy["decision"].update(
        {"type": "approve", "reason": "Reviewer evidence cleared the request."}
    )
    final_policy["policy_evaluation"]["gaps_or_conflicts"] = []
    final_policy["governance"] = {
        "semantic_drift_score": 0.0,
        "interceptor_action": "allow",
        "flags": [],
    }
    final_policy["_continuation"] = marker
    bundle["handoffs"].append(
        {
            "handoff_id": "POLICY-A",
            "from_agent": "policy_agent",
            "created_at": timestamp,
            "input_json": json.dumps({"_continuation": marker}),
            "output_json": json.dumps(final_policy),
        }
    )
    bundle["governance_events"] = [
        {
            "event_id": "TRIAGE-GOV",
            "trace_id": "demo07",
            "agent": "triage_agent",
            "owasp_category": "ASI00",
            "trigger_score": 0.0,
            "interceptor_action": "allow",
            "flags_json": json.dumps({"summary": "Passed"}),
            "created_at": timestamp,
        },
        {
            "event_id": "POLICY-GOV-Z",
            "trace_id": "demo07",
            "agent": "policy_agent",
            "owasp_category": "ASI01",
            "trigger_score": 0.91,
            "interceptor_action": "block",
            "flags_json": json.dumps({"finding": {"flag": "semantic_drift"}}),
            "created_at": timestamp,
        },
    ]
    bundle["policy_reviews"] = [
        {
            "policy_review_event_id": "POL-REV-Z",
            "trace_id": "demo07",
            "review_type": "low_confidence",
            "detail": "Initial evidence was incomplete.",
            "evidence_json": json.dumps(
                {"gaps_or_conflicts": initial_policy["policy_evaluation"]["gaps_or_conflicts"]}
            ),
            "created_at": timestamp,
        }
    ]

    detail = build_case_detail(bundle)

    assert detail["policy"]["decision"]["type"] == "Approve"
    assert detail["policy"]["gaps"] == []
    assert detail["hasGaps"] is False
    assert detail["governance"]["action"] == "allow"
    assert detail["riskTag"] is None
    assert detail["pipeline"][4]["state"] == "done"
    assert len(detail["policyReviews"]) == 1
    assert detail["policyReviews"][0]["detail"] == "Initial evidence was incomplete."


def test_latest_refund_uses_approval_continuation_audit_sequence() -> None:
    bundle = copy.deepcopy(_bundle("demo07"))
    marker = _approval_marker(sequence=61, attempt=2)
    timestamp = "2026-08-14T12:00:00"
    bundle["approvals"] = [
        {
            "approval_id": marker["approval_id"],
            "trace_id": "demo07",
            "triggering_event_type": "policy_review",
            "status": "approved",
            "notes": "Approved after review.",
            "created_at": NOW,
            "resolved_at": NOW,
        }
    ]
    bundle["refunds"] = [
        {
            "transaction_id": "REFUND-Z",
            "trace_id": "demo07",
            "status": "failed",
            "amount": 80.0,
            "currency": "USD",
            "external_ref": None,
            "created_at": timestamp,
        },
        {
            "transaction_id": "REFUND-A",
            "trace_id": "demo07",
            "status": "issued",
            "amount": 40.0,
            "currency": "USD",
            "external_ref": "REVIEWED",
            "created_at": timestamp,
        },
    ]
    bundle["audit_log"] = [
        {
            "log_id": 9,
            "trace_id": "demo07",
            "event_type": "refund_failed",
            "agent": "refund_agent",
            "payload_json": json.dumps(
                {
                    "transaction_id": "REFUND-Z",
                    "handoff_id": "REFUND-HANDOFF-Z",
                    "refund_result": {"status": "failed", "amount": 80.0},
                }
            ),
            "created_at": timestamp,
        },
        {
            "log_id": 65,
            "trace_id": "demo07",
            "event_type": "refund_issued",
            "agent": "refund_agent",
            "payload_json": json.dumps(
                {
                    "transaction_id": "REFUND-A",
                    "handoff_id": "REFUND-HANDOFF-A",
                    "refund_result": {"status": "issued", "amount": 40.0},
                    "_continuation": marker,
                }
            ),
            "created_at": timestamp,
        },
    ]

    detail = build_case_detail(bundle)

    assert detail["status"] == "human_approved"
    assert detail["refund"]["transactionId"] == "REFUND-A"
    assert detail["refund"]["amount"] == 40.0
    assert [row["transaction_id"] for row in detail["refundTransactions"]] == [
        "REFUND-Z",
        "REFUND-A",
    ]


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
    assert detail["approvals"][0]["requested_amount"] is None
    assert detail["approvals"][0]["ticket_requested_amount"] == 80
    assert detail["approvals"][0]["amount_paid"] == 80
    assert detail["approvals"][0]["remaining_refundable"] == 80
    assert detail["governance"]["triggerScore"] == 0.91
    assert detail["pipeline"][4]["state"] == "blocked"
    assert detail["pipeline"][5]["state"] == "current"


def test_pending_approval_keeps_authoritative_amount_nullable_and_ticket_context_separate():
    approval = normalize_approval_row(
        {
            "approval_id": "approval-demo20",
            "trace_id": "demo20",
            "status": "pending",
            "amount_requested": None,
            "ticket_requested_amount": 210,
            "ticket_currency": "USD",
            "order_amount_paid": 210,
            "order_prior_refund_total": 25,
            "order_currency": "USD",
            "notes": None,
            "policy_ids_json": None,
        }
    )

    assert approval["requested_amount"] is None
    assert approval["ticket_requested_amount"] == 210
    assert approval["amount_paid"] == 210
    assert approval["prior_refund_total"] == 25
    assert approval["remaining_refundable"] == 185
    assert approval["currency"] == "USD"
    assert "order_amount_paid" not in approval


def test_pending_approval_requested_amount_precedes_ticket_fallback():
    approval = normalize_approval_row(
        {
            "approval_id": "approval-demo08",
            "trace_id": "demo08",
            "status": "pending",
            "amount_requested": 180,
            "ticket_requested_amount": 120,
            "ticket_currency": "USD",
            "order_amount_paid": 120,
            "order_prior_refund_total": 0,
            "order_currency": "USD",
            "notes": None,
            "policy_ids_json": None,
        }
    )

    assert approval["requested_amount"] == 180
    assert approval["remaining_refundable"] == 120


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


def _completed_customer_followup_bundle() -> dict:
    bundle = copy.deepcopy(_bundle("demo10"))
    marker = {"type": "customer_followup", "claim_token": "followup-claim-demo10"}

    initial_envelope = json.loads(bundle["handoffs"][2]["output_json"])
    initial_envelope["response_result"].update(
        {"final_outcome": "need_info", "workflow_status": "waiting_user"}
    )
    initial_envelope["response_result"]["response"]["body"] = (
        "Please tell us what went wrong and the amount requested."
    )
    bundle["handoffs"][2]["output_json"] = json.dumps(initial_envelope)

    followup_envelope = copy.deepcopy(initial_envelope)
    followup_envelope["response_result"].update(
        {"final_outcome": "approved", "workflow_status": "completed"}
    )
    followup_envelope["response_result"]["response"]["body"] = (
        "Thanks for the additional details. Your refund was issued."
    )
    followup_envelope["_continuation"] = marker
    bundle["handoffs"].append(
        {
            "handoff_id": "4",
            "from_agent": "response_agent",
            "input_json": json.dumps({"_continuation": marker}),
            "output_json": json.dumps(followup_envelope),
            "created_at": NOW,
        }
    )
    bundle["audit_log"] = [
        {
            "log_id": 1,
            "trace_id": "demo10",
            "event_type": "response_agent_evaluated",
            "agent": "response_agent",
            "payload_json": json.dumps({"output": initial_envelope}),
            "created_at": NOW,
        },
        {
            "log_id": 2,
            "trace_id": "demo10",
            "event_type": "customer_followup_received",
            "agent": "customer",
            "payload_json": "{}",
            "created_at": NOW,
        },
        {
            "log_id": 3,
            "trace_id": "demo10",
            "event_type": "response_agent_evaluated",
            "agent": "response_agent",
            "payload_json": json.dumps(
                {"output": followup_envelope, "_continuation": marker}
            ),
            "created_at": NOW,
        },
        {
            "log_id": 4,
            "trace_id": "demo10",
            "event_type": "customer_followup_completed",
            "agent": "customer",
            "payload_json": "{}",
            "created_at": NOW,
        },
    ]
    return bundle


def test_customer_followup_has_distinct_status_and_preserves_two_stage_lineage():
    detail = build_case_detail(_completed_customer_followup_bundle())

    assert detail["status"] == "followup_approved"
    assert detail["statusSource"] == "Customer Follow-up"
    assert detail["resolutionPath"] == "customer_followup"
    assert detail["resolutionLineage"]["type"] == "customer_followup"
    assert {
        key: detail["resolutionLineage"]["initial"][key]
        for key in ("status", "finalOutcome", "workflowStatus")
    } == {
        "status": "needs_info",
        "finalOutcome": "need_info",
        "workflowStatus": "waiting_user",
    }
    assert {
        key: detail["resolutionLineage"]["followup"][key]
        for key in ("status", "finalOutcome", "workflowStatus")
    } == {
        "status": "followup_approved",
        "finalOutcome": "approved",
        "workflowStatus": "completed",
    }
    assert detail["resolutionLineage"]["initial"]["customerResponse"]["body"] == (
        "Please tell us what went wrong and the amount requested."
    )
    assert detail["customerResponse"]["body"] == (
        "Thanks for the additional details. Your refund was issued."
    )
    assert [note["text"] for note in detail["notes"]] == [
        "Response requested additional information from the customer.",
        "Customer follow-up was received and preserved.",
        "Final Response generated after customer follow-up.",
        "The customer follow-up continuation completed.",
    ]


def test_customer_followup_remains_in_no_human_throughput_but_not_auto_approved():
    metrics = DashboardService(
        FakeRepository([_bundle("demo01"), _completed_customer_followup_bundle()])
    ).metrics()

    assert metrics["primaryStats"][0] == {
        "label": "Automated Throughput",
        "value": "100%",
        "detail": "2 of 2 cases completed without human review; 1 after customer follow-up",
    }
    assert metrics["statusBreakdown"] == [
        {"status": "followup_approved", "count": 1},
        {"status": "auto_approved", "count": 1},
    ]


def test_completed_policy_denial_counts_as_no_human_but_human_denial_does_not():
    policy_denied = _bundle("demo04")
    policy = json.loads(policy_denied["handoffs"][1]["output_json"])
    policy["decision"].update({"type": "deny", "refund_amount": 0.0})
    policy["handoff"].update({"next_agent": "response_agent", "reason": "Denied"})
    policy_denied["handoffs"][1]["output_json"] = json.dumps(policy)
    response = json.loads(policy_denied["handoffs"][2]["output_json"])
    response["response_result"].update(
        {"final_outcome": "denied", "workflow_status": "completed"}
    )
    policy_denied["handoffs"][2]["output_json"] = json.dumps(response)
    policy_denied["refunds"] = []

    human_denied = copy.deepcopy(policy_denied)
    human_denied["workflow"]["trace_id"] = "demo20"
    marker = {"type": "human_approval", "claim_token": "human-denial-demo20"}
    human_response = json.loads(human_denied["handoffs"][2]["output_json"])
    human_response["_continuation"] = marker
    human_denied["handoffs"][2]["input_json"] = json.dumps({"_continuation": marker})
    human_denied["handoffs"][2]["output_json"] = json.dumps(human_response)
    human_denied["approvals"] = [
        {
            "approval_id": "APP-DEMO20",
            "trace_id": "demo20",
            "status": "rejected",
            "created_at": NOW,
            "resolved_at": NOW,
        }
    ]

    automated = _bundle("demo01")
    policy_denied_summary = build_case_detail(policy_denied)
    human_denied_summary = build_case_detail(human_denied)
    metrics = DashboardService(
        FakeRepository([automated, policy_denied, human_denied])
    ).metrics()

    assert policy_denied_summary["status"] == "rejected"
    assert policy_denied_summary["completedWithoutHuman"] is True
    assert human_denied_summary["status"] == "rejected"
    assert human_denied_summary["resolutionPath"] == "human_approval"
    assert human_denied_summary["completedWithoutHuman"] is False
    assert metrics["primaryStats"][0] == {
        "label": "Automated Throughput",
        "value": "67%",
        "detail": "2 of 3 cases completed without human review; 1 completed denial",
    }


def test_naive_mysql_timestamps_are_rendered_as_utc_not_local_future_time():
    five_minutes_ago_utc = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).replace(tzinfo=None)

    assert _relative_time(five_minutes_ago_utc) == "5m ago"
    assert _iso(datetime(2026, 8, 15, 4, 0, 0)) == "2026-08-15T04:00:00Z"
    bundle = _bundle()
    bundle["workflow"]["updated_at"] = datetime(2026, 8, 15, 4, 0, 0)
    assert build_case_detail(bundle)["updatedAt"] == "2026-08-15T04:00:00Z"


def test_future_timestamp_is_fail_visible_after_utc_normalization():
    three_minutes_from_now = (
        datetime.now(timezone.utc) + timedelta(minutes=3)
    ).replace(tzinfo=None)

    assert _relative_time(three_minutes_from_now) == "in 3m"


def test_case_updated_at_uses_latest_persisted_lifecycle_event():
    bundle = _bundle()
    bundle["workflow"]["updated_at"] = datetime(2026, 8, 15, 4, 0, 0)
    bundle["audit_log"][0]["created_at"] = datetime(2026, 8, 15, 4, 5, 0)

    detail = build_case_detail(bundle)

    assert detail["updatedAt"] == "2026-08-15T04:05:00Z"


def test_case_list_sorts_by_projected_lifecycle_timestamp_then_trace():
    stale_workflow_new_event = _bundle("demo02")
    stale_workflow_new_event["workflow"]["updated_at"] = datetime(2026, 8, 15, 4, 0, 0)
    stale_workflow_new_event["audit_log"][0]["created_at"] = datetime(2026, 8, 15, 4, 8, 0)
    newer_workflow_old_event = _bundle("demo01")
    newer_workflow_old_event["workflow"]["updated_at"] = datetime(2026, 8, 15, 4, 5, 0)
    newer_workflow_old_event["audit_log"][0]["created_at"] = datetime(2026, 8, 15, 4, 5, 0)

    cases = DashboardService(
        FakeRepository([newer_workflow_old_event, stale_workflow_new_event])
    ).list_cases()

    assert [case["traceId"] for case in cases] == ["demo02", "demo01"]
    assert [case["updatedAt"] for case in cases] == [
        "2026-08-15T04:08:00Z",
        "2026-08-15T04:05:00Z",
    ]


def test_missing_requested_amount_stays_nullable_instead_of_becoming_zero():
    bundle = _bundle("demo10")
    triage = json.loads(bundle["handoffs"][0]["output_json"])
    triage["triage_output"]["customer_request"]["requested_amount"] = None
    bundle["handoffs"][0]["output_json"] = json.dumps(triage)
    policy = json.loads(bundle["handoffs"][1]["output_json"])
    policy["customer_request"]["requested_amount"] = None
    bundle["handoffs"][1]["output_json"] = json.dumps(policy)
    bundle["ticket"]["requested_amount"] = None

    detail = build_case_detail(bundle)

    assert detail["amount"] is None
    assert detail["request"]["requestedAmount"] is None


def test_triage_governance_block_is_not_mislabeled_as_pending_policy():
    bundle = _bundle("demo12")
    bundle["handoffs"] = [
        row for row in bundle["handoffs"] if row["from_agent"] != "policy_agent"
    ]
    response = json.loads(bundle["handoffs"][-1]["output_json"])
    response["response_result"].update(
        {"final_outcome": "manual_review", "workflow_status": "pending_human"}
    )
    bundle["handoffs"][-1]["output_json"] = json.dumps(response)
    bundle["governance_events"] = [
        {
            "event_id": "GOV-TRIAGE-BLOCK",
            "trace_id": "demo12",
            "agent": "triage_agent",
            "owasp_category": "ASI01",
            "trigger_score": 0.98,
            "interceptor_action": "block",
            "flags_json": json.dumps({"finding": {"flag": "goal_hijack"}}),
            "created_at": NOW,
        }
    ]
    bundle["approvals"] = [
        {
            "approval_id": "APP-DEMO12",
            "trace_id": "demo12",
            "status": "pending",
            "created_at": NOW,
        }
    ]
    bundle["refunds"] = []
    bundle["workflow"].update(
        {"status": "pending_human", "current_agent": "human_approval"}
    )

    detail = build_case_detail(bundle)

    assert detail["policy"]["evaluated"] is False
    assert detail["policy"]["blockedBeforeEvaluation"] is True
    assert detail["reason"] == "damaged"
    assert detail["reasonLabel"] == "Prompt Injection"
    assert detail["policy"]["decision"] == {
        "type": "Not Evaluated",
        "amount": 0.0,
        "confidence": "-",
        "reasonText": "Blocked before Policy evaluation by Triage Governance.",
    }
    assert detail["pipeline"][2]["state"] == "blocked"
    assert detail["pipeline"][3]["state"] == "skipped"
    assert detail["pipeline"][4]["state"] == "skipped"
    assert detail["pipeline"][5]["state"] == "current"
    assert detail["pipeline"][7]["state"] == "pending"


def test_governance_null_score_and_typed_approval_trigger_remain_explicit():
    bundle = copy.deepcopy(_bundle("demo13"))
    bundle["handoffs"] = [bundle["handoffs"][0], bundle["handoffs"][2]]
    response = json.loads(bundle["handoffs"][1]["output_json"])
    response["response_result"].update(
        {"final_outcome": "manual_review", "workflow_status": "pending_human"}
    )
    bundle["handoffs"][1]["output_json"] = json.dumps(response)
    bundle["governance_events"] = [
        {
            "event_id": "GOV-DEMO13-PII",
            "trace_id": "demo13",
            "agent": "triage_agent",
            "owasp_category": "ASI07",
            "trigger_score": None,
            "interceptor_action": "block",
            "flags_json": json.dumps(
                {"finding": {"flag": "data_leakage", "detail": "Ownership mismatch"}}
            ),
            "offending_content": "redacted customer reference",
            "created_at": NOW,
        }
    ]
    bundle["approvals"] = [
        {
            "approval_id": "APP-DEMO13",
            "trace_id": "demo13",
            "triggering_event_id": "GOV-DEMO13-PII",
            "triggering_event_type": "governance",
            "reason": "Ownership review required",
            "status": "pending",
            "created_at": NOW,
        }
    ]
    bundle["refunds"] = []
    bundle["workflow"].update(
        {"status": "pending_human", "current_agent": "human_approval"}
    )

    detail = build_case_detail(bundle)

    assert detail["governance"]["triggerScore"] is None
    assert detail["reason"] == "damaged"
    assert detail["reasonLabel"] == "PII / Account Mismatch"
    assert detail["approvals"][0]["trigger"] == {
        "type": "governance",
        "category": "ASI07",
        "score": None,
        "action": "block",
        "detail": "redacted customer reference",
    }


def test_resolved_governance_trigger_is_history_not_current_case_state():
    bundle = copy.deepcopy(_bundle("demo20"))
    policy = json.loads(bundle["handoffs"][1]["output_json"])
    policy["decision"]["type"] = "manual_review"
    policy["governance"] = {
        "semantic_drift_score": 0.93,
        "interceptor_action": "block",
        "flags": ["excessive_autonomy"],
    }
    bundle["handoffs"][1]["output_json"] = json.dumps(policy)
    response = json.loads(bundle["handoffs"][2]["output_json"])
    response["response_result"].update(
        {"final_outcome": "denied", "workflow_status": "completed"}
    )
    bundle["handoffs"][2]["output_json"] = json.dumps(response)
    bundle["governance_events"] = [
        {
            "event_id": "GOV-DEMO20-REVIEWED",
            "trace_id": "demo20",
            "agent": "policy_agent",
            "owasp_category": "ASI08",
            "trigger_score": 0.93,
            "interceptor_action": "block",
            "flags_json": json.dumps(
                {"finding": {"flag": "excessive_autonomy", "detail": "Review required"}}
            ),
            "created_at": NOW,
        }
    ]
    bundle["approvals"] = [
        {
            "approval_id": "APP-DEMO20",
            "trace_id": "demo20",
            "triggering_event_id": "GOV-DEMO20-REVIEWED",
            "triggering_event_type": "governance",
            "reason": "Review required",
            "status": "rejected",
            "decision": "deny",
            "reviewer": "reviewer@example.test",
            "notes": "Denied after review.",
            "resolved_at": NOW,
            "created_at": NOW,
        }
    ]
    bundle["refunds"] = []

    detail = build_case_detail(bundle)

    assert detail["status"] == "rejected"
    assert detail["statusSource"] == "Human Approval"
    assert detail["riskTag"] is None
    assert detail["hasFlags"] is False
    assert detail["governance"]["action"] == "allow"
    assert detail["governance"]["triggerScore"] is None
    assert detail["governance"]["flags"] == []
    assert [row["event_id"] for row in detail["governanceEvents"]] == [
        "GOV-DEMO20-REVIEWED"
    ]
    assert detail["approvals"][0]["trigger"]["category"] == "ASI08"


def test_policy_review_fallback_is_visible_as_current_rationale():
    bundle = copy.deepcopy(_bundle("demo08"))
    bundle["policy_reviews"] = [
        {
            "policy_review_event_id": "POL-REV-DEMO08",
            "trace_id": "demo08",
            "review_type": "policy_conflict",
            "detail": "Two refund rules require reviewer interpretation.",
            "policy_ids_json": json.dumps(["POL-A", "POL-B"]),
            "created_at": NOW,
        }
    ]
    bundle["approvals"] = [
        {
            "approval_id": "APP-DEMO08",
            "trace_id": "demo08",
            "triggering_event_id": "POL-REV-DEMO08",
            "triggering_event_type": "policy_review",
            "status": "pending",
            "created_at": NOW,
        }
    ]

    detail = build_case_detail(bundle)

    assert detail["hasGaps"] is True
    assert detail["policy"]["gaps"] == [
        {
            "type": "Policy Conflict",
            "detail": "Two refund rules require reviewer interpretation.",
        }
    ]
    assert detail["approvals"][0]["trigger"] == {
        "type": "policy_review",
        "reviewType": "policy_conflict",
        "detail": "Two refund rules require reviewer interpretation.",
        "policyIds": ["POL-A", "POL-B"],
    }


def test_refund_failure_is_operational_not_a_policy_rejection():
    bundle = copy.deepcopy(_bundle("demo06"))
    response = json.loads(bundle["handoffs"][2]["output_json"])
    response["response_result"].update(
        {"final_outcome": "refund_failed", "workflow_status": "failed"}
    )
    bundle["handoffs"][2]["output_json"] = json.dumps(response)
    bundle["workflow"].update({"status": "failed", "current_agent": "refund_agent"})
    bundle["refunds"][0].update({"status": "failed", "external_ref": None})

    detail = build_case_detail(bundle)

    assert detail["status"] == "execution_failed"
    assert detail["statusSource"] == "Refund Agent"
    assert detail["executionOutcome"] == "refund_failed"
    assert detail["completedWithoutHuman"] is False
    assert detail["pipeline"][6]["state"] == "blocked"


def test_auditability_requires_evidence_for_every_case_and_human_events_are_admin():
    first = _bundle("demo01")
    second = _bundle("demo02")
    second["audit_log"] = []
    service = DashboardService(FakeRepository([first, second]))

    auditability = service.metrics()["primaryStats"][2]
    assert auditability == {
        "label": "System Auditability",
        "value": "Incomplete",
        "detail": "1 of 2 cases have audit evidence; 1 events total",
    }

    first["audit_log"] = [
        {
            "log_id": 9,
            "trace_id": "demo01",
            "event_type": "human_approval_resolved",
            "agent": "human_approval",
            "payload_json": json.dumps({"decision": "approve"}),
            "created_at": NOW,
        }
    ]
    assert DashboardService(FakeRepository([first])).audit()[0]["category"] == "Admin"


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
