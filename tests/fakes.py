"""Fakes + fixtures for offline triage tests (no LLM, no DB)."""
import json

import httpx
from openai import BadRequestError


# ── canned order lookup rows (what db.orders.get_order would return) ──────────

def valid_order() -> dict:
    return {
        "order_id": "ORD-001",
        "order_customer_id": "CUST-001",
        "product_type": "Electronics",
        "purchase_date": "2025-01-15",
        "item_status": "delivered",
        "amount_paid": 299.99,
        "prior_refund_total": 0.0,
        "contact_customer_id": "CUST-001",
        "contact_email": "alice@example.com",
        "contact_name": "Alice Johnson",
    }


def leaked_order() -> dict:
    """A buggy-JOIN row: valid order, but a different customer's contact."""
    row = valid_order()
    row["contact_customer_id"] = "CUST-002"
    row["contact_email"] = "bob@example.com"
    row["contact_name"] = "Bob Smith"
    return row


# ── Azure Responses API fakes ─────────────────────────────────────────────────

class _Part:
    def __init__(self, text: str) -> None:
        self.text = text


class MessageItem:
    type = "message"

    def __init__(self, text: str) -> None:
        self.content = [_Part(text)]


class FunctionCallItem:
    type = "function_call"

    def __init__(self, order_id: str, call_id: str = "call-1") -> None:
        self.arguments = json.dumps({"order_id": order_id})
        self.call_id = call_id


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(self, output: list, usage: tuple[int, int] = (10, 5)) -> None:
        self.output = output
        self.usage = _Usage(*usage)


class _Responses:
    def __init__(self, queue: list) -> None:
        self._queue = list(queue)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAzureClient:
    def __init__(self, queue: list) -> None:
        self.responses = _Responses(queue)


def content_filter_error() -> BadRequestError:
    request = httpx.Request("POST", "https://dummy.local/")
    response = httpx.Response(400, request=request)
    return BadRequestError("content_filter: message blocked", response=response, body=None)
