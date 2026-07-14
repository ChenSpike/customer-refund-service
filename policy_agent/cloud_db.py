from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import mysql.connector

from .models import PolicyAgentInput, PolicyAgentOutput, TokenUsage


DATABASE_NAME = "main_db"
POLICY_AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = POLICY_AGENT_DIR.parent
DATABASE_ENV = REPO_ROOT / "database" / ".env"
RunMode = Literal["pending", "all", "trace"]

REQUIRED_COLUMNS = {
    "agent_handoffs": {
        "handoff_id",
        "trace_id",
        "ticket_id",
        "from_agent",
        "to_agent",
        "input_json",
        "output_json",
        "input_tokens",
        "output_tokens",
    },
    "audit_log": {"trace_id", "event_type", "agent", "payload_json"},
    "governance_events": {
        "event_id",
        "trace_id",
        "agent",
        "owasp_category",
        "trigger_score",
        "interceptor_action",
        "flags_json",
        "offending_content",
    },
    "human_approvals": {"approval_id", "trace_id", "reason", "amount_requested", "status", "notes"},
    "workflow_runs": {"trace_id", "status", "current_agent", "policy_version"},
}

OWASP_BY_FLAG = {
    "semantic_drift": "ASI01",
    "forbidden_tool": "ASI02",
    "pii_risk": "ASI07",
    "policy_conflict": "ASI08",
}


class CloudDatabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceHandoff:
    handoff_id: str
    trace_id: str
    ticket_id: str
    output_json: str

    def payload(self) -> dict[str, Any]:
        value = json.loads(self.output_json)
        if not isinstance(value, dict):
            raise ValueError(f"{self.trace_id}: triage output_json must be a JSON object")
        return value


@dataclass(frozen=True)
class CloudCaseRecord:
    handoff_id: str
    trace_id: str
    ticket_id: str
    to_agent: str
    input_json: dict[str, Any]
    output_json: dict[str, Any]
    input_tokens: int
    output_tokens: int
    workflow_input_tokens: int
    workflow_output_tokens: int
    workflow_status: str
    current_agent: str


