from datetime import date


REQUIRED_POLICY_FIELDS = {
    "case": ("trace_id", "ticket_id", "policy_version"),
    "customer_request": ("sanitized_text", "currency"),
    "order_facts": (
        "order_id",
        "product_type",
        "purchase_date",
        "item_status",
        "amount_paid",
        "prior_refund_total",
    ),
}

ALLOWED_POLICY_VERSIONS = {"v1.0"}


def _is_blank(value) -> bool:
    return isinstance(value, str) and not value.strip()


def _normalize_non_empty_string(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return str(value).strip() or None


def _normalize_optional_string(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return str(value).strip() or None


def _normalize_non_negative_float(value):
    if value is None or _is_blank(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _normalize_optional_non_negative_float(value):
    if value is None or _is_blank(value):
        return None
    return _normalize_non_negative_float(value)


def _normalize_date_string(value):
    text = _normalize_non_empty_string(value)
    if text is None:
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def build_policy_input(state: dict) -> dict:
    triage_output = state["triage_output"]
    case = dict(triage_output.get("case") or {})
    customer_request = dict(triage_output.get("customer_request") or {})
    order_facts = dict(triage_output.get("order_facts") or {})

    policy_version = _normalize_non_empty_string(case.get("policy_version"))
    if policy_version not in ALLOWED_POLICY_VERSIONS:
        policy_version = None

    return {
        "case": {
            "trace_id": _normalize_non_empty_string(case.get("trace_id")),
            "ticket_id": _normalize_non_empty_string(case.get("ticket_id")),
            "policy_version": policy_version,
        },
        "customer_request": {
            "sanitized_text": _normalize_non_empty_string(customer_request.get("sanitized_text")),
            "refund_reason": _normalize_optional_string(customer_request.get("refund_reason")),
            "requested_amount": _normalize_optional_non_negative_float(
                customer_request.get("requested_amount")
            ),
            "currency": _normalize_non_empty_string(customer_request.get("currency")),
        },
        "order_facts": {
            "order_id": _normalize_non_empty_string(order_facts.get("order_id")),
            "product_type": _normalize_non_empty_string(order_facts.get("product_type")),
            "purchase_date": _normalize_date_string(order_facts.get("purchase_date")),
            "item_status": _normalize_non_empty_string(order_facts.get("item_status")),
            "amount_paid": _normalize_non_negative_float(order_facts.get("amount_paid")),
            "prior_refund_total": _normalize_non_negative_float(
                order_facts.get("prior_refund_total")
            ),
        },
    }


def missing_required_fields(policy_input: dict) -> list[str]:
    missing: list[str] = []
    for section, field_names in REQUIRED_POLICY_FIELDS.items():
        values = policy_input.get(section) or {}
        for field_name in field_names:
            value = values.get(field_name)
            if value is None:
                missing.append(f"{section}.{field_name}")
            elif isinstance(value, str) and not value.strip():
                missing.append(f"{section}.{field_name}")
    return missing


def request_info_result(state: dict, missing_fields: list[str]) -> dict:
    missing_list = ", ".join(missing_fields)
    return {
        "trace_id": state.get("trace_id"),
        "ticket_id": state.get("ticket_id"),
        "policy_decision": {
            "decision": "request_info",
            "refund_amount": 0.0,
            "reason": f"Missing required policy fields: {missing_list}.",
            "confidence": "low",
        },
        "policy_context": {
            "missing_required_fields": missing_fields,
        },
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
    }
