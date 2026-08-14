"""Validated access to the canonical ``demo01`` through ``demo20`` fixture."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST_PATH = REPO_ROOT / "database" / "fixtures" / "demo_cases.json"
DEMO_IDS = tuple(f"demo{index:02d}" for index in range(1, 21))
FINAL_DATABASE = "final"


class DemoCatalogError(ValueError):
    """Raised when fixture data or a requested demo selector is invalid."""


@dataclass(frozen=True)
class DemoExpectations:
    policy_decision: str
    policy_route: str
    route: str
    outcome: str
    terminal_state: str

    def as_dict(self) -> dict[str, str]:
        return {
            "policy_decision": self.policy_decision,
            "policy_route": self.policy_route,
            "route": self.route,
            "outcome": self.outcome,
            "terminal_state": self.terminal_state,
        }


@dataclass(frozen=True)
class DemoCase:
    trace_id: str
    ticket_id: str
    customer_id: str
    order_id: str
    selected_order_override: str | None
    customer: dict[str, Any]
    order: dict[str, Any]
    ticket: dict[str, Any]
    expectations: DemoExpectations
    evaluation_date: str
    policy_version: str

    @property
    def message(self) -> str:
        return str(self.ticket["raw_text"])

    @property
    def selected_order_id(self) -> str:
        """The canonical UI selection, including explicit override cases."""

        return self.selected_order_override or self.order_id

    def graph_input(self) -> dict[str, Any]:
        """Return the fixed root state for a seeded, idempotent graph invocation."""

        return {
            "user_id": self.customer_id,
            "message": self.message,
            "conversation_history": [],
            "request_context": {
                "trace_id": self.trace_id,
                "ticket_id": self.ticket_id,
                "demo_case_id": self.trace_id,
                "selected_order_id": self.selected_order_id,
                "evaluation_date": self.evaluation_date,
                "buggy_db": False,
            },
            "requested_order_id": self.selected_order_id,
            "trace_id": self.trace_id,
            "ticket_id": self.ticket_id,
        }

    def public_summary(self) -> dict[str, Any]:
        return {
            "case_id": self.trace_id,
            "ticket_id": self.ticket_id,
            "customer_id": self.customer_id,
            "order_id": self.order_id,
            "selected_order_id": self.selected_order_id,
            "message": self.message,
            "expectations": self.expectations.as_dict(),
        }

    def order_lookup_row(self) -> dict[str, Any]:
        """Return the same normalized shape as ``db.orders.get_order``."""

        return {
            "order_id": self.order_id,
            "order_customer_id": self.customer_id,
            "product_type": self.order["product_type"],
            "purchase_date": self.order["purchase_date"],
            "item_status": self.order["item_status"],
            "amount_paid": float(self.order["amount_paid"]),
            "prior_refund_total": float(self.order["prior_refund_total"]),
            "currency": self.order.get("currency", "USD"),
            "contact_customer_id": self.customer_id,
            "contact_email": self.customer["email"],
            "contact_name": self.customer["full_name"],
        }


@dataclass(frozen=True)
class DemoCatalog:
    fixture_version: str
    database: str
    evaluation_date: str
    policy_version: str
    description: str
    cases: tuple[DemoCase, ...]

    def get(self, case_id: str) -> DemoCase:
        normalized = str(case_id or "").strip().lower()
        if normalized not in DEMO_IDS:
            raise DemoCatalogError(
                f"Unknown demo case {case_id!r}; expected demo01 through demo20"
            )
        return self.cases[DEMO_IDS.index(normalized)]


def load_demo_catalog(path: str | Path = DEFAULT_MANIFEST_PATH) -> DemoCatalog:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DemoCatalogError(f"Could not load demo fixture {manifest_path}: {error}") from error
    return _catalog_from_payload(payload)


def resolve_demo_case(
    catalog: DemoCatalog,
    *,
    case_id: str | None = None,
    order_id: str | None = None,
    customer_id: str | None = None,
    message: str | None = None,
) -> DemoCase:
    """Resolve selectors to one case and reject ambiguous or mixed identities."""

    selectors: list[set[str]] = []
    if case_id and case_id.strip():
        selectors.append({catalog.get(case_id).trace_id})
    if order_id and order_id.strip():
        normalized_order = order_id.strip().lower()
        selectors.append(
            {
                case.trace_id
                for case in catalog.cases
                if normalized_order in {case.order_id.lower(), case.selected_order_id.lower()}
            }
        )
    if customer_id and customer_id.strip():
        normalized_customer = customer_id.strip().lower()
        selectors.append(
            {case.trace_id for case in catalog.cases if case.customer_id.lower() == normalized_customer}
        )
    if message is not None and message.strip():
        selectors.append({case.trace_id for case in catalog.cases if case.message == message})

    if not selectors:
        raise DemoCatalogError("A case_id, canonical order_id, customer_id, or exact fixture message is required")
    if any(not matches for matches in selectors):
        raise DemoCatalogError("One or more request selectors are not part of demo01 through demo20")
    matches = set.intersection(*selectors)
    if len(matches) != 1:
        raise DemoCatalogError("Request selectors do not identify the same single demo case")
    return catalog.get(next(iter(matches)))


def _catalog_from_payload(payload: Any) -> DemoCatalog:
    if not isinstance(payload, dict):
        raise DemoCatalogError("Demo fixture root must be a JSON object")
    if payload.get("database") != FINAL_DATABASE:
        raise DemoCatalogError("Demo fixture database must be 'final'")
    evaluation_date = _required_text(payload, "evaluation_date", "fixture")
    policy_version = _required_text(payload, "policy_version", "fixture")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) != len(DEMO_IDS):
        raise DemoCatalogError("Demo fixture must contain exactly 20 cases")

    cases = tuple(
        _parse_case(raw_case, index, evaluation_date, policy_version)
        for index, raw_case in enumerate(raw_cases, start=1)
    )
    if tuple(case.trace_id for case in cases) != DEMO_IDS:
        raise DemoCatalogError("Demo fixture cases must be ordered demo01 through demo20")
    for field in ("ticket_id", "customer_id", "order_id"):
        values = [getattr(case, field) for case in cases]
        if len(set(values)) != len(values):
            raise DemoCatalogError(f"Demo fixture {field} values must be unique")

    return DemoCatalog(
        fixture_version=str(payload.get("fixture_version") or ""),
        database=FINAL_DATABASE,
        evaluation_date=evaluation_date,
        policy_version=policy_version,
        description=str(payload.get("description") or ""),
        cases=cases,
    )


def _parse_case(
    value: Any,
    index: int,
    evaluation_date: str,
    policy_version: str,
) -> DemoCase:
    if not isinstance(value, dict):
        raise DemoCatalogError(f"demo{index:02d}: case must be a JSON object")
    trace_id = f"demo{index:02d}"
    expected_ids = {
        "trace_id": trace_id,
        "ticket_id": f"ticket-{trace_id}",
        "customer_id": f"customer-{trace_id}",
        "order_id": f"order-{trace_id}",
    }
    for field, expected in expected_ids.items():
        if value.get(field) != expected:
            raise DemoCatalogError(f"{trace_id}: {field} must be {expected!r}")

    selected = value.get("selected_order_id")
    if selected is not None and selected != expected_ids["order_id"]:
        raise DemoCatalogError(f"{trace_id}: selected_order_id must select its canonical order")
    customer = _required_object(value, "customer", trace_id)
    order = _required_object(value, "order", trace_id)
    ticket = _required_object(value, "ticket", trace_id)
    expectations = _required_object(value, "expectations", trace_id)
    for field in ("email", "full_name"):
        _required_text(customer, field, trace_id)
    for field in ("product_type", "purchase_date", "item_status", "currency"):
        _required_text(order, field, trace_id)
    for field in ("amount_paid", "prior_refund_total"):
        if not isinstance(order.get(field), (int, float)) or isinstance(order.get(field), bool):
            raise DemoCatalogError(f"{trace_id}: order.{field} must be numeric")
    _required_text(ticket, "raw_text", trace_id)

    parsed_expectations = DemoExpectations(
        policy_decision=_required_text(expectations, "legacy_policy_decision", trace_id),
        policy_route=_required_text(expectations, "legacy_policy_route", trace_id),
        route=_required_text(expectations, "e2e_route", trace_id),
        outcome=_required_text(expectations, "e2e_outcome", trace_id),
        terminal_state=_required_text(expectations, "e2e_terminal_state", trace_id),
    )
    return DemoCase(
        trace_id=trace_id,
        ticket_id=expected_ids["ticket_id"],
        customer_id=expected_ids["customer_id"],
        order_id=expected_ids["order_id"],
        selected_order_override=selected,
        customer=deepcopy(customer),
        order=deepcopy(order),
        ticket=deepcopy(ticket),
        expectations=parsed_expectations,
        evaluation_date=evaluation_date,
        policy_version=policy_version,
    )


def _required_object(value: dict[str, Any], field: str, label: str) -> dict[str, Any]:
    result = value.get(field)
    if not isinstance(result, dict):
        raise DemoCatalogError(f"{label}: {field} must be an object")
    return result


def _required_text(value: dict[str, Any], field: str, label: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise DemoCatalogError(f"{label}: {field} must be non-empty text")
    return result
