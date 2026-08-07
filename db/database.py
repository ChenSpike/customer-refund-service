from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import mysql.connector

from agents.policy.models import (
	GovernanceFinding,
	PolicyAgentInput,
	PolicyAgentOutput,
	PolicyReasoningResult,
	PrecedentContext,
	TokenUsage,
)


DATABASE_NAME = "main_db"
DB_DIR = Path(__file__).resolve().parent
REPO_ROOT = DB_DIR.parent
DATABASE_ENV = REPO_ROOT / "database" / ".env"
POLICY_MIGRATION_001_PATH = REPO_ROOT / "agents" / "policy" / "migrations" / "001_policy_governance_separation.sql"
POLICY_MIGRATION_002_PATH = REPO_ROOT / "agents" / "policy" / "migrations" / "002_unified_human_approval_trigger.sql"
RunMode = Literal["pending", "all", "trace", "benchmark"]
BENCHMARK_TRACE_PATTERN = r"^TRACE-POL-(00[1-9]|01[0-9]|020)$"

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
	"policy_review_events": {
		"policy_review_event_id",
		"trace_id",
		"policy_version",
		"review_type",
		"policy_ids_json",
		"evidence_json",
		"detail",
	},
	"human_approvals": {
		"approval_id",
		"trace_id",
		"triggering_event_id",
		"triggering_event_type",
		"reason",
		"amount_requested",
		"status",
		"approved_next_agent",
		"rejected_next_agent",
		"notes",
	},
	"workflow_runs": {"trace_id", "status", "current_agent", "policy_version"},
}

