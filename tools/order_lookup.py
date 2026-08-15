"""Order_Database_Lookup tool for the triage node. The actual read lives in
db.orders (shared final-database read path); this module owns the LLM tool schema and
the thin wrapper the node calls."""
from db.orders import get_order

# Tool definition for the Responses API (flat format — no nested "function" wrapper)
ORDER_LOOKUP_TOOL: dict = {
    "type": "function",
    "name": "Order_Database_Lookup",
    "description": (
        "Look up an order by order_id. Returns order details and the contact "
        "information of the customer associated with that order."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "The order ID to look up (for example ORD-001 or order-demo01)",
            }
        },
        "required": ["order_id"],
    },
}


def order_database_lookup(order_id: str, buggy: bool = False) -> dict | None:
    return get_order(order_id, buggy=buggy)
