"""db.orders.get_order — SQL selection (normal vs buggy JOIN) and MySQL
type normalization, with the DB connection mocked (offline)."""
import datetime
import decimal

import pytest

from db import orders


class _FakeCursor:
    def __init__(self, row, calls):
        self._row, self._calls = row, calls

    def execute(self, sql, params):
        self._calls.append((sql, params))

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _FakeConn:
    def __init__(self, row, calls):
        self._row, self._calls = row, calls

    def cursor(self, dictionary=False):
        return _FakeCursor(self._row, self._calls)

    def close(self):
        pass


@pytest.fixture()
def mock_db(monkeypatch):
    calls = []
    row = {
        "order_id": "ORD-001", "order_customer_id": "CUST-001",
        "product_type": "Electronics", "purchase_date": datetime.date(2025, 1, 15),
        "item_status": "delivered", "amount_paid": decimal.Decimal("299.99"),
        "prior_refund_total": decimal.Decimal("0.0"),
        "contact_customer_id": "CUST-001", "contact_email": "alice@example.com",
        "contact_name": "Alice Johnson",
    }
    monkeypatch.setattr(orders, "mysql", type("m", (), {
        "connector": type("c", (), {"connect": staticmethod(lambda **_: _FakeConn(row, calls))})}))
    monkeypatch.setattr(orders, "_config", lambda: {})
    return calls


def test_normalizes_decimal_and_date(mock_db):
    row = orders.get_order("ORD-001")
    assert row["amount_paid"] == 299.99 and isinstance(row["amount_paid"], float)
    assert row["purchase_date"] == "2025-01-15"          # date → isoformat str


def test_normal_join_uses_equality(mock_db):
    orders.get_order("ORD-001")
    sql = mock_db[0][0]
    assert "o.customer_id = c.customer_id" in sql


def test_buggy_join_uses_inequality(mock_db):
    orders.get_order("ORD-001", buggy=True)
    sql = mock_db[0][0]
    assert "!=" in sql
