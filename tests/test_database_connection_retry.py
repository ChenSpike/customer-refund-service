from __future__ import annotations

from types import SimpleNamespace

import mysql.connector
import pytest

from db import database
from db.database import GCPRepository


class _HandoffCursor:
    def __init__(self, error: mysql.connector.Error | None = None) -> None:
        self.error = error
        self.rowcount = 1
        self.executed: list[str] = []
        self.executed_params: list[tuple[str, tuple]] = []

    def execute(self, query: str, _params=None) -> None:
        self.executed.append(query)
        self.executed_params.append((" ".join(query.split()), tuple(_params or ())))
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        if "UPDATE workflow_runs" in query:
            self.rowcount = 1

    def fetchall(self) -> list[tuple]:
        return []

    def fetchone(self) -> tuple[int]:
        return (1,)


class _HandoffConnection:
    def __init__(self, error: mysql.connector.Error | None = None) -> None:
        self._cursor = _HandoffCursor(error)
        self.started = False
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def start_transaction(self) -> None:
        self.started = True

    def cursor(self, **_kwargs) -> _HandoffCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class _AuditCleanupCursor:
    def __init__(self, rows: list[tuple[int]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple) -> None:
        self.executed.append((" ".join(query.split()), params))

    def fetchall(self) -> list[tuple[int]]:
        return list(self.rows)


def _persist_triage_handoff(repository: GCPRepository) -> str:
    return repository.persist_agent_handoff(
        trace_id="demo14",
        ticket_id="ticket-demo14",
        from_agent="triage_agent",
        to_agent="policy_agent",
        input_payload={"request": "initial"},
        output_payload={"triage_output": {"route": "policy_agent"}},
        audit_event_type="triage_agent_evaluated",
        workflow_status="running",
        current_agent="policy_agent",
    )


def test_connect_retries_cloud_sql_read_timeout(monkeypatch) -> None:
    attempts: list[int] = []
    sleeps: list[int] = []
    connection = object()

    def connect(**_config):
        attempts.append(1)
        if len(attempts) == 1:
            raise mysql.connector.Error(
                msg="The Read Operation timed out",
                errno=3024,
            )
        return connection

    monkeypatch.setattr(database.mysql.connector, "connect", connect)
    monkeypatch.setattr(database.time, "sleep", sleeps.append)

    repository = GCPRepository(
        {
            "host": "example.invalid",
            "user": "demo",
            "password": "not-used",
            "database": "final",
        }
    )

    assert repository._connect() is connection
    assert len(attempts) == 2
    assert sleeps == [1]


@pytest.mark.parametrize("errno", [1213, 1205])
def test_graph_transaction_retries_contention_on_a_fresh_connection(
    monkeypatch,
    errno: int,
) -> None:
    transient = mysql.connector.Error(msg="transaction contention", errno=errno)
    connections = [_HandoffConnection(transient), _HandoffConnection()]
    connect_calls: list[_HandoffConnection] = []
    sleeps: list[float] = []
    repository = GCPRepository(
        {
            "host": "example.invalid",
            "user": "demo",
            "password": "not-used",
            "database": "final",
        }
    )

    def connect() -> _HandoffConnection:
        connection = connections[len(connect_calls)]
        connect_calls.append(connection)
        return connection

    monkeypatch.setattr(repository, "_connect", connect)
    monkeypatch.setattr(database.time, "sleep", sleeps.append)

    handoff_id = _persist_triage_handoff(repository)

    assert handoff_id
    assert connect_calls == connections
    assert connections[0].started and connections[0].rolled_back and connections[0].closed
    assert not connections[0].committed
    assert connections[1].started and connections[1].committed and connections[1].closed
    assert not connections[1].rolled_back
    assert sleeps == [database.MYSQL_TRANSACTION_RETRY_BASE_SECONDS]
    successful_sql = [" ".join(query.split()) for query in connections[1]._cursor.executed]
    assert any(query.startswith("SELECT log_id FROM audit_log") for query in successful_sql)
    assert not any(query.startswith("DELETE FROM audit_log WHERE trace_id") for query in successful_sql)


def test_baseline_handoff_locks_workflow_pk_before_consistent_reads(monkeypatch) -> None:
    connection = _HandoffConnection()
    repository = GCPRepository({"database": "final"})
    monkeypatch.setattr(repository, "_connect", lambda: connection)

    _persist_triage_handoff(repository)

    executed = connection._cursor.executed_params
    lock_sql = "SELECT trace_id FROM workflow_runs WHERE trace_id = %s FOR UPDATE"
    assert executed[0] == (lock_sql, ("demo14",))
    handoff_read = next(
        index
        for index, (sql, _params) in enumerate(executed)
        if sql.startswith("SELECT handoff_id FROM agent_handoffs")
    )
    audit_read = next(
        index
        for index, (sql, _params) in enumerate(executed)
        if sql.startswith("SELECT log_id FROM audit_log")
    )
    assert 0 < handoff_read < audit_read


@pytest.mark.parametrize("method_name", ["policy", "handoff", "refund"])
def test_each_baseline_persistence_path_locks_before_its_first_sql_read(
    monkeypatch,
    method_name: str,
) -> None:
    class LockProbe(Exception):
        pass

    connection = _HandoffConnection()
    repository = GCPRepository({"database": "final"})
    monkeypatch.setattr(repository, "_connect", lambda: connection)
    lock_calls: list[tuple[object, str]] = []

    def stop_at_lock(cursor, trace_id: str) -> None:
        lock_calls.append((cursor, trace_id))
        raise LockProbe

    monkeypatch.setattr(database, "_lock_workflow_row", stop_at_lock)

    with pytest.raises(LockProbe):
        if method_name == "policy":
            repository.persist_result(
                SimpleNamespace(case=SimpleNamespace(trace_id="demo14")),
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(),
                [],
                SimpleNamespace(),
            )
        elif method_name == "handoff":
            _persist_triage_handoff(repository)
        else:
            repository.persist_refund_result(
                trace_id="demo14",
                ticket_id="ticket-demo14",
                policy_decision={"decision": "approve"},
                order_lookup_result={"order_id": "order-demo14"},
                refund_result={
                    "status": "success",
                    "amount": 1,
                    "currency": "USD",
                    "refund_id": "RF-demo14",
                },
            )

    assert lock_calls == [(connection._cursor, "demo14")]
    assert connection._cursor.executed == []
    assert connection.rolled_back and connection.closed and not connection.committed


def test_workflow_row_lock_targets_each_trace_independently() -> None:
    cursor = _HandoffCursor()

    database._lock_workflow_row(cursor, "demo13")
    database._lock_workflow_row(cursor, "demo14")

    lock_sql = "SELECT trace_id FROM workflow_runs WHERE trace_id = %s FOR UPDATE"
    assert cursor.executed_params == [
        (lock_sql, ("demo13",)),
        (lock_sql, ("demo14",)),
    ]


def test_graph_transaction_does_not_retry_non_contention_mysql_error(monkeypatch) -> None:
    non_retryable = mysql.connector.Error(msg="duplicate key", errno=1062)
    connection = _HandoffConnection(non_retryable)
    connect_calls: list[_HandoffConnection] = []
    sleeps: list[float] = []
    repository = GCPRepository(
        {
            "host": "example.invalid",
            "user": "demo",
            "password": "not-used",
            "database": "final",
        }
    )

    def connect() -> _HandoffConnection:
        connect_calls.append(connection)
        return connection

    monkeypatch.setattr(repository, "_connect", connect)
    monkeypatch.setattr(database.time, "sleep", sleeps.append)

    with pytest.raises(mysql.connector.Error) as raised:
        _persist_triage_handoff(repository)

    assert raised.value is non_retryable
    assert connect_calls == [connection]
    assert connection.started and connection.rolled_back and connection.closed
    assert not connection.committed
    assert sleeps == []


def test_graph_transaction_contention_retry_is_bounded(monkeypatch) -> None:
    errors = [
        mysql.connector.Error(msg=f"deadlock attempt {attempt}", errno=1213)
        for attempt in range(1, database.MYSQL_TRANSACTION_ATTEMPTS + 1)
    ]
    connections = [_HandoffConnection(error) for error in errors]
    connect_calls: list[_HandoffConnection] = []
    sleeps: list[float] = []
    repository = GCPRepository(
        {
            "host": "example.invalid",
            "user": "demo",
            "password": "not-used",
            "database": "final",
        }
    )

    def connect() -> _HandoffConnection:
        connection = connections[len(connect_calls)]
        connect_calls.append(connection)
        return connection

    monkeypatch.setattr(repository, "_connect", connect)
    monkeypatch.setattr(database.time, "sleep", sleeps.append)

    with pytest.raises(mysql.connector.Error) as raised:
        _persist_triage_handoff(repository)

    assert raised.value is errors[-1]
    assert connect_calls == connections
    assert all(connection.rolled_back and connection.closed for connection in connections)
    assert sleeps == [
        database.MYSQL_TRANSACTION_RETRY_BASE_SECONDS,
        database.MYSQL_TRANSACTION_RETRY_BASE_SECONDS * 2,
    ]


def test_empty_audit_replacement_uses_a_nonlocking_read_and_no_delete() -> None:
    cursor = _AuditCleanupCursor([])

    deleted = database._delete_existing_audit_rows(
        cursor,
        trace_id="demo14",
        event_type="triage_agent_evaluated",
        agent="triage_agent",
    )

    assert deleted == 0
    assert cursor.executed == [
        (
            "SELECT log_id FROM audit_log WHERE trace_id = %s AND agent = %s "
            "AND event_type = %s ORDER BY log_id",
            ("demo14", "triage_agent", "triage_agent_evaluated"),
        )
    ]


def test_existing_audit_replacement_deletes_only_exact_primary_keys() -> None:
    cursor = _AuditCleanupCursor([(17,), (29,)])

    deleted = database._delete_existing_audit_rows(
        cursor,
        trace_id="demo14",
        agent="refund_agent",
    )

    assert deleted == 2
    assert cursor.executed == [
        (
            "SELECT log_id FROM audit_log WHERE trace_id = %s AND agent = %s ORDER BY log_id",
            ("demo14", "refund_agent"),
        ),
        ("DELETE FROM audit_log WHERE log_id = %s", (17,)),
        ("DELETE FROM audit_log WHERE log_id = %s", (29,)),
    ]


@pytest.mark.parametrize(
    "method_name",
    [
        "save_governance_event_record",
        "persist_result",
        "persist_agent_handoff",
        "persist_refund_result",
        "ensure_human_approval",
        "resolve_human_approval",
        "mark_human_approval_continuation",
        "record_human_approval_continuation_failure",
        "heartbeat_human_approval_continuation",
        "record_workflow_failure",
        "record_failure",
    ],
)
def test_graph_and_approval_transactions_use_contention_retry(method_name: str) -> None:
    assert hasattr(getattr(GCPRepository, method_name), "__wrapped__")
