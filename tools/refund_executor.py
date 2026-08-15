from datetime import datetime, timezone

from agents.refund.models import RefundRequest, RefundResult


def execute_refund(refund_request: RefundRequest) -> RefundResult:
    if refund_request.amount <= 0:
        return RefundResult(
            status="failed",
            refund_id=None,
            order_id=refund_request.order_id,
            amount=refund_request.amount,
            currency=refund_request.currency,
            processed_at=datetime.now(timezone.utc),
            message="Refund execution failed.",
            failure_code="INVALID_REFUND_AMOUNT",
        )

    refund_id = f"RF-{refund_request.ticket_id}"
    return RefundResult(
        status="success",
        refund_id=refund_id,
        order_id=refund_request.order_id,
        amount=refund_request.amount,
        currency=refund_request.currency,
        processed_at=datetime.now(timezone.utc),
        message="Refund processed successfully.",
        failure_code=None,
    )