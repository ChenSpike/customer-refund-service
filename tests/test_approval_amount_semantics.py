from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_approval_form_disables_unsafe_full_approval_and_requires_exact_amount() -> None:
    source = (ROOT / "frontend/src/components/ApprovalResolutionForm.jsx").read_text(
        encoding="utf-8"
    )

    assert "requestedExceedsRemaining" in source
    assert 'disabled={refundRoute && !financials.fullApprovalAvailable}' in source
    assert "Full approval is unavailable" in source
    assert "Choose Approve partial refund" in source
    assert "amountInCents(resolvedAmount) !== amountInCents(financials.fullApprovalAmount)" in source
    assert "readOnly={decision === 'approve'}" in source


def test_approval_form_uses_remaining_balance_as_full_target_when_request_is_missing() -> None:
    source = (ROOT / "frontend/src/components/ApprovalResolutionForm.jsx").read_text(
        encoding="utf-8"
    )

    assert "const fullApprovalAmount = requested === null ? remaining : requested" in source
    assert "full remaining refundable amount" in source


def test_approval_form_treats_zero_requested_amount_as_non_actionable() -> None:
    source = (ROOT / "frontend/src/components/ApprovalResolutionForm.jsx").read_text(
        encoding="utf-8"
    )

    assert "requestedAmountInvalid" in source
    assert 'disabled={financials.requestedAmountInvalid}' in source
    assert "persisted requested amount must be greater than zero" in source
    assert "Deny this request or correct the upstream case data" in source
