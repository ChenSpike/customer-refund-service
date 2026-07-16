from db import backend, pipeline_store
from governance.audit_logger import log_event, log_governance_event
from governance.pii_detector import scan_dict_for_pii
from state import TriageState

# Every field Order_Database_Lookup must return, with expected Python types
_REQUIRED_FIELDS: dict[str, type | tuple] = {
    "order_id":           str,
    "order_customer_id":  str,
    "product_type":       str,
    "purchase_date":      str,
    "item_status":        str,
    "amount_paid":        (int, float),
    "prior_refund_total": (int, float),
    "contact_customer_id": str,
    "contact_email":      str,
    "contact_name":       str,
}

_VALID_ITEM_STATUSES = {"delivered", "damaged", "returned", "unknown"}


# ── DB helpers for PII cross-referencing ──────────────────────────────────────

# NOTE: this must query the SAME backend as Order_Database_Lookup, otherwise
# lookups served by GCP would be cross-referenced against the local DB and
# Check C would silently miss foreign PII. Email is the only contact PII in the
# canonical customers schema (no phone column).

def _owner_of_email(email: str) -> str | None:
    row = backend.query_one(
        "SELECT customer_id FROM customers WHERE email = ?", (email,)
    )
    return row["customer_id"] if row else None


# ── Main interceptor ──────────────────────────────────────────────────────────

def intercept_triage_output(state: TriageState) -> dict:
    """
    ASI07 — Data Leakage check.
    Inspects the raw Order_Database_Lookup result before the triage output
    is passed to the Policy Agent.

    Single exit point: runs the checks, persists the verdict to the local
    audit store (masked), then returns a state patch with governance_result
    and next_agent.
    """
    patch = _run_checks(state)

    trace_id = state.get("trace_id") or "unknown"
    ticket_id = state.get("ticket_id")
    user_id = state.get("user_id", "unknown")
    result = patch["governance_result"]
    next_agent = patch["next_agent"]

    log_governance_event(trace_id, ticket_id, user_id, result, next_agent)
    log_event(
        trace_id,
        f"interceptor_{result['status']}",  # interceptor_allow | interceptor_block
        agent="governance_interceptor",
        ticket_id=ticket_id,
        user_id=user_id,
        payload={"failed_check": result.get("failed_check"),
                 "checks_passed": result.get("checks_passed")},
    )
    log_event(
        trace_id,
        "handoff_ready",
        agent="governance_interceptor",
        ticket_id=ticket_id,
        user_id=user_id,
        payload={"next_agent": next_agent},
    )

    # Shared main_db: snapshot the control transfer + advance the workflow run.
    output_json = pipeline_store.build_handoff_output_json(state)
    if result["status"] == "block":
        output_json["governance_result"] = result
    pipeline_store.record_handoff(
        trace_id, ticket_id or "unknown",
        from_agent="triage_agent", to_agent=next_agent,
        input_json=pipeline_store.build_handoff_input_json(state),
        output_json=output_json,
        input_tokens=state.get("llm_input_tokens"),
        output_tokens=state.get("llm_output_tokens"),
    )
    # Status vocabulary per Derrick's policy tests: a GOVERNANCE block parks the
    # run as 'paused_governance'; 'pending_human' is reserved for the policy
    # decision manual_review queue.
    status_map = {"policy_agent": "running", "human_approval": "paused_governance"}
    pipeline_store.update_workflow_run(
        trace_id, status=status_map.get(next_agent, "running"),
        current_agent=next_agent)

    return patch


def _run_checks(state: TriageState) -> dict:
    """Run checks A/B/C and build the state patch. No side effects."""
    user_id: str = state["user_id"]
    raw: dict = state.get("order_lookup_result", {})

    # ── Check A: Schema validation ────────────────────────────────────────────
    for field, expected_type in _REQUIRED_FIELDS.items():
        if field not in raw:
            return _block(
                check="schema_validation",
                detail=f"Missing required field: '{field}'",
            )
        if not isinstance(raw[field], expected_type):
            return _block(
                check="schema_validation",
                detail=(
                    f"Field '{field}' has wrong type "
                    f"(expected {expected_type}, got {type(raw[field]).__name__})"
                ),
            )

    if raw["item_status"] not in _VALID_ITEM_STATUSES:
        return _block(
            check="schema_validation",
            detail=f"Invalid item_status value: '{raw['item_status']}'",
        )

    # ── Check B: Ownership ────────────────────────────────────────────────────
    # The customer_id returned by the JOIN must match the requesting user.
    # A buggy JOIN (e.g. ON !=) causes this to mismatch even though the
    # order itself belongs to the correct user.
    if raw["contact_customer_id"] != user_id:
        return _block(
            check="ownership",
            detail=(
                f"contact_customer_id '{raw['contact_customer_id']}' "
                f"does not match requesting user '{user_id}'"
            ),
            offending_field="contact_customer_id",
            offending_value=raw["contact_customer_id"],
            pii_type="customer_id",
        )

    # ── Check C: PII scan ─────────────────────────────────────────────────────
    # Secondary defense: even if ownership passes, cross-reference any detected
    # email against the DB to catch subtler contamination. Email is the only
    # contact PII in the canonical customers schema.
    for hit in scan_dict_for_pii(raw):
        owner_id = None
        if hit.pii_type == "email":
            owner_id = _owner_of_email(hit.value)

        if owner_id and owner_id != user_id:
            return _block(
                check="pii_scan",
                detail=(
                    f"PII belonging to customer '{owner_id}' "
                    f"found in field '{hit.field}'"
                ),
                offending_field=hit.field,
                offending_value=hit.value,
                pii_type=hit.pii_type,
            )

    # ── All checks passed ─────────────────────────────────────────────────────
    return {
        "governance_result": {
            "status": "allow",
            "rule": "ASI07",
            "checks_passed": ["schema_validation", "ownership", "pii_scan"],
        },
        "next_agent": "policy_agent",
    }


def _block(check: str, detail: str, **extra) -> dict:
    return {
        "governance_result": {
            "status": "block",
            "rule": "ASI07",
            "failed_check": check,
            "detail": detail,
            "interceptor_action": "block",
            **extra,
        },
        "next_agent": "human_approval",
    }
