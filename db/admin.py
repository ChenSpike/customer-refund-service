"""Fail-closed bootstrap and fixture administration for the `final` database.

All mutating CLI commands require both ``--database final`` and
``--confirm final``.  The module has no import-time connections and is designed
so its core functions can be exercised with mocked MySQL connections.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import mysql.connector


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "migrations" / "001_initial_schema.sql"
FIXTURE_PATH = REPO_ROOT / "database" / "fixtures" / "demo_cases.json"
FINAL_DATABASE = "final"
DATABASE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

CORE_TABLES = ("customers", "orders", "tickets", "workflow_runs")
OUTPUT_TABLES = (
    "agent_handoffs",
    "audit_log",
    "governance_events",
    "policy_review_events",
    "human_approvals",
    "refund_transactions",
)
APPLICATION_TABLES = CORE_TABLES + OUTPUT_TABLES
DEMO_TRACE_IDS = tuple(f"demo{index:02d}" for index in range(1, 21))
TRUSTED_UI_SELECTION_CASES = frozenset({4, 10, 13, 14, 18})

EXPECTED_FOREIGN_KEYS = frozenset(
    {
        ("orders", "customer_id", "customers", "customer_id"),
        ("tickets", "customer_id", "customers", "customer_id"),
        ("workflow_runs", "ticket_id", "tickets", "ticket_id"),
        ("agent_handoffs", "trace_id", "workflow_runs", "trace_id"),
        ("agent_handoffs", "ticket_id", "tickets", "ticket_id"),
        ("audit_log", "trace_id", "workflow_runs", "trace_id"),
        ("governance_events", "trace_id", "workflow_runs", "trace_id"),
        ("policy_review_events", "trace_id", "workflow_runs", "trace_id"),
        ("human_approvals", "trace_id", "workflow_runs", "trace_id"),
        ("refund_transactions", "trace_id", "workflow_runs", "trace_id"),
        ("refund_transactions", "approval_id", "human_approvals", "approval_id"),
    }
)

ID_COLUMNS = {
    "customers": "customer_id",
    "orders": "order_id",
    "tickets": "ticket_id",
    "workflow_runs": "trace_id",
}

ORPHAN_QUERIES = {
    "orders_customer": """
        SELECT COUNT(*) FROM orders child
        LEFT JOIN customers parent ON parent.customer_id = child.customer_id
        WHERE parent.customer_id IS NULL
    """,
    "tickets_customer": """
        SELECT COUNT(*) FROM tickets child
        LEFT JOIN customers parent ON parent.customer_id = child.customer_id
        WHERE parent.customer_id IS NULL
    """,
    "workflow_ticket": """
        SELECT COUNT(*) FROM workflow_runs child
        LEFT JOIN tickets parent ON parent.ticket_id = child.ticket_id
        WHERE parent.ticket_id IS NULL
    """,
    "handoff_workflow": """
        SELECT COUNT(*) FROM agent_handoffs child
        LEFT JOIN workflow_runs parent ON parent.trace_id = child.trace_id
        WHERE parent.trace_id IS NULL
    """,
    "handoff_ticket": """
        SELECT COUNT(*) FROM agent_handoffs child
        LEFT JOIN tickets parent ON parent.ticket_id = child.ticket_id
        WHERE parent.ticket_id IS NULL
    """,
    "audit_workflow": """
        SELECT COUNT(*) FROM audit_log child
        LEFT JOIN workflow_runs parent ON parent.trace_id = child.trace_id
        WHERE child.trace_id IS NOT NULL AND parent.trace_id IS NULL
    """,
    "governance_workflow": """
        SELECT COUNT(*) FROM governance_events child
        LEFT JOIN workflow_runs parent ON parent.trace_id = child.trace_id
        WHERE parent.trace_id IS NULL
    """,
    "policy_review_workflow": """
        SELECT COUNT(*) FROM policy_review_events child
        LEFT JOIN workflow_runs parent ON parent.trace_id = child.trace_id
        WHERE parent.trace_id IS NULL
    """,
    "approval_workflow": """
        SELECT COUNT(*) FROM human_approvals child
        LEFT JOIN workflow_runs parent ON parent.trace_id = child.trace_id
        WHERE parent.trace_id IS NULL
    """,
    "refund_workflow": """
        SELECT COUNT(*) FROM refund_transactions child
        LEFT JOIN workflow_runs parent ON parent.trace_id = child.trace_id
        WHERE parent.trace_id IS NULL
    """,
    "refund_approval": """
        SELECT COUNT(*) FROM refund_transactions child
        LEFT JOIN human_approvals parent ON parent.approval_id = child.approval_id
        WHERE child.approval_id IS NOT NULL AND parent.approval_id IS NULL
    """,
}


class AdminError(RuntimeError):
    """Raised when a bootstrap safety or integrity condition fails."""


def load_environment(explicit_path: str | Path | None = None) -> None:
    """Load ignored env files without overriding an existing process setting."""

    candidates: list[Path] = []
    if explicit_path:
        explicit = Path(explicit_path).expanduser().resolve()
        if not explicit.is_file():
            raise AdminError(f"Explicit env file does not exist: {explicit}")
        candidates.append(explicit)
    elif os.getenv("IDOX_DB_ENV"):
        configured = Path(os.environ["IDOX_DB_ENV"]).expanduser().resolve()
        if not configured.is_file():
            raise AdminError(f"IDOX_DB_ENV file does not exist: {configured}")
        candidates.append(configured)
    else:
        candidates.extend(
            [
                REPO_ROOT / ".env",
                REPO_ROOT / "agents" / "policy" / ".env",
                REPO_ROOT / "database" / ".env",
            ]
        )

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        for raw_line in resolved.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def connection_config(database: str | None) -> dict[str, Any]:
    required = ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise AdminError("Missing MySQL settings: " + ", ".join(missing))
    config: dict[str, Any] = {
        "host": os.environ["MYSQL_HOST"],
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.environ["MYSQL_USER"],
        "password": os.environ["MYSQL_PASSWORD"],
        "connection_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "10")),
    }
    if database is not None:
        config["database"] = database
    return config


def connect(database: str | None):
    if database is not None:
        _validate_database_name(database)
    return mysql.connector.connect(**connection_config(database))


def require_write_target(database: str, confirmation: str | None) -> None:
    if database != FINAL_DATABASE or confirmation != FINAL_DATABASE:
        raise AdminError("Writes require --database final --confirm final")
    _validate_database_name(database)


def _validate_database_name(database: str) -> None:
    if not DATABASE_NAME_RE.fullmatch(database):
        raise AdminError(f"Unsafe database identifier: {database!r}")
    if database != FINAL_DATABASE:
        raise AdminError("This administrator is restricted to database 'final'")


def schema_statements(path: Path = SCHEMA_PATH) -> list[str]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("--")]
    statements = [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]
    if len(statements) != len(APPLICATION_TABLES):
        raise AdminError(
            f"Initial schema must contain exactly {len(APPLICATION_TABLES)} statements; found {len(statements)}"
        )
    if any(not statement.upper().startswith("CREATE TABLE ") for statement in statements):
        raise AdminError("Initial schema may contain only CREATE TABLE statements")
    return statements


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_fixture(payload)
    return payload


def validate_fixture(payload: dict[str, Any]) -> None:
    if payload.get("database") != FINAL_DATABASE:
        raise AdminError("Fixture database must be 'final'")
    if payload.get("evaluation_date") != "2026-07-01":
        raise AdminError("Fixture evaluation_date must be the fixed benchmark date 2026-07-01")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != len(DEMO_TRACE_IDS):
        raise AdminError("Fixture must contain exactly 20 cases")

    seen_emails: set[str] = set()
    for index, case in enumerate(cases, start=1):
        trace_id = f"demo{index:02d}"
        expected_ids = {
            "trace_id": trace_id,
            "ticket_id": f"ticket-{trace_id}",
            "customer_id": f"customer-{trace_id}",
            "order_id": f"order-{trace_id}",
        }
        for field, expected in expected_ids.items():
            if case.get(field) != expected:
                raise AdminError(f"{trace_id}: {field} must be {expected!r}")
        email = case.get("customer", {}).get("email")
        if not email or email in seen_emails:
            raise AdminError(f"{trace_id}: customer email must be present and unique")
        seen_emails.add(email)
        if case.get("order", {}).get("purchase_date") is None:
            raise AdminError(f"{trace_id}: purchase_date is required")
        expected_selection = (
            expected_ids["order_id"] if index in TRUSTED_UI_SELECTION_CASES else None
        )
        if case.get("selected_order_id") != expected_selection:
            raise AdminError(
                f"{trace_id}: selected_order_id must be {expected_selection!r}"
            )
        expectations = case.get("expectations", {})
        for field in ("legacy_policy_decision", "legacy_policy_route", "e2e_route", "e2e_terminal_state"):
            if not expectations.get(field):
                raise AdminError(f"{trace_id}: missing expectation {field}")

    actual_ids = tuple(case["trace_id"] for case in cases)
    if actual_ids != DEMO_TRACE_IDS:
        raise AdminError("Fixture cases must be ordered demo01 through demo20")
    by_trace = {case["trace_id"]: case for case in cases}
    if "order-demo99" not in by_trace["demo13"]["ticket"]["raw_text"]:
        raise AdminError("demo13 must preserve its intentionally invalid order reference")
    if "order-demo99" not in by_trace["demo18"]["ticket"]["raw_text"]:
        raise AdminError("demo18 must preserve its intentionally invalid order reference")


def _assert_selected_database(connection: Any, database: str) -> None:
    _validate_database_name(database)
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT DATABASE()")
        row = cursor.fetchone()
    finally:
        cursor.close()
    selected = row[0] if row else None
    if selected != database:
        raise AdminError(f"Connected database is {selected!r}; expected {database!r}")


def _list_tables(cursor: Any, database: str) -> tuple[str, ...]:
    cursor.execute(
        """
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
        """,
        (database,),
    )
    return tuple(row[0] for row in cursor.fetchall())


def _require_canonical_tables(cursor: Any, database: str) -> None:
    actual = set(_list_tables(cursor, database))
    expected = set(APPLICATION_TABLES)
    if actual != expected:
        raise AdminError(
            f"Database tables differ from the canonical schema; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def create_database(server_connection: Any, database: str = FINAL_DATABASE) -> None:
    _validate_database_name(database)
    cursor = server_connection.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
            (database,),
        )
        if int(cursor.fetchone()[0]):
            raise AdminError(f"Database {database!r} already exists; refusing to reuse or overwrite it")
        cursor.execute(
            f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
        )
        server_connection.commit()
    finally:
        cursor.close()


def apply_schema(connection: Any, database: str = FINAL_DATABASE, path: Path = SCHEMA_PATH) -> None:
    _assert_selected_database(connection, database)
    cursor = connection.cursor()
    try:
        for statement in schema_statements(path):
            cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def _table_counts(cursor: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in APPLICATION_TABLES:
        cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
        counts[table] = int(cursor.fetchone()[0])
    return counts


def seed_database(
    connection: Any,
    fixture: dict[str, Any],
    database: str = FINAL_DATABASE,
) -> None:
    validate_fixture(fixture)
    _assert_selected_database(connection, database)
    cursor = connection.cursor()
    try:
        connection.start_transaction()
        _require_canonical_tables(cursor, database)
        occupied = {table: count for table, count in _table_counts(cursor).items() if count}
        if occupied:
            raise AdminError(f"Seed requires every application table to be empty; found {occupied}")

        for case in fixture["cases"]:
            customer = case["customer"]
            order = case["order"]
            ticket = case["ticket"]
            cursor.execute(
                "INSERT INTO customers (customer_id, email, full_name) VALUES (%s, %s, %s)",
                (case["customer_id"], customer["email"], customer["full_name"]),
            )
            cursor.execute(
                """
                INSERT INTO orders (
                  order_id, customer_id, product_type, purchase_date, item_status,
                  amount_paid, prior_refund_total, currency
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    case["order_id"],
                    case["customer_id"],
                    order["product_type"],
                    order["purchase_date"],
                    order["item_status"],
                    order["amount_paid"],
                    order["prior_refund_total"],
                    order["currency"],
                ),
            )
            cursor.execute(
                """
                INSERT INTO tickets (
                  ticket_id, customer_id, raw_text, sanitized_text, refund_reason,
                  requested_amount, currency, status, injection_flag
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'new', %s)
                """,
                (
                    case["ticket_id"],
                    case["customer_id"],
                    ticket["raw_text"],
                    ticket["sanitized_text"],
                    ticket["refund_reason"],
                    ticket["requested_amount"],
                    ticket["currency"],
                    int(ticket["injection_flag"]),
                ),
            )
            cursor.execute(
                """
                INSERT INTO workflow_runs (trace_id, ticket_id, status, current_agent, policy_version)
                VALUES (%s, %s, 'running', 'triage_agent', %s)
                """,
                (case["trace_id"], case["ticket_id"], fixture["policy_version"]),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def _trace_rows(cursor: Any, table: str, *, for_update: bool = False) -> list[str | None]:
    suffix = " FOR UPDATE" if for_update else ""
    cursor.execute(f"SELECT trace_id FROM `{table}` ORDER BY trace_id{suffix}")
    return [row[0] for row in cursor.fetchall()]


def _assert_allowed_traces(rows: Iterable[str | None], *, table: str, require_all: bool = False) -> None:
    values = list(rows)
    actual = {value for value in values if value is not None}
    unexpected = actual - set(DEMO_TRACE_IDS)
    null_count = sum(value is None for value in values)
    if unexpected or null_count:
        raise AdminError(
            f"{table} contains non-demo traces; unexpected={sorted(unexpected)}, null_trace_rows={null_count}"
        )
    if require_all and actual != set(DEMO_TRACE_IDS):
        raise AdminError(
            f"{table} does not contain the exact demo allowlist; missing={sorted(set(DEMO_TRACE_IDS) - actual)}"
        )


def reset_demo(connection: Any, database: str = FINAL_DATABASE) -> None:
    _assert_selected_database(connection, database)
    cursor = connection.cursor()
    placeholders = ", ".join("%s" for _ in DEMO_TRACE_IDS)
    try:
        connection.start_transaction()
        workflow_rows = _trace_rows(cursor, "workflow_runs", for_update=True)
        _assert_allowed_traces(workflow_rows, table="workflow_runs", require_all=True)
        for table in OUTPUT_TABLES:
            _assert_allowed_traces(
                _trace_rows(cursor, table, for_update=True),
                table=table,
            )

        for table in (
            "refund_transactions",
            "human_approvals",
            "policy_review_events",
            "governance_events",
            "audit_log",
            "agent_handoffs",
        ):
            cursor.execute(
                f"DELETE FROM `{table}` WHERE trace_id IN ({placeholders})",
                DEMO_TRACE_IDS,
            )
        cursor.execute(
            f"""
            UPDATE workflow_runs
            SET status = 'running', current_agent = 'triage_agent', policy_version = 'v1.0',
                completed_at = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE trace_id IN ({placeholders})
            """,
            DEMO_TRACE_IDS,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def _expected_core_ids(fixture: dict[str, Any]) -> dict[str, set[str]]:
    cases = fixture["cases"]
    return {
        "customers": {case["customer_id"] for case in cases},
        "orders": {case["order_id"] for case in cases},
        "tickets": {case["ticket_id"] for case in cases},
        "workflow_runs": {case["trace_id"] for case in cases},
    }


def expected_canonical_root_rows(
    fixture: dict[str, Any],
    *,
    phase: str = "baseline",
) -> dict[str, list[tuple[Any, ...]]]:
    """Return normalized fixture rows for every persistent workflow root.

    Runtime verification deliberately excludes mutable workflow status fields,
    while baseline verification also requires the exact clean starting state.
    """

    if phase not in {"baseline", "runtime"}:
        raise AdminError("Verification phase must be baseline or runtime")
    rows: dict[str, list[tuple[Any, ...]]] = {
        "customers": [],
        "orders": [],
        "tickets": [],
        "workflow_runs": [],
    }
    for case in fixture["cases"]:
        customer = case["customer"]
        order = case["order"]
        ticket = case["ticket"]
        rows["customers"].append(
            (case["customer_id"], customer["email"], customer["full_name"])
        )
        rows["orders"].append(
            (
                case["order_id"],
                case["customer_id"],
                order["product_type"],
                _canonical_date(order["purchase_date"]),
                order["item_status"],
                _canonical_money(order["amount_paid"]),
                _canonical_money(order["prior_refund_total"]),
                order["currency"],
            )
        )
        rows["tickets"].append(
            (
                case["ticket_id"],
                case["customer_id"],
                ticket["raw_text"],
                ticket["sanitized_text"],
                ticket["refund_reason"],
                _canonical_money(ticket["requested_amount"]),
                ticket["currency"],
                "new",
                int(bool(ticket["injection_flag"])),
            )
        )
        workflow = (case["trace_id"], case["ticket_id"], fixture["policy_version"])
        if phase == "baseline":
            workflow += ("running", "triage_agent", None)
        rows["workflow_runs"].append(workflow)
    return {table: sorted(values, key=lambda row: row[0]) for table, values in rows.items()}


def canonical_root_fingerprint(fixture: dict[str, Any], *, phase: str = "baseline") -> str:
    payload = expected_canonical_root_rows(fixture, phase=phase)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def verify_canonical_root_content(
    cursor: Any,
    fixture: dict[str, Any],
    *,
    phase: str = "baseline",
) -> str:
    """Fail closed if any seeded customer/order/ticket/workflow fact drifted."""

    expected = expected_canonical_root_rows(fixture, phase=phase)
    queries = {
        "customers": """
            SELECT customer_id, email, full_name
            FROM customers ORDER BY customer_id
        """,
        "orders": """
            SELECT order_id, customer_id, product_type, purchase_date, item_status,
                   amount_paid, prior_refund_total, currency
            FROM orders ORDER BY order_id
        """,
        "tickets": """
            SELECT ticket_id, customer_id, raw_text, sanitized_text, refund_reason,
                   requested_amount, currency, status, injection_flag
            FROM tickets ORDER BY ticket_id
        """,
        "workflow_runs": (
            """
            SELECT trace_id, ticket_id, policy_version, status, current_agent, completed_at
            FROM workflow_runs ORDER BY trace_id
            """
            if phase == "baseline"
            else """
            SELECT trace_id, ticket_id, policy_version
            FROM workflow_runs ORDER BY trace_id
            """
        ),
    }
    for table, query in queries.items():
        cursor.execute(query)
        actual = [
            _normalize_canonical_root_row(table, tuple(row), phase=phase)
            for row in cursor.fetchall()
        ]
        if actual != expected[table]:
            expected_by_id = {str(row[0]): row for row in expected[table]}
            actual_by_id = {str(row[0]): row for row in actual}
            changed = sorted(
                identity
                for identity in expected_by_id.keys() & actual_by_id.keys()
                if expected_by_id[identity] != actual_by_id[identity]
            )
            raise AdminError(
                f"{table} canonical root content differs; "
                f"missing={sorted(expected_by_id.keys() - actual_by_id.keys())}, "
                f"unexpected={sorted(actual_by_id.keys() - expected_by_id.keys())}, "
                f"changed={changed}"
            )
    return canonical_root_fingerprint(fixture, phase=phase)


def _normalize_canonical_root_row(
    table: str,
    row: tuple[Any, ...],
    *,
    phase: str,
) -> tuple[Any, ...]:
    if table == "orders":
        return (
            str(row[0]),
            str(row[1]),
            row[2],
            _canonical_date(row[3]),
            row[4],
            _canonical_money(row[5]),
            _canonical_money(row[6]),
            row[7],
        )
    if table == "tickets":
        return (
            str(row[0]),
            str(row[1]),
            row[2],
            row[3],
            row[4],
            _canonical_money(row[5]),
            row[6],
            row[7],
            int(bool(row[8])),
        )
    if table == "workflow_runs":
        normalized: tuple[Any, ...] = (str(row[0]), str(row[1]), row[2])
        if phase == "baseline":
            normalized += (row[3], row[4], row[5])
        return normalized
    return tuple(str(value) if index < 1 else value for index, value in enumerate(row))


def _canonical_money(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except (InvalidOperation, TypeError, ValueError) as error:
        raise AdminError(f"Invalid canonical monetary value: {value!r}") from error


def _canonical_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value).strip()[:10]


def verify_database(
    connection: Any,
    fixture: dict[str, Any],
    database: str = FINAL_DATABASE,
    phase: str = "baseline",
) -> dict[str, Any]:
    if phase not in {"baseline", "runtime"}:
        raise AdminError("Verification phase must be baseline or runtime")
    validate_fixture(fixture)
    _assert_selected_database(connection, database)
    cursor = connection.cursor()
    try:
        _require_canonical_tables(cursor, database)
        counts = _table_counts(cursor)
        if phase == "baseline":
            expected_counts = {table: 20 for table in CORE_TABLES}
            expected_counts.update({table: 0 for table in OUTPUT_TABLES})
            if counts != expected_counts:
                raise AdminError(f"Baseline row counts differ: expected={expected_counts}, actual={counts}")

        expected_ids = _expected_core_ids(fixture)
        for table, column in ID_COLUMNS.items():
            cursor.execute(f"SELECT `{column}` FROM `{table}` ORDER BY `{column}`")
            actual = {row[0] for row in cursor.fetchall()}
            if actual != expected_ids[table]:
                raise AdminError(
                    f"{table} IDs differ; missing={sorted(expected_ids[table] - actual)}, "
                    f"unexpected={sorted(actual - expected_ids[table])}"
                )

        root_fingerprint = verify_canonical_root_content(cursor, fixture, phase=phase)

        for table in OUTPUT_TABLES:
            _assert_allowed_traces(_trace_rows(cursor, table), table=table)

        cursor.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL
            """,
            (database,),
        )
        foreign_keys = frozenset(tuple(row) for row in cursor.fetchall())
        if foreign_keys != EXPECTED_FOREIGN_KEYS:
            raise AdminError(
                f"Foreign keys differ; missing={sorted(EXPECTED_FOREIGN_KEYS - foreign_keys)}, "
                f"unexpected={sorted(foreign_keys - EXPECTED_FOREIGN_KEYS)}"
            )

        cursor.execute(
            """
            SELECT COLUMN_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'workflow_runs' AND COLUMN_NAME = 'status'
            """,
            (database,),
        )
        row = cursor.fetchone()
        workflow_status_type = str(row[0]) if row else ""
        if "'pending_human'" not in workflow_status_type or "'waiting_human'" in workflow_status_type:
            raise AdminError(f"workflow_runs.status has the wrong enum: {workflow_status_type!r}")

        orphans: dict[str, int] = {}
        for name, query in ORPHAN_QUERIES.items():
            cursor.execute(query)
            orphans[name] = int(cursor.fetchone()[0])
        if any(orphans.values()):
            raise AdminError(f"Foreign-key orphan checks failed: {orphans}")
        return {
            "phase": phase,
            "counts": counts,
            "orphans": orphans,
            "canonical_root_fingerprint": root_fingerprint,
        }
    finally:
        cursor.close()


def doctor(server_connection: Any, database: str = FINAL_DATABASE) -> dict[str, Any]:
    _validate_database_name(database)
    cursor = server_connection.cursor()
    try:
        cursor.execute("SELECT VERSION(), CURRENT_USER(), @@SESSION.sql_mode")
        version, current_user, sql_mode = cursor.fetchone()
        cursor.execute(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
            (database,),
        )
        exists = bool(cursor.fetchone()[0])
        return {
            "version": str(version),
            "current_user": str(current_user),
            "sql_mode": str(sql_mode),
            "database": database,
            "exists": exists,
        }
    finally:
        cursor.close()


def _add_common_read_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", required=True, choices=[FINAL_DATABASE])
    parser.add_argument("--env", help="Ignored env file; defaults to IDOX_DB_ENV or repository env files.")


def _add_common_write_args(parser: argparse.ArgumentParser) -> None:
    _add_common_read_args(parser)
    parser.add_argument("--confirm", required=True, choices=[FINAL_DATABASE])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely bootstrap and verify the standalone final database.")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor_parser = commands.add_parser("doctor", help="Read-only server and database reachability check.")
    _add_common_read_args(doctor_parser)

    create_parser = commands.add_parser("create", help="Create a new final database and apply its schema.")
    _add_common_write_args(create_parser)
    create_parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)

    seed_parser = commands.add_parser("seed", help="Seed the empty final database with demo01-demo20.")
    _add_common_write_args(seed_parser)
    seed_parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)

    reset_parser = commands.add_parser("reset", help="Remove only allowlisted demo output rows.")
    _add_common_write_args(reset_parser)

    verify_parser = commands.add_parser("verify", help="Read-only schema, fixture, count, and FK checks.")
    _add_common_read_args(verify_parser)
    verify_parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    verify_parser.add_argument("--phase", choices=["baseline", "runtime"], default="baseline")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_environment(args.env)
    try:
        if args.command == "doctor":
            server = connect(None)
            try:
                report = doctor(server, args.database)
            finally:
                server.close()
        elif args.command == "create":
            require_write_target(args.database, args.confirm)
            server = connect(None)
            try:
                create_database(server, args.database)
            finally:
                server.close()
            target = connect(args.database)
            try:
                apply_schema(target, args.database, args.schema)
            except Exception as error:
                raise AdminError(
                    "Database was created but schema application failed; inspect the partial final database "
                    "without dropping it automatically"
                ) from error
            finally:
                target.close()
            report = {"database": args.database, "created": True, "tables": len(APPLICATION_TABLES)}
        elif args.command == "seed":
            require_write_target(args.database, args.confirm)
            fixture = load_fixture(args.fixture)
            target = connect(args.database)
            try:
                seed_database(target, fixture, args.database)
                report = verify_database(target, fixture, args.database, phase="baseline")
            finally:
                target.close()
        elif args.command == "reset":
            require_write_target(args.database, args.confirm)
            fixture = load_fixture()
            target = connect(args.database)
            try:
                reset_demo(target, args.database)
                report = verify_database(target, fixture, args.database, phase="baseline")
            finally:
                target.close()
        else:
            fixture = load_fixture(args.fixture)
            target = connect(args.database)
            try:
                report = verify_database(target, fixture, args.database, phase=args.phase)
            finally:
                target.close()
    except (AdminError, mysql.connector.Error, OSError, ValueError) as error:
        print(f"Database admin failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
