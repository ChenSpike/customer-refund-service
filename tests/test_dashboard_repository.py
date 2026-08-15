import pytest

from dashboard_app.repository import (
    DashboardRepository,
    DashboardRepositoryError,
    database_config_status,
)


def test_connection_errors_are_propagated_as_typed_repository_errors():
    def fail_connection():
        raise OSError("network unavailable")

    repository = DashboardRepository(fail_connection, "final")

    with pytest.raises(DashboardRepositoryError) as captured:
        repository.ping()

    assert isinstance(captured.value.__cause__, OSError)


class FakeCursor:
    def __init__(self, database_name="final"):
        self.executed = []
        self.closed = False
        self.database_name = database_name

    def execute(self, query, params=()):
        self.executed.append((query, params))

    def fetchall(self):
        return [
            {
                "approval_id": "POL-APP-001",
                "trace_id": "demo01",
                "triggering_event_type": "policy_review",
                "triggering_event_id": "POL-REV-001",
                "status": "pending",
                "policy_review_type": "low_confidence",
                "policy_review_detail": "A reviewer must confirm the exception.",
                "policy_ids_json": '["POL-RET-01"]',
                "ticket_requested_amount": 79.99,
                "ticket_currency": "USD",
                "order_amount_paid": 99.99,
                "order_prior_refund_total": 20.0,
                "order_currency": "USD",
            }
        ]

    def fetchone(self):
        return {"database_name": self.database_name, "ok": 1}

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, database_name="final"):
        self.cursor_value = FakeCursor(database_name)
        self.closed = False

    def cursor(self, *, dictionary=False):
        assert dictionary is True
        return self.cursor_value

    def close(self):
        self.closed = True


def test_pending_approval_query_joins_both_typed_trigger_sources_and_closes_resources():
    connection = FakeConnection()
    repository = DashboardRepository(lambda: connection, "final")

    rows = repository.pending_approvals(limit=20)

    assert rows[0]["approval_id"] == "POL-APP-001"
    assert connection.cursor_value.executed[0] == (
        "SELECT DATABASE() AS database_name",
        (),
    )
    query, params = connection.cursor_value.executed[1]
    assert "triggering_event_type = 'governance'" in query
    assert "triggering_event_type = 'policy_review'" in query
    assert "JOIN workflow_runs" in query
    assert "JOIN tickets" in query
    assert "LEFT JOIN orders" in query
    assert "ticket_requested_amount" in query
    assert "order_prior_refund_total" in query
    assert params == (20,)
    assert connection.cursor_value.closed is True
    assert connection.closed is True


@pytest.mark.parametrize("database_name", ["main_db", "Final", " final ", ""])
def test_repository_rejects_every_database_name_except_exact_final(database_name):
    called = False

    def connect():
        nonlocal called
        called = True
        return FakeConnection()

    with pytest.raises(DashboardRepositoryError, match="restricted to the final database"):
        DashboardRepository(connect, database_name)

    assert called is False


def test_repository_rejects_a_connection_that_selected_another_database():
    connection = FakeConnection("main_db")
    repository = DashboardRepository(lambda: connection, "final")

    with pytest.raises(DashboardRepositoryError, match="restricted to the final database"):
        repository.pending_approvals()

    assert connection.cursor_value.closed is True
    assert connection.closed is True


def test_database_config_status_marks_non_final_database_unsafe(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "example.test")
    monkeypatch.setenv("MYSQL_USER", "demo")
    monkeypatch.setenv("MYSQL_PASSWORD", "secret")
    monkeypatch.setenv("MYSQL_DATABASE", "main_db")

    status = database_config_status()

    assert status["status"] == "unsafe_database"
    assert status["database"] == "main_db"


def test_trace_history_queries_use_stable_primary_key_tiebreaks():
    cursor = FakeCursor()

    DashboardRepository._bulk_by_trace(cursor, "agent_handoffs", ["demo10"])
    bulk_query = cursor.executed[-1][0]
    DashboardRepository._for_trace(
        cursor,
        "governance_events",
        "demo10",
        descending=True,
    )
    trace_query = cursor.executed[-1][0]

    assert "ORDER BY trace_id, created_at, handoff_id" in bulk_query
    assert "ORDER BY created_at DESC, event_id DESC" in trace_query
