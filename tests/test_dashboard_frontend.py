from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_dashboard_badge_uses_pending_endpoint_and_includes_quarantine_fallback() -> None:
    source = _source("frontend/src/App.jsx")

    assert "getPendingApprovals" in source
    assert "setPendingApprovalCount" in source
    assert "response.data.length" in source
    assert "caseItem.status === 'pending_review'" in source
    assert "caseItem.status === 'quarantined'" in source
    assert "Promise.all([loadCases(), loadPendingApprovalCount(), loadHealth()])" in source


def test_dashboard_supports_exact_demo_trace_deep_links() -> None:
    source = _source("frontend/src/App.jsx")

    assert "new URLSearchParams(window.location.search).get('trace')" in source
    assert "url.searchParams.set('trace', traceId)" in source
    assert "url.searchParams.delete('trace')" in source
    assert "^demo(?:0[1-9]|1[0-9]|20)$" in source


def test_approval_form_shows_and_enforces_canonical_financial_limits() -> None:
    source = _source("frontend/src/components/ApprovalResolutionForm.jsx")

    for field in (
        "requested_amount",
        "amount_paid",
        "prior_refund_total",
        "remaining_refundable",
    ):
        assert field in source
    assert "amountSuggestion" in source
    assert "Number(resolvedAmount) > financials.remaining" in source
    assert "Remaining refundable" in source


def test_pending_approval_cards_use_normalized_amount_contract() -> None:
    source = _source("frontend/src/pages/PendingApprovals.jsx")

    assert "approval.requested_amount" in source
    assert "approval.remaining_refundable" in source
    assert "approval.amount_requested" not in source


def test_case_detail_displays_the_persisted_customer_response_and_checks() -> None:
    source = _source("frontend/src/pages/CaseDetail.jsx")

    assert "Customer Response" in source
    assert "detail.customerResponse.body" in source
    assert "responseChecksPassed" in source
    assert "Semantic checks:" in source


def test_dashboard_distinguishes_customer_followup_resolution_everywhere() -> None:
    theme = _source("frontend/src/theme.js")
    overview = _source("frontend/src/pages/Overview.jsx")
    detail = _source("frontend/src/pages/CaseDetail.jsx")

    assert "followup_approved: { label: 'Follow-up Approved'" in theme
    assert "{ key: 'followup_approved', label: 'Follow-up Approved'" in overview
    assert "cases.filter((c) => c.status === 'followup_approved').length" in overview
    assert "cases.filter((c) => c.completedWithoutHuman).length" in overview
    assert "counts.all - counts.auto_approved - counts.followup_approved" in overview
    assert "detail.status === 'followup_approved'" in detail
    assert "Two-stage customer resolution" in detail
    assert "Original request-info response" in detail
    assert "initial.customerResponse?.body" in detail


def test_no_human_completion_includes_completed_denials_without_renaming_them() -> None:
    overview = _source("frontend/src/pages/Overview.jsx")

    assert "cases.filter((c) => c.completedWithoutHuman).length" in overview
    assert "Completed approvals and denials without a reviewer" in overview


def test_case_detail_calls_out_triage_block_before_policy() -> None:
    source = _source("frontend/src/pages/CaseDetail.jsx")

    assert "detail.policy?.blockedBeforeEvaluation" in source
    assert "Triage Governance blocked this case before it reached the Policy Agent" in source


def test_requested_amount_labels_do_not_imply_the_full_amount_was_issued() -> None:
    theme = _source("frontend/src/theme.js")
    overview = _source("frontend/src/pages/Overview.jsx")
    detail = _source("frontend/src/pages/CaseDetail.jsx")
    approvals = _source("frontend/src/components/ApprovalResolutionForm.jsx")
    pending = _source("frontend/src/pages/PendingApprovals.jsx")

    assert "export function requestedMoney" in theme
    assert "return 'Not provided'" in theme
    assert "<div>Requested</div><div>Status</div>" in overview
    assert "· requested <span" in overview
    assert "· requested{' '}" in detail
    assert "requestedMoney(detail.request?.requestedAmount" in detail
    assert "optionalNumericAmount" in approvals
    assert "function requestedAmountSource" in approvals
    assert "hasOwnProperty.call(approval, 'requested_amount')" in approvals
    assert "requestedAmountSource(approval, requestedAmount)" in approvals
    assert "requestedMoney(value, currency)" in approvals
    assert "requestedMoney(approval.requested_amount" in pending


def test_governance_feed_renders_the_persisted_event_endpoint_not_case_tags() -> None:
    source = _source("frontend/src/pages/GovernanceEvents.jsx")

    assert "queryGovernanceEvents({ limit: 1000 })" in source
    assert "events.map((event)" in source
    assert "event.event_id" in source
    assert "event.interceptor_action" in source
    assert "event.trigger_score !== null" in source
    assert "'N/A'" in source
    assert "persisted event" in source
    assert "cases.filter((c) => c.riskTag)" not in source


def test_human_review_trigger_and_resolution_evidence_is_visible() -> None:
    detail = _source("frontend/src/pages/CaseDetail.jsx")
    pending = _source("frontend/src/pages/PendingApprovals.jsx")
    evidence = _source("frontend/src/components/ApprovalTriggerEvidence.jsx")

    assert "<ApprovalTriggerEvidence approval={pendingApproval}" in detail
    assert "Human Review History" in detail
    assert "approval.resolved_amount" in detail
    assert "approval.reviewer" in detail
    assert "approval.notesPayload?.text" in detail
    assert "approval.resolved_at" in detail
    assert "<ApprovalTriggerEvidence approval={approval}" in pending
    assert "trigger.policyIds" in evidence
    assert "trigger.category" in evidence
    assert "trigger.score !== null" in evidence


def test_pending_filter_includes_governance_holds_and_execution_failure_is_distinct() -> None:
    overview = _source("frontend/src/pages/Overview.jsx")
    detail = _source("frontend/src/pages/CaseDetail.jsx")
    theme = _source("frontend/src/theme.js")

    assert "c.status === 'manual_review' || c.status === 'quarantined'" in overview
    assert "{ key: 'execution_failed', label: 'Execution Failed'" in overview
    assert "execution_failed: { label: 'Execution Failed'" in theme
    assert "detail.status === 'execution_failed'" in detail
    assert "Operational execution failed" in detail


def test_null_governance_score_is_not_rendered_as_zero() -> None:
    source = _source("frontend/src/pages/CaseDetail.jsx")

    assert "rawGovernanceScore !== null" in source
    assert "hasGovernanceScore ? governanceScore.toFixed(2) : 'N/A'" in source


def test_metrics_describes_historical_persisted_governance_events() -> None:
    source = _source("frontend/src/pages/Metrics.jsx")

    assert "Persisted Blocks by OWASP Category" in source
    assert "Historical persisted block or quarantine events" in source
    assert "Governance Holds by OWASP Category" not in source
