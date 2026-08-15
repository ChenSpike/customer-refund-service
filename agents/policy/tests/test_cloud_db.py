from __future__ import annotations

import mysql.connector
import pytest

import db.database as database
from db.database import (
    POLICY_MIGRATION_002_PATH,
    CloudDatabaseError,
    _approval_trigger,
    _migration_statements,
    _require_workflow_row,
)


def test_human_approval_uses_exactly_one_typed_trigger() -> None:
    assert _approval_trigger("TRACE-1", "POL-REV-001", []) == (
        "policy_review",
        "POL-REV-001",
    )
    assert _approval_trigger("TRACE-1", None, ["POL-GOV-001"]) == (
        "governance",
        "POL-GOV-001",
    )


def test_governance_trigger_has_precedence_when_both_events_exist() -> None:
    assert _approval_trigger("TRACE-1", "POL-REV-001", ["POL-GOV-001"]) == (
        "governance",
        "POL-GOV-001",
    )


def test_human_approval_without_an_event_fails_closed() -> None:
    with pytest.raises(CloudDatabaseError, match="requires a governance or policy review event"):
        _approval_trigger("TRACE-1", None, [])


def test_unified_trigger_migration_has_four_ordered_steps() -> None:
    statements = _migration_statements(POLICY_MIGRATION_002_PATH, expected=4)

    assert "ADD COLUMN triggering_event_type" in statements[0]
    assert "DROP FOREIGN KEY fk_human_approvals_event" in statements[1]
    assert "triggering_event_id = COALESCE" in statements[2]
    assert "DROP COLUMN policy_review_event_id" in statements[3]


class _WorkflowCursor:
    def __init__(self, rowcount: int, matching_rows: int = 0) -> None:
        self.rowcount = rowcount
        self.matching_rows = matching_rows
        self.executed: list[tuple[str, tuple[str]]] = []

    def execute(self, statement: str, params: tuple[str]) -> None:
        self.executed.append((statement, params))

    def fetchone(self) -> tuple[int]:
        return (self.matching_rows,)


def test_idempotent_workflow_update_accepts_existing_row() -> None:
    cursor = _WorkflowCursor(rowcount=0, matching_rows=1)

    _require_workflow_row(cursor, "TRACE-1")

    assert cursor.executed == [
        ("SELECT COUNT(*) FROM workflow_runs WHERE trace_id = %s", ("TRACE-1",))
    ]


def test_workflow_update_fails_when_trace_is_missing() -> None:
    cursor = _WorkflowCursor(rowcount=0, matching_rows=0)

    with pytest.raises(CloudDatabaseError, match="row was not updated"):
        _require_workflow_row(cursor, "TRACE-1")


def test_workflow_update_rejects_multiple_affected_rows() -> None:
    cursor = _WorkflowCursor(rowcount=2)

    with pytest.raises(CloudDatabaseError, match="affected 2 rows"):
        _require_workflow_row(cursor, "TRACE-1")


def test_transient_mysql_connection_is_retried(monkeypatch) -> None:
    expected = object()
    attempts = iter(
        [
            mysql.connector.InterfaceError(msg="lost", errno=2013),
            expected,
        ]
    )
    sleeps: list[int] = []

    def connect(**_config):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(database.mysql.connector, "connect", connect)
    monkeypatch.setattr(database.time, "sleep", sleeps.append)

    assert database.GCPRepository({})._connect() is expected
    assert sleeps == [1]


def test_nontransient_mysql_connection_error_fails_immediately(monkeypatch) -> None:
    calls = 0

    def connect(**_config):
        nonlocal calls
        calls += 1
        raise mysql.connector.InterfaceError(msg="denied", errno=1045)

    monkeypatch.setattr(database.mysql.connector, "connect", connect)

    with pytest.raises(CloudDatabaseError, match="after 1 attempt"):
        database.GCPRepository({})._connect()
    assert calls == 1
