"""Canned order rows for offline (REFUND_DB=fake) mode.

Shape matches what db.orders.get_order returns, so the same triage node code
runs unchanged whether the row comes from here or from the live orders table.
"""
from __future__ import annotations

# Keyed by order_id (upper-cased on lookup).
_ORDERS: dict[str, dict] = {
    # Clean order — owner's contact matches. Flows through to policy/refund.
    "ORD-001": {
        "order_id": "ORD-001",
        "order_customer_id": "CUST-001",
        "product_type": "Electronics",
        "purchase_date": "2025-01-15",
        "item_status": "damaged",
        "amount_paid": 299.99,
        "prior_refund_total": 0.0,
        "contact_customer_id": "CUST-001",
        "contact_email": "alice@example.com",
        "contact_name": "Alice Johnson",
    },
    # ASI07 data-leakage row — a foreign customer's contact leaked in (the buggy
    # JOIN scenario). Triage governance must block this and route to human review.
    "ORD-LEAK": {
        "order_id": "ORD-LEAK",
        "order_customer_id": "CUST-001",
        "product_type": "Electronics",
        "purchase_date": "2025-01-15",
        "item_status": "damaged",
        "amount_paid": 299.99,
        "prior_refund_total": 0.0,
        "contact_customer_id": "CUST-002",       # ← different owner: ownership breach
        "contact_email": "bob@example.com",
        "contact_name": "Bob Smith",
    },
    # Delivered-and-fine order — policy should deny (no valid refund reason).
    "ORD-777": {
        "order_id": "ORD-777",
        "order_customer_id": "CUST-001",
        "product_type": "Apparel",
        "purchase_date": "2025-03-02",
        "item_status": "delivered",
        "amount_paid": 59.00,
        "prior_refund_total": 0.0,
        "contact_customer_id": "CUST-001",
        "contact_email": "alice@example.com",
        "contact_name": "Alice Johnson",
    },
}


def get_order_fixture(order_id: str, buggy: bool = False) -> dict | None:
    """Drop-in replacement for tools.order_lookup.order_database_lookup."""
    if not order_id:
        return None
    return _ORDERS.get(order_id.strip().upper())


def known_order_ids() -> list[str]:
    return list(_ORDERS.keys())
