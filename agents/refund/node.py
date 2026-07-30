from agents.refund.models import RefundRequest
from tools.refund_executor import execute_refund


def refund_node(state) -> dict:
	policy_decision = state.get("policy_decision", {})
	order_lookup_result = state.get("order_lookup_result", {})
	order_id = state.get("requested_order_id") or order_lookup_result.get("order_id", "")
	currency = order_lookup_result.get("currency") or "USD"

	refund_request = RefundRequest(
		trace_id=state.get("trace_id", ""),
		ticket_id=state.get("ticket_id", ""),
		order_id=order_id,
		amount=policy_decision.get("refund_amount", 0),
		currency=currency,
	)
	refund_result = execute_refund(refund_request)

	final_outcome = "refund_failed"
	if refund_result.status == "success":
		final_outcome = policy_decision.get("decision", "approved")

	return {
		"current_stage": "refund_agent",
		"refund_result": refund_result.model_dump(mode="json"),
		"final_outcome": final_outcome,
		"workflow_status": "running",
	}
