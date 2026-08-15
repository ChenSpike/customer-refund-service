"""Read-only adapters for the canonical ``demo01`` through ``demo20`` fixture.

Offline order lookup deliberately exposes only records already declared by
``database/fixtures/demo_cases.json``.  It never invents a customer, order,
ticket, or workflow identifier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from demo.catalog import DEFAULT_MANIFEST_PATH, DemoCase, load_demo_catalog


def get_case_fixture(
    case_id: str,
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
) -> DemoCase:
    """Return one validated canonical case."""

    return load_demo_catalog(manifest_path).get(case_id)


def get_order_fixture(
    order_id: str,
    buggy: bool = False,
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
) -> dict[str, Any] | None:
    """Drop-in offline replacement for ``order_database_lookup``.

    ``buggy=True`` retains a deterministic ASI07 test seam without expanding
    the root corpus: contact details are borrowed from the following canonical
    demo customer while the selected order remains unchanged.
    """

    normalized = str(order_id or "").strip().lower()
    if not normalized:
        return None
    catalog = load_demo_catalog(manifest_path)
    case = next(
        (
            item
            for item in catalog.cases
            if normalized
            in {
                value.lower()
                for value in (item.order_id, item.selected_order_id)
                if value is not None
            }
        ),
        None,
    )
    if case is None:
        return None

    row = case.order_lookup_row()
    if buggy:
        index = catalog.cases.index(case)
        foreign = catalog.cases[(index + 1) % len(catalog.cases)]
        row.update(
            {
                "contact_customer_id": foreign.customer_id,
                "contact_email": foreign.customer["email"],
                "contact_name": foreign.customer["full_name"],
            }
        )
    return row


def known_order_ids(
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
) -> list[str]:
    """Return the exact, ordered 20-order allowlist."""

    return [case.order_id for case in load_demo_catalog(manifest_path).cases]
