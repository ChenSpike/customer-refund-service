from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_dashboard_badge_uses_pending_endpoint_and_includes_quarantine_fallback() -> None:
    source = _source("frontend/src/App.jsx")

    assert "getPendingApprovals" in source
    assert "setPendingApprovalCount" in source
    assert "response.data.length" in source
    assert "caseItem.status === 'quarantined'" in source


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