OWASP_BY_FLAG = {
	"semantic_drift": "ASI01",
	"forbidden_tool": "ASI02",
	"pii_risk": "ASI07",
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

	def migrate_schema(self) -> dict[str, bool]:
		separation = _migration_statements(POLICY_MIGRATION_001_PATH, expected=3)
		unified_trigger = _migration_statements(POLICY_MIGRATION_002_PATH, expected=4)

		connection = self._connect()
		changed = {
			"policy_review_events": False,
			"approval_routing_columns": False,
			"unified_approval_trigger": False,
		}
		try:
			cursor = connection.cursor()
			table_exists = self._table_exists(cursor, "policy_review_events")
			approval_columns = self._table_columns(cursor, "human_approvals")
			routing_columns = {"approved_next_agent", "rejected_next_agent"}
			present_routing = routing_columns & approval_columns
			if present_routing and present_routing != routing_columns:
				raise CloudDatabaseError(f"Partial Policy Agent migration detected: {sorted(present_routing)}")

			if not table_exists:
				cursor.execute(separation[0])
				changed["policy_review_events"] = True
			if not present_routing:
				cursor.execute(separation[1])
				changed["approval_routing_columns"] = True
				approval_columns = self._table_columns(cursor, "human_approvals")
			if (
				"policy_review_event_id" in approval_columns
				and not self._constraint_exists(cursor, "human_approvals", "fk_human_policy_review")
			):
				cursor.execute(separation[2])

			approval_columns = self._table_columns(cursor, "human_approvals")
			if "triggering_event_type" not in approval_columns:
				cursor.execute(unified_trigger[0])
				changed["unified_approval_trigger"] = True
				approval_columns.add("triggering_event_type")
			if "policy_review_event_id" in approval_columns:
				trigger_constraints = {
					name: self._constraint_exists(cursor, "human_approvals", name)
					for name in ("fk_human_approvals_event", "fk_human_policy_review")
				}
				if len(set(trigger_constraints.values())) != 1:
					raise CloudDatabaseError(
						f"Partial approval trigger constraint migration: {trigger_constraints}"
					)
				if all(trigger_constraints.values()):
					cursor.execute(unified_trigger[1])
				cursor.execute(unified_trigger[2])
				cursor.execute(
					"SELECT COUNT(*) FROM human_approvals "
					"WHERE triggering_event_id IS NULL OR triggering_event_type IS NULL"
				)
				if cursor.fetchone()[0]:
					raise CloudDatabaseError("Every human approval must have one typed triggering event")
				cursor.execute(unified_trigger[3])
				changed["unified_approval_trigger"] = True

			connection.commit()
			return changed
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

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
			return {"required_tables": len(REQUIRED_COLUMNS)}
		finally:
			connection.close()

	def fetch_source_handoffs(self, mode: RunMode, trace_id: str | None = None) -> list[SourceHandoff]:
		if mode == "trace" and not trace_id:
			raise ValueError("trace mode requires trace_id")
		conditions = ["h.from_agent = 'triage_agent'", "h.to_agent = 'policy_agent'"]
		params: list[Any] = []
		if mode == "pending":
			conditions.append("w.current_agent = 'policy_agent'")
		if mode == "benchmark":
			conditions.append("h.trace_id REGEXP %s")
			params.append(BENCHMARK_TRACE_PATTERN)
		if mode == "trace":
			conditions.append("h.trace_id = %s")
			params.append(trace_id)

		connection = self._connect()
		try:
			cursor = connection.cursor(dictionary=True)
			cursor.execute(
				f"""
				SELECT h.handoff_id, h.trace_id, h.ticket_id, h.output_json
				FROM agent_handoffs h
				JOIN workflow_runs w ON w.trace_id = h.trace_id
				WHERE {' AND '.join(conditions)}
				ORDER BY CAST(h.handoff_id AS UNSIGNED), h.trace_id
				""",
				tuple(params),
			)
			return [SourceHandoff(**row) for row in cursor.fetchall()]
		finally:
			connection.close()

	def persist_result(
		self,
		policy_input: PolicyAgentInput,
		output: PolicyAgentOutput,
		policy_result: PolicyReasoningResult,
		precedent_context: PrecedentContext,
		findings: list[GovernanceFinding],
		usage: TokenUsage,
	) -> str:
		trace_id = policy_input.case.trace_id
		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor()
			handoff_id = self._upsert_handoff(cursor, policy_input, output, usage)
			existing_approval_id = self._single_prefixed_id(
				cursor,
				"SELECT approval_id FROM human_approvals WHERE trace_id = %s AND approval_id LIKE 'POL-APP-%%'",
				trace_id,
				"human approval",
			)
			policy_event_id = self._persist_policy_review(cursor, output)
			governance_event_ids = self._persist_governance(cursor, output, findings)
			self._persist_human_approval(cursor, output, existing_approval_id, policy_event_id, governance_event_ids)
			audit_payload = {
				"handoff_id": handoff_id,
				"input_tokens": usage.input_tokens,
				"output_tokens": usage.output_tokens,
				"output": output.model_dump(mode="json"),
				"policy_evidence_manifest": policy_result.evidence_manifest.model_dump(mode="json"),
				"precedent_memory": {
					"available": precedent_context.available,
					"status": precedent_context.status,
					"reason": precedent_context.reason,
					"record_count": len(precedent_context.records),
				},
			}
			cursor.execute(
				"""
				INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
				VALUES (%s, 'policy_agent_evaluated', 'policy_agent', %s)
				""",
				(trace_id, json.dumps(audit_payload, ensure_ascii=False)),
			)
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

	def _upsert_handoff(self, cursor: Any, policy_input: PolicyAgentInput, output: PolicyAgentOutput, usage: TokenUsage) -> str:
		cursor.execute(
			"""
			SELECT handoff_id FROM agent_handoffs
			WHERE trace_id = %s AND ticket_id = %s AND from_agent = 'policy_agent'
			ORDER BY created_at DESC
			""",
			(policy_input.case.trace_id, policy_input.case.ticket_id),
		)
		existing = [row[0] for row in cursor.fetchall()]
		if len(existing) > 1:
			raise CloudDatabaseError(f"{policy_input.case.trace_id}: multiple policy_agent handoffs already exist")
		handoff_id = existing[0] if existing else self._next_handoff_id(cursor)
		cursor.execute(
			"""
			INSERT INTO agent_handoffs (
			  handoff_id, trace_id, ticket_id, from_agent, to_agent,
			  input_json, output_json, input_tokens, output_tokens
			)
			VALUES (%s, %s, %s, 'policy_agent', %s, %s, %s, %s, %s)
			ON DUPLICATE KEY UPDATE
			  to_agent = VALUES(to_agent), input_json = VALUES(input_json),
			  output_json = VALUES(output_json), input_tokens = VALUES(input_tokens),
			  output_tokens = VALUES(output_tokens), created_at = CURRENT_TIMESTAMP
			""",
			(
				handoff_id,
				policy_input.case.trace_id,
				policy_input.case.ticket_id,
				output.handoff.next_agent,
				policy_input.model_dump_json(),
				output.model_dump_json(),
				usage.input_tokens,
				usage.output_tokens,
			),
		)
		return handoff_id

	def _persist_policy_review(self, cursor: Any, output: PolicyAgentOutput) -> str | None:
		cursor.execute(
			"SELECT policy_review_event_id FROM policy_review_events WHERE trace_id = %s ORDER BY created_at",
			(output.case.trace_id,),
		)
		existing = [row[0] for row in cursor.fetchall()]
		if len(existing) > 1:
			raise CloudDatabaseError(f"{output.case.trace_id}: multiple policy review rows already exist")
		cursor.execute("DELETE FROM policy_review_events WHERE trace_id = %s", (output.case.trace_id,))
		if output.decision.type != "manual_review":
			return None
		event_id = existing[0] if existing else self._next_prefixed_id(cursor, "policy_review_events", "policy_review_event_id", "POL-REV-")
		gaps = output.policy_evaluation.gaps_or_conflicts
		review_type = "low_confidence" if any(gap.type == "low_confidence" for gap in gaps) else "policy_rule"
		review_policies = [policy.policy_id for policy in output.policy_evaluation.matched_policies if policy.effect == "requires_review"]
		if not review_policies:
			review_policies = [policy.policy_id for policy in output.policy_evaluation.matched_policies]
		evidence = {
			"matched_policies": [policy.model_dump(mode="json") for policy in output.policy_evaluation.matched_policies],
			"gaps_or_conflicts": [gap.model_dump(mode="json") for gap in gaps],
		}
		cursor.execute(
			"""
			INSERT INTO policy_review_events (
			  policy_review_event_id, trace_id, policy_version, review_type,
			  policy_ids_json, evidence_json, detail
			)
			VALUES (%s, %s, %s, %s, %s, %s, %s)
			""",
			(
				event_id,
				output.case.trace_id,
				output.case.policy_version_used,
				review_type,
				json.dumps(review_policies, ensure_ascii=False),
				json.dumps(evidence, ensure_ascii=False),
				output.decision.reason,
			),
		)
		return event_id

	def _persist_governance(self, cursor: Any, output: PolicyAgentOutput, findings: list[GovernanceFinding]) -> list[str]:
		cursor.execute(
			"SELECT event_id FROM governance_events WHERE trace_id = %s AND agent = 'policy_agent' ORDER BY created_at",
			(output.case.trace_id,),
		)
		existing = [row[0] for row in cursor.fetchall()]
		cursor.execute(
			"DELETE FROM governance_events WHERE trace_id = %s AND agent = 'policy_agent'",
			(output.case.trace_id,),
		)
		event_ids: list[str] = []
		for index, finding in enumerate(findings):
			event_id = existing[index] if index < len(existing) else self._next_prefixed_id(cursor, "governance_events", "event_id", "POL-GOV-")
			event_ids.append(event_id)
			score = finding.score
			if score is None and finding.flag == "semantic_drift":
				score = output.governance.semantic_drift_score
			cursor.execute(
				"""
				INSERT INTO governance_events (
				  event_id, trace_id, agent, owasp_category, trigger_score,
				  interceptor_action, flags_json, offending_content
				)
				VALUES (%s, %s, 'policy_agent', %s, %s, %s, %s, %s)
				""",
				(
					event_id,
					output.case.trace_id,
					OWASP_BY_FLAG[finding.flag],
					score,
					output.governance.interceptor_action,
					json.dumps({"finding": finding.model_dump(mode="json"), "governance": output.governance.model_dump(mode="json")}, ensure_ascii=False),
					finding.offending_content,
				),
			)
		return event_ids

	def _persist_human_approval(
		self,
		cursor: Any,
		output: PolicyAgentOutput,
		existing_approval_id: str | None,
		policy_event_id: str | None,
		governance_event_ids: list[str],
	) -> None:
		if output.handoff.next_agent != "human_approval":
			cursor.execute(
				"DELETE FROM human_approvals WHERE trace_id = %s AND approval_id LIKE 'POL-APP-%%'",
				(output.case.trace_id,),
			)
			return
		approval_id = existing_approval_id or self._next_prefixed_id(cursor, "human_approvals", "approval_id", "POL-APP-")
		trigger_type, trigger_id = _approval_trigger(output.case.trace_id, policy_event_id, governance_event_ids)
		notes = {
			"review_source": trigger_type,
			"decision_reason": output.decision.reason,
			"customer_safe_summary": output.response_guidance.customer_safe_summary,
			"governance_flags": output.governance.flags,
		}
		cursor.execute(
			"""
			INSERT INTO human_approvals (
			  approval_id, trace_id, triggering_event_id, triggering_event_type,
			  reason, amount_requested, status, approved_next_agent,
			  rejected_next_agent, notes
			)
			VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, 'response_agent', %s)
			ON DUPLICATE KEY UPDATE
			  triggering_event_id = VALUES(triggering_event_id),
			  triggering_event_type = VALUES(triggering_event_type),
			  reason = VALUES(reason), amount_requested = VALUES(amount_requested),
			  status = 'pending', approved_next_agent = VALUES(approved_next_agent),
			  rejected_next_agent = VALUES(rejected_next_agent), notes = VALUES(notes),
			  reviewer = NULL, resolved_at = NULL, updated_at = CURRENT_TIMESTAMP
			""",
			(
				approval_id,
				output.case.trace_id,
				trigger_id,
				trigger_type,
				output.handoff.reason[:255],
				output.customer_request.requested_amount,
				_approved_next_agent(output),
				json.dumps(notes, ensure_ascii=False),
			),
		)

	@staticmethod
	def _single_prefixed_id(cursor: Any, query: str, trace_id: str, label: str) -> str | None:
		cursor.execute(query, (trace_id,))
		existing = [row[0] for row in cursor.fetchall()]
		if len(existing) > 1:
			raise CloudDatabaseError(f"{trace_id}: multiple {label} rows already exist")
		return existing[0] if existing else None

	@staticmethod
	def _next_handoff_id(cursor: Any) -> str:
		cursor.execute(
			"""
			SELECT COALESCE(MAX(CAST(handoff_id AS UNSIGNED)), 0) + 1
			FROM agent_handoffs WHERE handoff_id REGEXP '^[0-9]+$'
			"""
		)
		return str(int(cursor.fetchone()[0]))

	@staticmethod
	def _next_prefixed_id(cursor: Any, table: str, column: str, prefix: str) -> str:
		allowed = {
			("policy_review_events", "policy_review_event_id", "POL-REV-"),
			("governance_events", "event_id", "POL-GOV-"),
			("human_approvals", "approval_id", "POL-APP-"),
		}
		if (table, column, prefix) not in allowed:
			raise ValueError("Unsupported sequential ID target")
		cursor.execute(
			f"""
			SELECT COALESCE(MAX(CAST(SUBSTRING({column}, %s) AS UNSIGNED)), 0) + 1
			FROM {table} WHERE {column} LIKE %s
			""",
			(len(prefix) + 1, prefix + "%"),
		)
		return f"{prefix}{int(cursor.fetchone()[0]):03d}"

	@staticmethod
	def _table_exists(cursor: Any, table: str) -> bool:
		cursor.execute(
			"SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
			(DATABASE_NAME, table),
		)
		return bool(cursor.fetchone()[0])

	@staticmethod
	def _table_columns(cursor: Any, table: str) -> set[str]:
		cursor.execute(
			"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
			(DATABASE_NAME, table),
		)
		return {row[0] for row in cursor.fetchall()}

	@staticmethod
	def _constraint_exists(cursor: Any, table: str, constraint: str) -> bool:
		cursor.execute(
			"""
			SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
			WHERE CONSTRAINT_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = %s
			""",
			(DATABASE_NAME, table, constraint),
		)
		return bool(cursor.fetchone()[0])

	def _connect(self):
		try:
			return mysql.connector.connect(**self.connection_config)
		except mysql.connector.Error as error:
			raise CloudDatabaseError(f"Could not connect to GCP MySQL {DATABASE_NAME}: {error}") from error


def load_database_env() -> None:
	_load_env_file(REPO_ROOT / ".env")
	_load_env_file(REPO_ROOT / "agents" / "policy" / ".env")
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
	if output.handoff.next_agent == "human_approval":
		return "pending_human", "human_approval"
	if output.handoff.next_agent == "refund_agent":
		return "running", "refund_agent"
	return "running", output.handoff.next_agent


def _approved_next_agent(output: PolicyAgentOutput) -> str:
	if output.decision.type in {"approve", "partial_refund"}:
		return "refund_agent"
	return "response_agent"


def _approval_trigger(trace_id: str, policy_event_id: str | None, governance_event_ids: list[str]) -> tuple[str, str]:
	if governance_event_ids:
		return "governance", governance_event_ids[0]
	if policy_event_id:
		return "policy_review", policy_event_id
	raise CloudDatabaseError(f"{trace_id}: human approval requires a governance or policy review event")


def _migration_statements(path: Path, expected: int) -> list[str]:
	statements = [statement.strip() for statement in path.read_text(encoding="utf-8").split(";") if statement.strip()]
	if len(statements) != expected:
		raise CloudDatabaseError(f"{path.name} must contain exactly {expected} SQL statements")
	return statements
