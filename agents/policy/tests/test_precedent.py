from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from agents.policy.policy_node import load_policy_context, load_precedent_context


def _record(index: int = 1) -> dict:
    return {
        "precedent_id": f"PREC-{index:03d}",
        "policy_version": "v1.0",
        "normalized_case": "Damaged delivered item in the low amount band.",
        "relevant_attributes": {
            "refund_reason": "damaged",
            "item_status": "delivered",
            "product_type": "electronics",
            "amount_band": "low",
            "purchase_window": "within_30_days",
            "prior_refund_state": "none",
        },
        "matched_rule_ids": ["R-APPROVE-DAMAGED-30D"],
        "final_decision": "approve",
        "human_outcome": "approved",
        "finalized_at": "2026-07-01T00:00:00Z",
    }


def _memory(records: list[dict] | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "policy_version": "v1.0",
        "generated_at": "2026-07-18T00:00:00Z",
        "derived_guidance": [],
        "precedents": records if records is not None else [_record()],
    }


def _write(path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _load(path):
    return load_precedent_context(
        "v1.0",
        policy_context=load_policy_context("v1.0"),
        path=path,
    )


def test_valid_yaml_memory_loads_finalized_precedents(tmp_path) -> None:
    path = tmp_path / "precedents.yaml"
    _write(path, _memory())

    context = _load(path)

    assert context.available is True
    assert context.status == "loaded"
    assert [record.precedent_id for record in context.records] == ["PREC-001"]


def test_missing_empty_and_malformed_memory_are_nonfatal(tmp_path) -> None:
    missing = _load(tmp_path / "missing.yaml")
    assert (missing.available, missing.status) == (False, "missing")

    empty_path = tmp_path / "empty.yaml"
    empty_path.write_text("", encoding="utf-8")
    empty = _load(empty_path)
    assert (empty.available, empty.status) == (False, "empty")

    malformed_path = tmp_path / "malformed.yaml"
    malformed_path.write_text("precedents: [", encoding="utf-8")
    malformed = _load(malformed_path)
    assert (malformed.available, malformed.status) == (False, "malformed")


def test_valid_empty_memory_is_reported_as_unavailable(tmp_path) -> None:
    path = tmp_path / "precedents.yaml"
    _write(path, _memory([]))

    context = _load(path)

    assert context.available is False
    assert context.status == "empty"
    assert "no finalized" in context.reason


def test_policy_version_mismatch_is_not_loaded(tmp_path) -> None:
    path = tmp_path / "precedents.yaml"
    payload = _memory()
    payload["policy_version"] = "v2.0"
    payload["precedents"][0]["policy_version"] = "v2.0"
    _write(path, payload)

    context = _load(path)

    assert context.available is False
    assert context.status == "version_mismatch"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["precedents"][0].update(
            {"normalized_case": "Customer email jane@example.com requested a refund."}
        ),
        lambda payload: payload["precedents"][0].update(
            {"normalized_case": "The source used order ID ORD-12345."}
        ),
        lambda payload: payload["precedents"][0].update(
            {"raw_customer_text": "I demand a refund."}
        ),
        lambda payload: payload["precedents"][0].update(
            {"final_decision": "manual_review"}
        ),
        lambda payload: payload["precedents"].append(deepcopy(payload["precedents"][0])),
    ],
)
def test_sensitive_extra_invalid_and_duplicate_records_are_rejected(tmp_path, mutate) -> None:
    path = tmp_path / "precedents.yaml"
    payload = _memory()
    mutate(payload)
    _write(path, payload)

    context = _load(path)

    assert context.available is False
    assert context.status == "malformed"


def test_record_policy_version_mismatch_is_rejected_as_malformed(tmp_path) -> None:
    path = tmp_path / "precedents.yaml"
    payload = _memory()
    payload["precedents"][0]["policy_version"] = "v2.0"
    _write(path, payload)

    context = _load(path)

    assert context.available is False
    assert context.status == "malformed"