class GCPRepository:
    """Read and write the unified GCP MySQL main_db."""

    def __init__(self, connection_config: dict[str, Any]) -> None:
        self.connection_config = connection_config

    @classmethod
    def from_env(cls) -> "GCPRepository":
        load_database_env()
        required = {name: os.getenv(name) for name in ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD")}
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise CloudDatabaseError("Missing MySQL settings: " + ", ".join(missing))
        return cls(
            {
                "host": required["MYSQL_HOST"],
                "port": int(os.getenv("MYSQL_PORT", "3306")),
                "user": required["MYSQL_USER"],
                "password": required["MYSQL_PASSWORD"],
                "database": DATABASE_NAME,
                "connection_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "10")),
            }
        )

    @staticmethod
    def config_status() -> dict[str, bool]:
        load_database_env()
        return {name: bool(os.getenv(name)) for name in ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD")}

    def check_schema(self) -> dict[str, int]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                """,
                (DATABASE_NAME,),
            )
            actual: dict[str, set[str]] = {}
            for table, column in cursor.fetchall():
                actual.setdefault(table, set()).add(column)
            missing = {
                table: sorted(columns - actual.get(table, set()))
                for table, columns in REQUIRED_COLUMNS.items()
                if columns - actual.get(table, set())
            }
            if missing:
                raise CloudDatabaseError(f"GCP main_db schema is missing required columns: {missing}")
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM agent_handoffs
                WHERE from_agent = 'triage_agent' AND to_agent = 'policy_agent'
                """
            )
            source_count = int(cursor.fetchone()[0])
            return {"source_handoffs": source_count, "required_tables": len(REQUIRED_COLUMNS)}
        finally:
            connection.close()

    def fetch_source_handoffs(self, mode: RunMode, trace_id: str | None = None) -> list[SourceHandoff]:
        if mode == "trace" and not trace_id:
            raise ValueError("trace mode requires trace_id")
        conditions = ["h.from_agent = 'triage_agent'", "h.to_agent = 'policy_agent'"]
        params: list[Any] = []
        if mode == "pending":
            conditions.append("w.current_agent = 'policy_agent'")
        if mode == "trace":
            conditions.append("h.trace_id = %s")
            params.append(trace_id)

        sql = f"""
            SELECT h.handoff_id, h.trace_id, h.ticket_id, h.output_json
            FROM agent_handoffs h
            JOIN workflow_runs w ON w.trace_id = h.trace_id
            WHERE {' AND '.join(conditions)}
            ORDER BY CAST(h.handoff_id AS UNSIGNED), h.trace_id
        """
        connection = self._connect()
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(sql, tuple(params))
            return [SourceHandoff(**row) for row in cursor.fetchall()]
        finally:
            connection.close()

    def persist_result(
        self,
        policy_input: PolicyAgentInput,
        output: PolicyAgentOutput,
        usage: TokenUsage,
    ) -> str:
        trace_id = policy_input.case.trace_id
        ticket_id = policy_input.case.ticket_id
        input_json = policy_input.model_dump_json()
        output_json = output.model_dump_json()
        connection = self._connect()
        try:
            connection.start_transaction()
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT handoff_id
                FROM agent_handoffs
                WHERE trace_id = %s AND ticket_id = %s AND from_agent = 'policy_agent'
                ORDER BY created_at DESC
                """,
                (trace_id, ticket_id),
            )
            existing = [row[0] for row in cursor.fetchall()]
            if len(existing) > 1:
                raise CloudDatabaseError(f"{trace_id}: multiple policy_agent handoff rows already exist")
            handoff_id = existing[0] if existing else self._next_handoff_id(cursor)

            cursor.execute(
                """
                INSERT INTO agent_handoffs (
                  handoff_id, trace_id, ticket_id, from_agent, to_agent,
                  input_json, output_json, input_tokens, output_tokens
                )
                VALUES (%s, %s, %s, 'policy_agent', %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  to_agent = VALUES(to_agent),
                  input_json = VALUES(input_json),
                  output_json = VALUES(output_json),
                  input_tokens = VALUES(input_tokens),
                  output_tokens = VALUES(output_tokens),
                  created_at = CURRENT_TIMESTAMP
                """,
                (
                    handoff_id,
                    trace_id,
                    ticket_id,
                    output.handoff.next_agent,
                    input_json,
                    output_json,
                    usage.input_tokens,
                    usage.output_tokens,
                ),
            )

            audit_payload = {
                "handoff_id": handoff_id,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "output": output.model_dump(mode="json"),
            }
            cursor.execute(
                """
                INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
                VALUES (%s, 'policy_agent_evaluated', 'policy_agent', %s)
                """,
                (trace_id, json.dumps(audit_payload, ensure_ascii=False)),
            )

            self._persist_governance(cursor, output)
            self._persist_human_approval(cursor, output)
            workflow_status, current_agent = _workflow_state(output)
            cursor.execute(
                """
                UPDATE workflow_runs
                SET status = %s, current_agent = %s, policy_version = %s,
                    completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE trace_id = %s
                """,
                (workflow_status, current_agent, output.case.policy_version_used, trace_id),
            )
            if cursor.rowcount != 1:
                raise CloudDatabaseError(f"{trace_id}: workflow_runs row was not updated")
            connection.commit()
            return handoff_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reset_policy_agent_data(self) -> dict[str, int]:
        """Return main_db to the triage -> policy_agent handoff baseline."""
        connection = self._connect()
        try:
            connection.start_transaction()
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM agent_handoffs
                WHERE from_agent = 'triage_agent' AND to_agent = 'policy_agent'
                """
            )
            counts = {"source_handoffs": int(cursor.fetchone()[0])}

            cursor.execute(
                """
                DELETE approvals
                FROM human_approvals approvals
                JOIN agent_handoffs source ON source.trace_id = approvals.trace_id
                WHERE source.from_agent = 'triage_agent' AND source.to_agent = 'policy_agent'
                """
            )
            counts["human_approvals"] = cursor.rowcount

            cursor.execute("DELETE FROM governance_events WHERE agent = 'policy_agent'")
            counts["governance_events"] = cursor.rowcount
            cursor.execute("DELETE FROM audit_log WHERE agent = 'policy_agent'")
            counts["audit_log"] = cursor.rowcount
            cursor.execute("DELETE FROM agent_handoffs WHERE from_agent = 'policy_agent'")
            counts["policy_handoffs"] = cursor.rowcount

            cursor.execute(
                """
                UPDATE workflow_runs workflow
                JOIN agent_handoffs source ON source.trace_id = workflow.trace_id
                SET workflow.status = 'running', workflow.current_agent = 'policy_agent',
                    workflow.completed_at = NULL, workflow.updated_at = CURRENT_TIMESTAMP
                WHERE source.from_agent = 'triage_agent' AND source.to_agent = 'policy_agent'
                """
            )
            counts["workflow_runs"] = cursor.rowcount
            connection.commit()

            cursor.execute("SELECT COUNT(*) FROM audit_log")
            if int(cursor.fetchone()[0]) == 0:
                cursor.execute("ALTER TABLE audit_log AUTO_INCREMENT = 1")
            return counts
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def policy_artifact_ids(self) -> dict[str, list[Any]]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            result: dict[str, list[Any]] = {}
            cursor.execute(
                """
                SELECT handoff_id
                FROM agent_handoffs
                WHERE from_agent = 'triage_agent' AND to_agent = 'policy_agent'
                ORDER BY CAST(handoff_id AS UNSIGNED)
                """
            )
            result["source_handoffs"] = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT handoff_id
                FROM agent_handoffs
                WHERE from_agent = 'policy_agent'
                ORDER BY CAST(handoff_id AS UNSIGNED)
                """
            )
            result["policy_handoffs"] = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT event_id
                FROM governance_events
                WHERE agent = 'policy_agent'
                ORDER BY created_at, event_id
                """
            )
            result["governance_events"] = [row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT approval_id FROM human_approvals ORDER BY created_at, approval_id")
            result["human_approvals"] = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT log_id, event_type
                FROM audit_log
                WHERE agent = 'policy_agent'
                ORDER BY log_id
                """
            )
            result["audit_log"] = list(cursor.fetchall())
            return result
        finally:
            connection.close()

    def record_failure(self, trace_id: str, error: Exception) -> None:
        payload = json.dumps({"error_type": type(error).__name__, "message": str(error)}, ensure_ascii=False)
        connection = self._connect()
        try:
            connection.start_transaction()
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
                VALUES (%s, 'policy_agent_failed', 'policy_agent', %s)
                """,
                (trace_id, payload),
            )
            cursor.execute(
                """
                UPDATE workflow_runs
                SET status = 'failed', current_agent = 'policy_agent', updated_at = CURRENT_TIMESTAMP
                WHERE trace_id = %s
                """,
                (trace_id,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fetch_case_records(self) -> list[CloudCaseRecord]:
        connection = self._connect()
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT
                  p.handoff_id, p.trace_id, p.ticket_id, p.to_agent,
                  p.input_json, p.output_json, p.input_tokens, p.output_tokens,
                  COALESCE(SUM(all_h.input_tokens), 0) AS workflow_input_tokens,
                  COALESCE(SUM(all_h.output_tokens), 0) AS workflow_output_tokens,
                  w.status AS workflow_status, w.current_agent
                FROM agent_handoffs p
                JOIN workflow_runs w ON w.trace_id = p.trace_id
                JOIN agent_handoffs all_h ON all_h.trace_id = p.trace_id
                WHERE p.from_agent = 'policy_agent'
                GROUP BY
                  p.handoff_id, p.trace_id, p.ticket_id, p.to_agent,
                  p.input_json, p.output_json, p.input_tokens, p.output_tokens,
                  w.status, w.current_agent
                ORDER BY p.trace_id
                """
            )
            records = []
            for row in cursor.fetchall():
                row["input_json"] = json.loads(row["input_json"])
                row["output_json"] = json.loads(row["output_json"])
                for key in ("input_tokens", "output_tokens", "workflow_input_tokens", "workflow_output_tokens"):
                    row[key] = int(row[key])
                records.append(CloudCaseRecord(**row))
            return records
        finally:
            connection.close()

    def integrity_counts(self) -> dict[str, int]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            result: dict[str, int] = {}
            for table in ("audit_log", "governance_events", "human_approvals"):
                cursor.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {table} child
                    LEFT JOIN workflow_runs w ON w.trace_id = child.trace_id
                    WHERE w.trace_id IS NULL
                    """
                )
                result[f"orphan_{table}"] = int(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM human_approvals")
            result["human_approvals"] = int(cursor.fetchone()[0])
            return result
        finally:
            connection.close()

    def _persist_governance(self, cursor: Any, output: PolicyAgentOutput) -> None:
        for flag in output.governance.flags:
            category = OWASP_BY_FLAG.get(flag)
            if category is None:
                continue
            details = [gap.detail for gap in output.policy_evaluation.gaps_or_conflicts]
            cursor.execute(
                """
                INSERT INTO governance_events (
                  event_id, trace_id, agent, owasp_category, trigger_score,
                  interceptor_action, flags_json, offending_content
                )
                VALUES (%s, %s, 'policy_agent', %s, %s, %s, %s, %s)
                """,
                (
                    self._next_prefixed_id(cursor, "governance_events", "event_id", "POL-GOV-"),
                    output.case.trace_id,
                    category,
                    output.governance.semantic_drift_score
                    if flag == "semantic_drift"
                    else output.decision.confidence,
                    output.governance.interceptor_action,
                    json.dumps({"flag": flag, "governance": output.governance.model_dump(mode="json")}),
                    "\n".join(details) or None,
                ),
            )

    def _persist_human_approval(self, cursor: Any, output: PolicyAgentOutput) -> None:
        requires_approval = (
            output.handoff.next_agent == "human_approval"
            or output.decision.type == "manual_review"
            or output.governance.interceptor_action in {"quarantine", "block"}
        )
        if not requires_approval:
            cursor.execute(
                "DELETE FROM human_approvals WHERE trace_id = %s AND status = 'pending'",
                (output.case.trace_id,),
            )
            return
        cursor.execute(
            "SELECT approval_id FROM human_approvals WHERE trace_id = %s ORDER BY created_at DESC",
            (output.case.trace_id,),
        )
        existing = [row[0] for row in cursor.fetchall()]
        if len(existing) > 1:
            raise CloudDatabaseError(f"{output.case.trace_id}: multiple human approval rows already exist")
        approval_id = existing[0] if existing else self._next_prefixed_id(
            cursor, "human_approvals", "approval_id", "POL-APP-"
        )
        notes = {
            "decision_reason": output.decision.reason,
            "customer_safe_summary": output.response_guidance.customer_safe_summary,
            "missing_info_to_request": output.response_guidance.missing_info_to_request,
            "governance_flags": output.governance.flags,
        }
        cursor.execute(
            """
            INSERT INTO human_approvals (
              approval_id, trace_id, reason, amount_requested, status, notes
            )
            VALUES (%s, %s, %s, %s, 'pending', %s)
            ON DUPLICATE KEY UPDATE
              reason = VALUES(reason),
              amount_requested = VALUES(amount_requested),
              notes = VALUES(notes),
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                approval_id,
                output.case.trace_id,
                output.handoff.reason[:255],
                output.customer_request.requested_amount,
                json.dumps(notes, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _next_handoff_id(cursor: Any) -> str:
        cursor.execute(
            """
            SELECT COALESCE(MAX(CAST(handoff_id AS UNSIGNED)), 0) + 1
            FROM agent_handoffs
            WHERE handoff_id REGEXP '^[0-9]+$'
            """
        )
        return str(int(cursor.fetchone()[0]))

    @staticmethod
    def _next_prefixed_id(cursor: Any, table: str, column: str, prefix: str) -> str:
        allowed = {
            ("governance_events", "event_id", "POL-GOV-"),
            ("human_approvals", "approval_id", "POL-APP-"),
        }
        if (table, column, prefix) not in allowed:
            raise ValueError("Unsupported sequential ID target")
        cursor.execute(
            f"""
            SELECT COALESCE(MAX(CAST(SUBSTRING({column}, %s) AS UNSIGNED)), 0) + 1
            FROM {table}
            WHERE {column} LIKE %s
            """,
            (len(prefix) + 1, prefix + "%"),
        )
        return f"{prefix}{int(cursor.fetchone()[0]):03d}"

    def _connect(self):
        try:
            return mysql.connector.connect(**self.connection_config)
        except mysql.connector.Error as error:
            raise CloudDatabaseError(f"Could not connect to GCP MySQL {DATABASE_NAME}: {error}") from error


def load_database_env() -> None:
    _load_env_file(REPO_ROOT / ".env")
    _load_env_file(POLICY_AGENT_DIR / ".env")
    _load_env_file(DATABASE_ENV)


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _workflow_state(output: PolicyAgentOutput) -> tuple[str, str]:
    if output.governance.interceptor_action == "block":
        return "paused_governance", "human_approval"
    if output.handoff.next_agent == "human_approval":
        return "pending_human", "human_approval"
    return "running", output.handoff.next_agent
