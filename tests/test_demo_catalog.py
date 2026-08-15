from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo.catalog import (
    DEFAULT_MANIFEST_PATH,
    DEMO_IDS,
    REFUND_PORTAL_ORIGIN,
    TRUSTED_UI_SELECTION_CASES,
    DemoCatalogError,
    load_demo_catalog,
    resolve_demo_case,
)
from refund_app.fixtures import get_order_fixture, known_order_ids
from refund_app.simulator import simulate, simulate_case


def test_catalog_is_exact_ordered_twenty_case_allowlist() -> None:
    catalog = load_demo_catalog()

    assert catalog.fixture_version == "1.1"
    assert catalog.database == "final"
    assert catalog.evaluation_date == "2026-07-01"
    assert tuple(case.trace_id for case in catalog.cases) == DEMO_IDS
    assert [case.ticket_id for case in catalog.cases] == [f"ticket-{case_id}" for case_id in DEMO_IDS]
    assert [case.customer_id for case in catalog.cases] == [
        f"customer-{case_id}" for case_id in DEMO_IDS
    ]
    assert known_order_ids() == [f"order-{case_id}" for case_id in DEMO_IDS]


@pytest.mark.parametrize("case_id", sorted(TRUSTED_UI_SELECTION_CASES))
def test_graph_input_preserves_message_and_supplies_explicit_selected_order(case_id: str) -> None:
    case = load_demo_catalog().get(case_id)
    state = case.graph_input()

    assert state["message"] == case.ticket["raw_text"]
    assert state["requested_order_id"] == case.order_id
    assert state["request_context"]["selected_order_id"] == case.order_id
    assert state["request_context"]["demo_case_id"] == case_id
    assert state["request_context"]["request_origin"] == REFUND_PORTAL_ORIGIN


def test_graph_input_requires_azure_order_resolution_without_explicit_ui_selection() -> None:
    case = load_demo_catalog().get("demo01")
    state = case.graph_input()

    assert case.selected_order_id is None
    assert "requested_order_id" not in state
    assert "selected_order_id" not in state["request_context"]
    assert state["request_context"]["request_origin"] == REFUND_PORTAL_ORIGIN


def test_case18_keeps_wrong_id_in_message_but_selects_canonical_order() -> None:
    case = load_demo_catalog().get("demo18")

    assert "order-demo99" in case.message
    assert case.selected_order_id == "order-demo18"
    assert resolve_demo_case(
        load_demo_catalog(),
        case_id="demo18",
        order_id="order-demo18",
        message=case.message,
    ) == case


def test_case13_explicit_selection_preserves_the_cross_customer_report() -> None:
    case = load_demo_catalog().get("demo13")

    assert "order-demo99" in case.message
    assert case.selected_order_id == "order-demo13"


def test_triage_governance_cases_do_not_claim_unexecuted_policy_results() -> None:
    catalog = load_demo_catalog()

    for case_id in ("demo12", "demo13"):
        case = catalog.get(case_id)
        assert case.expectations.policy_decision is None
        assert case.expectations.policy_route is None
        assert case.public_summary()["expectations"]["policy_decision"] is None
        assert simulate_case(case)["policy_decision"] is None


def test_only_waiting_user_cases_define_canonical_real_followups() -> None:
    catalog = load_demo_catalog()
    configured = [case.trace_id for case in catalog.cases if case.follow_up is not None]

    assert configured == ["demo10", "demo14"]
    for case_id in configured:
        case = catalog.get(case_id)
        follow_up = case.follow_up
        assert follow_up is not None
        assert follow_up.refund_reason == "damaged"
        assert follow_up.requested_amount == case.order["amount_paid"]
        assert follow_up.expectations.route == "refund_agent"
        state = case.follow_up_graph_input()
        assert state["trace_id"] == case.trace_id
        assert state["ticket_id"] == case.ticket_id
        assert state["user_id"] == case.customer_id
        assert state["requested_order_id"] == case.order_id
        assert state["request_context"]["request_origin"] == REFUND_PORTAL_ORIGIN
        assert state["request_context"]["selected_order_id"] == case.order_id



def test_mixed_or_unknown_selectors_are_rejected() -> None:
    catalog = load_demo_catalog()

    with pytest.raises(DemoCatalogError, match="same single demo case"):
        resolve_demo_case(catalog, case_id="demo01", order_id="order-demo02")
    with pytest.raises(DemoCatalogError, match="not part"):
        resolve_demo_case(catalog, order_id="order-demo99")
    with pytest.raises(DemoCatalogError, match="required"):
        resolve_demo_case(catalog)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data["cases"].append(data["cases"][0]), "exactly 20"),
        (lambda data: data["cases"][0].update(trace_id="demo99"), "trace_id"),
        (lambda data: data.update(database="main_db"), "must be 'final'"),
        (
            lambda data: data["cases"][0].update(selected_order_id="order-demo01"),
            "selected_order_id must be absent",
        ),
    ],
)
def test_catalog_rejects_fixture_scope_drift(tmp_path: Path, mutation, message: str) -> None:
    payload = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    mutation(payload)
    fixture = tmp_path / "demo_cases.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DemoCatalogError, match=message):
        load_demo_catalog(fixture)


def test_offline_fixture_lookup_never_expands_root_corpus() -> None:
    assert get_order_fixture("order-demo01")["order_id"] == "order-demo01"
    assert get_order_fixture("ORDER-DEMO20")["order_id"] == "order-demo20"
    assert get_order_fixture("order-demo21") is None

    clean = get_order_fixture("order-demo01")
    buggy = get_order_fixture("order-demo01", buggy=True)
    assert buggy["order_id"] == clean["order_id"]
    assert buggy["order_customer_id"] == clean["order_customer_id"]
    assert buggy["contact_customer_id"] != buggy["order_customer_id"]


def test_simulator_matches_all_manifest_e2e_expectations_with_stable_ids() -> None:
    catalog = load_demo_catalog()
    results = [simulate_case(case) for case in catalog.cases]

    assert [result["trace_id"] for result in results] == list(DEMO_IDS)
    for case, result in zip(catalog.cases, results):
        assert result["ticket_id"] == case.ticket_id
        assert result["customer_id"] == case.customer_id
        assert result["order_id"] == case.order_id
        assert result["message"] == case.message
        assert result["expected_message"] == case.message
        assert result["order_resolution_source"] == (
            "trusted_ui_selection" if case.selected_order_id else "offline_fixture"
        )
        assert result["route"] == case.expectations.route
        assert result["final_outcome"] == case.expectations.outcome
        assert result["workflow_status"] == case.expectations.terminal_state

    assert simulate(case_id="demo01") == simulate(case_id="demo01")
    with pytest.raises(DemoCatalogError):
        simulate(message="Please create a brand new refund case")
