"""
Integration tests: Order_Database_Lookup tool + ASI07 Governance Interceptor.
Requires a seeded SQLite DB (no OpenAI calls needed here).
"""
import pytest
from db import pipeline_store
from db.seed import seed
from tools.order_lookup import order_database_lookup
from governance.interceptor import intercept_triage_output


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    seed()


# ── Order_Database_Lookup ─────────────────────────────────────────────────────

class TestOrderDatabaseLookup:
    def test_normal_returns_correct_customer_contact(self):
        result = order_database_lookup("ORD-001", buggy=False)
        assert result is not None
        assert result["order_customer_id"] == "CUST-001"
        assert result["contact_customer_id"] == "CUST-001"
        assert result["contact_email"] == "alice@example.com"

    def test_buggy_returns_wrong_customer_contact(self):
        result = order_database_lookup("ORD-001", buggy=True)
        assert result is not None
        assert result["order_customer_id"] == "CUST-001"
        # Bug: contact data belongs to a different customer
        assert result["contact_customer_id"] != "CUST-001"
        assert result["contact_email"] != "alice@example.com"

    def test_unknown_order_returns_none(self):
        assert order_database_lookup("ORD-999") is None

    def test_returns_all_required_fields(self):
        result = order_database_lookup("ORD-001")
        required = {
            "order_id", "order_customer_id", "product_type", "purchase_date",
            "item_status", "amount_paid", "prior_refund_total",
            "contact_customer_id", "contact_email", "contact_name",
        }
        assert required.issubset(result.keys())


# ── Governance Interceptor ────────────────────────────────────────────────────

def _state(order_lookup_result: dict, user_id: str = "CUST-001") -> dict:
    return {"user_id": user_id, "order_lookup_result": order_lookup_result}


class TestInterceptorHappyPath:
    def test_clean_result_is_allowed(self):
        raw = order_database_lookup("ORD-001", buggy=False)
        result = intercept_triage_output(_state(raw))
        assert result["governance_result"]["status"] == "allow"
        assert result["next_agent"] == "policy_agent"

    def test_allow_result_lists_all_passed_checks(self):
        raw = order_database_lookup("ORD-001", buggy=False)
        result = intercept_triage_output(_state(raw))
        checks = result["governance_result"]["checks_passed"]
        assert "schema_validation" in checks
        assert "ownership" in checks
        assert "pii_scan" in checks


class TestInterceptorDemoScenario:
    """Scenario 2 from the proposal: buggy JOIN leaks another customer's PII."""

    def test_buggy_db_is_blocked(self):
        raw = order_database_lookup("ORD-001", buggy=True)
        result = intercept_triage_output(_state(raw))
        assert result["governance_result"]["status"] == "block"
        assert result["governance_result"]["rule"] == "ASI07"
        assert result["next_agent"] == "human_approval"

    def test_buggy_db_fails_ownership_check(self):
        raw = order_database_lookup("ORD-001", buggy=True)
        result = intercept_triage_output(_state(raw))
        assert result["governance_result"]["failed_check"] == "ownership"


class TestInterceptorOwnership:
    def test_mismatched_contact_customer_id_is_blocked(self):
        raw = order_database_lookup("ORD-001", buggy=False)
        raw["contact_customer_id"] = "CUST-002"  # simulate cross-customer contamination
        result = intercept_triage_output(_state(raw))
        assert result["governance_result"]["status"] == "block"
        assert result["governance_result"]["failed_check"] == "ownership"

    def test_block_includes_offending_value(self):
        raw = order_database_lookup("ORD-001", buggy=False)
        raw["contact_customer_id"] = "CUST-002"
        result = intercept_triage_output(_state(raw))
        assert result["governance_result"]["offending_value"] == "CUST-002"


