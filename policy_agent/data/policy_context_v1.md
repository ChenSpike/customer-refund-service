# Refund Policy Knowledge Base v1.0

Human owners define refund rules here. Azure applies these rules to the validated customer request and order facts.

## Decision Rules

- `R-APPROVE-DAMAGED-30D`: approve when `refund_reason = damaged`, the purchase is within 30 days, the request is not more than unpaid order value, and there is no prior refund conflict.
- `R-APPROVE-WRONG-ITEM-30D`: approve when `refund_reason = wrong_item`, the purchase is within 30 days, and linked order facts do not conflict.
- `R-APPROVE-NOT-DELIVERED-OPEN`: approve when `refund_reason = not_delivered_within_timeframe` and `item_status = unknown`.
- `R-DENY-DISSATISFACTION`: deny when `refund_reason = doesnt_like_it` and `item_status = delivered`, unless a review rule applies.
- `R-DENY-OUTSIDE-WINDOW`: deny when the request is outside the 30-day window, unless a review rule applies.
- `R-DENY-DUPLICATE`: deny when `prior_refund_total >= amount_paid`.
- `R-REQUEST-MISSING-FACTS`: request information when the reason, amount, or linked order facts are insufficient for a clean decision.

## Rule Precedence

- `R-DENY-DUPLICATE` is a clean denial. Do not use `R-REVIEW-OVER-REQUEST` only because unpaid order value is zero.
- `R-DENY-OUTSIDE-WINDOW` and `R-DENY-DISSATISFACTION` are clean denials unless a review rule or verified fact conflict applies.
- `R-REVIEW-CONFLICT` takes precedence over an approval rule when the customer supplies a different refund-order ID or says the refund-order ID may be wrong.
- Reporting information explicitly described as belonging to another customer is not a refund-policy conflict; it is evaluated by the separate governance layer.

## Human Review Rules

- `R-REVIEW-HIGH-VALUE`: manual review when `requested_amount >= 500` or `amount_paid >= 500`.
- `R-REVIEW-OVER-REQUEST`: manual review when `requested_amount > amount_paid - prior_refund_total`, except clean duplicate full-refund denials.
- `R-REVIEW-PRIOR-PARTIAL`: manual review when `0 < prior_refund_total < amount_paid`.
- `R-REVIEW-RETURNED`: manual review when `item_status = returned` and the customer asks for a refund or refund-status decision.
- `R-REVIEW-CONFLICT`: manual review when the customer identifies a different order as the subject of this refund, says their refund-order number may be wrong, or makes a delivery claim that conflicts with verified order facts.
- `R-REVIEW-GOODWILL`: manual review for discretionary partial refunds, goodwill credits, or outcomes not explicitly covered above.
## Output Requirements

- Cite every applied rule in `policy_evaluation.matched_policies`.
- Treat a rule as applied only when all of its written conditions are satisfied. Do not count failed candidate rules as applicable or create conflict evidence solely to explain their failure.
- Explain missing facts or policy conflicts in `policy_evaluation.gaps_or_conflicts`.
- Identify applicable and supporting rules, required fact paths, classified evidence, and any policy gaps or conflicts.
- Assign discrete confidence `3` (high), `2` (moderate), `1` (low), or `0` (insufficient) using the definitions supplied in the Policy Agent prompt.
- Apply decision precedence in the Policy Agent prompt: conflicts and review rules require `manual_review`; essential missing facts require `request_info`; no relevant supporting policy or low confidence requires `manual_review`.
- A clear review rule may support a high-confidence `manual_review`; confidence measures confidence in the selected policy decision, not whether the decision is automated.
- Treat a substantive product, delivery, or return condition in `customer_request.sanitized_text` that points to a different rule than the structured `refund_reason` as minor interpretive ambiguity, unless it establishes a missing fact, policy conflict, or review rule.
- Precedents are advisory confidence evidence only. Unavailable precedent memory is neutral. Precedents never replace this knowledge base or create a refund rule.
- OWASP governance and final route escalation are evaluated separately.
- Never claim that a refund was executed.
