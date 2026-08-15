from __future__ import annotations

import re
from collections import Counter

import pytest

from db import admin


class FakeConnection:
    def __init__(self) -> None:
        self.selected_database = "final"
        self.schema_exists = False
        self.tables = set(admin.APPLICATION_TABLES)
        self.counts = {table: 0 for table in admin.APPLICATION_TABLES}
        self.ids = {
            "customers": {f"customer-demo{index:02d}" for index in range(1, 21)},
            "orders": {f"order-demo{index:02d}" for index in range(1, 21)},
            "tickets": {f"ticket-demo{index:02d}" for index in range(1, 21)},
            "workflow_runs": set(admin.DEMO_TRACE_IDS),
        }
        self.output_traces = {table: [] for table in admin.OUTPUT_TABLES}
        self.foreign_keys = set(admin.EXPECTED_FOREIGN_KEYS)
        self.workflow_status_type = (
            "enum('running','waiting_user','paused_governance','pending_human','completed','failed')"
        )
        self.root_rows = admin.expected_canonical_root_rows(
            admin.load_fixture(),
            phase="baseline",
        )
        self.calls: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.transactions = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def start_transaction(self, *args, **kwargs) -> None:
        self.transactions += 1

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rows: list[tuple] = []
        self.rowcount = 0

    def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        self.connection.calls.append((normalized, params))
        self.rowcount = 0

        if normalized == "SELECT DATABASE()":
            self.rows = [(self.connection.selected_database,)]
        elif "FROM INFORMATION_SCHEMA.SCHEMATA" in normalized:
            self.rows = [(int(self.connection.schema_exists),)]
        elif normalized.startswith("CREATE DATABASE "):
            self.rows = []
            self.connection.schema_exists = True
        elif "FROM INFORMATION_SCHEMA.TABLES" in normalized:
            self.rows = [(table,) for table in sorted(self.connection.tables)]
        elif "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE" in normalized:
            self.rows = [tuple(item) for item in sorted(self.connection.foreign_keys)]
        elif "FROM INFORMATION_SCHEMA.COLUMNS" in normalized:
            self.rows = [(self.connection.workflow_status_type,)]
        elif "LEFT JOIN" in normalized and normalized.startswith("SELECT COUNT(*)"):
            self.rows = [(0,)]
        elif normalized.startswith("SELECT customer_id, email, full_name FROM customers"):
            self.rows = list(self.connection.root_rows["customers"])
        elif normalized.startswith(
            "SELECT order_id, customer_id, product_type, purchase_date, item_status,"
        ):
            self.rows = list(self.connection.root_rows["orders"])
        elif normalized.startswith(
            "SELECT ticket_id, customer_id, raw_text, sanitized_text, refund_reason,"
        ):
            self.rows = list(self.connection.root_rows["tickets"])
        elif normalized.startswith(
            "SELECT trace_id, ticket_id, policy_version, status, current_agent, completed_at"
        ):
            self.rows = list(self.connection.root_rows["workflow_runs"])
        elif normalized.startswith("SELECT trace_id, ticket_id, policy_version FROM workflow_runs"):
            self.rows = [row[:3] for row in self.connection.root_rows["workflow_runs"]]
        elif match := re.match(r"SELECT COUNT\(\*\) FROM `([^`]+)`", normalized):
            self.rows = [(self.connection.counts[match.group(1)],)]
        elif match := re.match(r"SELECT `([^`]+)` FROM `([^`]+)`", normalized):
            table = match.group(2)
            self.rows = [(value,) for value in sorted(self.connection.ids[table])]
        elif match := re.match(r"SELECT trace_id FROM `([^`]+)`", normalized):
            table = match.group(1)
            values = (
                sorted(self.connection.ids["workflow_runs"])
                if table == "workflow_runs"
                else list(self.connection.output_traces[table])
            )
            self.rows = [(value,) for value in values]
        elif normalized.startswith("SELECT VERSION()"):
            self.rows = [("8.4.10-google", "root@%", "STRICT_TRANS_TABLES")]
        elif normalized.startswith(("CREATE TABLE ", "INSERT INTO ", "DELETE FROM ", "UPDATE ")):
            self.rows = []
            self.rowcount = 1
        else:
            raise AssertionError(f"Unhandled fake SQL: {normalized}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self) -> None:
        pass


def _baseline_connection() -> FakeConnection:
    connection = FakeConnection()
    for table in admin.CORE_TABLES:
        connection.counts[table] = 20
    return connection


def test_schema_is_complete_dependency_ordered_and_uses_pending_human() -> None:
    statements = admin.schema_statements()
    table_names = [re.match(r"CREATE TABLE ([a-z_]+)", statement).group(1) for statement in statements]

    assert table_names == [
        "customers",
        "orders",
        "tickets",
        "workflow_runs",
        "agent_handoffs",
        "audit_log",
        "governance_events",
        "policy_review_events",
        "human_approvals",
        "refund_transactions",
    ]
    schema = admin.SCHEMA_PATH.read_text(encoding="utf-8")
    assert "'pending_human'" in schema
    assert "'waiting_human'" not in schema
    assert "CREATE DATABASE" not in "\n".join(statements)
    assert "IF NOT EXISTS" not in "\n".join(statements)
    assert "VARCHAR(36)" in schema


def test_fixture_has_exact_demo_allowlist_and_original_policy_distribution() -> None:
    fixture = admin.load_fixture()
    cases = fixture["cases"]

    assert fixture["evaluation_date"] == "2026-07-01"
    assert [case["trace_id"] for case in cases] == list(admin.DEMO_TRACE_IDS)
    assert Counter(case["expectations"]["legacy_policy_decision"] for case in cases) == {
        "approve": 5,
        "deny": 5,
        "manual_review": 8,
        "request_info": 2,
    }
    assert Counter(case["expectations"]["legacy_policy_route"] for case in cases) == {
        "refund_agent": 4,
        "response_agent": 6,
        "human_approval": 10,
    }
    by_trace = {case["trace_id"]: case for case in cases}
    for index in (4, 10, 13, 14, 18):
        trace_id = f"demo{index:02d}"
        assert by_trace[trace_id]["selected_order_id"] == f"order-{trace_id}"
    for index in set(range(1, 21)) - {4, 10, 13, 14, 18}:
        assert by_trace[f"demo{index:02d}"]["selected_order_id"] is None
    assert "order-demo99" in by_trace["demo13"]["ticket"]["raw_text"]
    assert "order-demo99" in by_trace["demo18"]["ticket"]["raw_text"]


@pytest.mark.parametrize(
    ("database", "confirmation"),
    [("main_db", "final"), ("final", "main_db"), ("final", None)],
)
def test_write_guard_rejects_every_target_except_confirmed_final(database, confirmation) -> None:
    with pytest.raises(admin.AdminError, match="--database final --confirm final"):
        admin.require_write_target(database, confirmation)


def test_write_cli_requires_confirmation_before_any_connection() -> None:
    with pytest.raises(SystemExit):
        admin.build_parser().parse_args(["seed", "--database", "final"])


def test_create_fails_on_collision_without_executing_ddl() -> None:
    connection = FakeConnection()
    connection.schema_exists = True

    with pytest.raises(admin.AdminError, match="already exists"):
        admin.create_database(connection)

    assert not any(sql.startswith("CREATE DATABASE") for sql, _ in connection.calls)


def test_create_uses_collision_sensitive_statement() -> None:
    connection = FakeConnection()

    admin.create_database(connection)

    create_sql = next(sql for sql, _ in connection.calls if sql.startswith("CREATE DATABASE"))
    assert create_sql == "CREATE DATABASE `final` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
    assert "IF NOT EXISTS" not in create_sql
    assert connection.commits == 1


def test_apply_schema_executes_exactly_ten_create_table_statements() -> None:
    connection = FakeConnection()

    admin.apply_schema(connection)

    creates = [sql for sql, _ in connection.calls if sql.startswith("CREATE TABLE")]
    assert len(creates) == 10
    assert connection.commits == 1


def test_seed_is_transactional_and_inserts_only_four_roots_per_case() -> None:
    connection = FakeConnection()
    fixture = admin.load_fixture()

    admin.seed_database(connection, fixture)

    inserts = [(sql, params) for sql, params in connection.calls if sql.startswith("INSERT INTO")]
    assert connection.transactions == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert len(inserts) == 80
    assert Counter(sql.split()[2] for sql, _ in inserts) == {
        "customers": 20,
        "orders": 20,
        "tickets": 20,
        "workflow_runs": 20,
    }
    inserted_workflows = [params[0] for sql, params in inserts if sql.startswith("INSERT INTO workflow_runs")]
    assert inserted_workflows == list(admin.DEMO_TRACE_IDS)


def test_seed_refuses_any_preexisting_application_row() -> None:
    connection = FakeConnection()
    connection.counts["customers"] = 1

    with pytest.raises(admin.AdminError, match="every application table to be empty"):
        admin.seed_database(connection, admin.load_fixture())

    assert not any(sql.startswith("INSERT INTO") for sql, _ in connection.calls)
    assert connection.rollbacks == 1


def test_reset_aborts_before_delete_when_an_unexpected_trace_exists() -> None:
    connection = FakeConnection()
    connection.ids["workflow_runs"].add("rogue-case")

    with pytest.raises(admin.AdminError, match="non-demo traces"):
        admin.reset_demo(connection)

    assert not any(sql.startswith("DELETE FROM") for sql, _ in connection.calls)
    assert connection.rollbacks == 1


def test_reset_deletes_only_allowlisted_output_rows_in_fk_order() -> None:
    connection = FakeConnection()
    connection.output_traces["refund_transactions"] = ["demo01"]
    connection.output_traces["human_approvals"] = ["demo01"]

    admin.reset_demo(connection)

    deletes = [(sql, params) for sql, params in connection.calls if sql.startswith("DELETE FROM")]
    assert [re.match(r"DELETE FROM `([^`]+)`", sql).group(1) for sql, _ in deletes] == [
        "refund_transactions",
        "human_approvals",
        "policy_review_events",
        "governance_events",
        "audit_log",
        "agent_handoffs",
    ]
    assert all(tuple(params) == admin.DEMO_TRACE_IDS for _, params in deletes)
    assert connection.commits == 1


def test_verify_baseline_checks_counts_ids_foreign_keys_and_enum() -> None:
    connection = _baseline_connection()

    report = admin.verify_database(connection, admin.load_fixture(), phase="baseline")

    assert report["counts"] == {
        "customers": 20,
        "orders": 20,
        "tickets": 20,
        "workflow_runs": 20,
        "agent_handoffs": 0,
        "audit_log": 0,
        "governance_events": 0,
        "policy_review_events": 0,
        "human_approvals": 0,
        "refund_transactions": 0,
    }
    assert all(count == 0 for count in report["orphans"].values())
    assert report["canonical_root_fingerprint"] == admin.canonical_root_fingerprint(
        admin.load_fixture()
    )


def test_verify_baseline_rejects_a_mutated_seeded_order_fact() -> None:
    connection = _baseline_connection()
    first = list(connection.root_rows["orders"][0])
    first[5] = "999.99"
    connection.root_rows["orders"][0] = tuple(first)

    with pytest.raises(admin.AdminError, match=r"orders canonical root content differs.*demo01"):
        admin.verify_database(connection, admin.load_fixture(), phase="baseline")


def test_verify_rejects_wrong_workflow_status_enum() -> None:
    connection = _baseline_connection()
    connection.workflow_status_type = "enum('running','waiting_human','completed')"

    with pytest.raises(admin.AdminError, match="wrong enum"):
        admin.verify_database(connection, admin.load_fixture())