class TestInterceptorPIIScan:
    def test_foreign_email_in_field_is_blocked(self):
        # Ownership passes (contact_customer_id is correct) but email belongs to
        # another customer — Check C catches it as a secondary defense layer.
        raw = order_database_lookup("ORD-001", buggy=False)
        raw["contact_email"] = "bob@example.com"  # bob belongs to CUST-002
        result = intercept_triage_output(_state(raw))
        assert result["governance_result"]["status"] == "block"
        assert result["governance_result"]["failed_check"] == "pii_scan"
        assert result["governance_result"]["pii_type"] == "email"

    def test_own_email_does_not_trigger_pii_block(self):
        raw = order_database_lookup("ORD-001", buggy=False)
        # alice@example.com belongs to CUST-001, same as the requesting user
        assert raw["contact_email"] == "alice@example.com"
        result = intercept_triage_output(_state(raw, user_id="CUST-001"))
        assert result["governance_result"]["status"] == "allow"


class TestInterceptorPipelineWrites:
    """The verdict side must snapshot the handoff and advance the workflow run."""

    def _capture(self, monkeypatch):
        handoffs, run_updates = [], []
        monkeypatch.setattr(pipeline_store, "record_handoff",
                            lambda *a, **k: handoffs.append((a, k)))
        monkeypatch.setattr(pipeline_store, "update_workflow_run",
                            lambda *a, **k: run_updates.append((a, k)))
        return handoffs, run_updates

    def test_handoff_recorded_on_allow(self, monkeypatch):
        handoffs, run_updates = self._capture(monkeypatch)
        raw = order_database_lookup("ORD-001", buggy=False)
        state = {"user_id": "CUST-001", "trace_id": "TR-X", "ticket_id": "TK-X",
                 "message": "ORD-001 broke", "order_lookup_result": raw,
                 "triage_output": {"case": {}, "customer_request": {}, "order_facts": {}},
                 "llm_input_tokens": 150, "llm_output_tokens": 30}
        intercept_triage_output(state)

        assert len(handoffs) == 1
        _, kw = handoffs[0]
        assert kw["from_agent"] == "triage_agent"
        assert kw["to_agent"] == "policy_agent"
        assert kw["input_tokens"] == 150 and kw["output_tokens"] == 30
        assert "governance_result" not in kw["output_json"]  # allow path
        assert run_updates[0][1] == {"status": "running",
                                     "current_agent": "policy_agent"}

    def test_handoff_recorded_on_block_with_governance_result(self, monkeypatch):
        handoffs, run_updates = self._capture(monkeypatch)
        raw = order_database_lookup("ORD-001", buggy=True)  # leaks another customer
        state = {"user_id": "CUST-001", "trace_id": "TR-Y", "ticket_id": "TK-Y",
                 "message": "ORD-001 broke", "order_lookup_result": raw}
        intercept_triage_output(state)

        _, kw = handoffs[0]
        assert kw["to_agent"] == "human_approval"
        assert kw["output_json"]["governance_result"]["failed_check"] == "ownership"
        # governance block → paused_governance (Derrick's status vocabulary)
        assert run_updates[0][1] == {"status": "paused_governance",
                                     "current_agent": "human_approval"}


class TestInterceptorSchemaValidation:
    def test_missing_field_is_blocked(self):
        raw = order_database_lookup("ORD-001", buggy=False)
        del raw["contact_email"]
        result = intercept_triage_output(_state(raw))
        assert result["governance_result"]["status"] == "block"
        assert result["governance_result"]["failed_check"] == "schema_validation"

    def test_wrong_field_type_is_blocked(self):
        raw = order_database_lookup("ORD-001", buggy=False)
        raw["amount_paid"] = "free"  # should be numeric
        result = intercept_triage_output(_state(raw))
        assert result["governance_result"]["status"] == "block"
        assert result["governance_result"]["failed_check"] == "schema_validation"

    def test_invalid_item_status_is_blocked(self):
        raw = order_database_lookup("ORD-001", buggy=False)
        raw["item_status"] = "flying"
        result = intercept_triage_output(_state(raw))
        assert result["governance_result"]["status"] == "block"
        assert result["governance_result"]["failed_check"] == "schema_validation"
