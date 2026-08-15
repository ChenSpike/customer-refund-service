from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
from governance import GovernanceStatement


DEFAULT_DATABASE_NAME = "final"
# Kept as a compatibility export for callers that import the old constant.
# Runtime repositories use their own configured database name instead.
DATABASE_NAME = DEFAULT_DATABASE_NAME
DB_DIR = Path(__file__).resolve().parent
REPO_ROOT = DB_DIR.parent
DATABASE_ENV = REPO_ROOT / "database" / ".env"
POLICY_MIGRATION_001_PATH = REPO_ROOT / "agents" / "policy" / "migrations" / "001_policy_governance_separation.sql"
POLICY_MIGRATION_002_PATH = REPO_ROOT / "agents" / "policy" / "migrations" / "002_unified_human_approval_trigger.sql"
RunMode = Literal["pending", "all", "trace", "benchmark"]
BENCHMARK_TRACE_PATTERN = r"^TRACE-POL-(00[1-9]|01[0-9]|020)$"
DEMO_TRACE_PATTERN = re.compile(r"^demo(?:0[1-9]|1[0-9]|20)$")
TRANSIENT_MYSQL_ERRORS = frozenset({2003, 2006, 2013})
MYSQL_CONNECT_ATTEMPTS = 3
DEFAULT_CONTINUATION_LEASE_SECONDS = 30

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
		"resolved_amount",
		"decision",
		"reviewer",
		"resolved_at",
	},
	"refund_transactions": {
		"transaction_id",
		"trace_id",
		"approval_id",
		"amount",
		"currency",
		"status",
		"external_ref",
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


class HumanApprovalError(CloudDatabaseError):
	"""Base class for typed dashboard review failures."""


class HumanApprovalNotFoundError(HumanApprovalError):
	"""The requested trace or approval does not exist."""


class HumanApprovalConflictError(HumanApprovalError):
	"""The requested mutation conflicts with persisted review state."""


class HumanApprovalStateError(HumanApprovalError):
	"""Persisted workflow data cannot support a safe continuation."""


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


@dataclass(frozen=True)
class HumanApprovalResolution:
	"""A durable review decision plus the state needed to continue statelessly."""

	approval_id: str
	trace_id: str
	ticket_id: str
	status: str
	decision: str
	resolved_amount: float | None
	reviewer: str
	notes: str
	next_agent: str
	review_trigger_stage: str
	state: dict[str, Any]
	idempotent: bool
	continuation_complete: bool
	continuation_resumable: bool


class GCPRepository:
	"""Read and write one configured GCP MySQL workflow database."""

	def __init__(self, connection_config: dict[str, Any]) -> None:
		self.connection_config = connection_config
		self.database_name = str(connection_config.get("database") or DEFAULT_DATABASE_NAME)

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
				"database": os.getenv("MYSQL_DATABASE", DEFAULT_DATABASE_NAME),
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
				(self.database_name,),
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
				raise CloudDatabaseError(
					f"GCP {self.database_name} schema is missing required columns: {missing}"
				)
			if "policy_review_event_id" in actual.get("human_approvals", set()):
				raise CloudDatabaseError(
					f"GCP {self.database_name} still has the legacy "
					"human_approvals.policy_review_event_id column"
				)
			cursor.execute(
				"""
				SELECT COLUMN_NAME, IS_NULLABLE
				FROM INFORMATION_SCHEMA.COLUMNS
				WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'human_approvals'
				  AND COLUMN_NAME IN ('triggering_event_id', 'triggering_event_type')
				""",
				(self.database_name,),
			)
			nullable_triggers = [
				name for name, nullable in cursor.fetchall() if nullable != "NO"
			]
			if nullable_triggers:
				raise CloudDatabaseError(
					f"Human approval trigger columns must be required: {nullable_triggers}"
				)
			legacy_constraints = [
				name
				for name in ("fk_human_approvals_event", "fk_human_policy_review")
				if self._constraint_exists(cursor, "human_approvals", name)
			]
			if legacy_constraints:
				raise CloudDatabaseError(
					f"Legacy single-parent approval constraints remain: {legacy_constraints}"
				)
			cursor.execute(
				"""
				SELECT COUNT(*), SUM(trace_id REGEXP %s)
				FROM agent_handoffs
				WHERE from_agent = 'triage_agent' AND to_agent = 'policy_agent'
				""",
				(BENCHMARK_TRACE_PATTERN,),
			)
			all_sources, benchmark_sources = cursor.fetchone()
			return {
				"source_handoffs": int(benchmark_sources or 0),
				"all_source_handoffs": int(all_sources),
				"required_tables": len(REQUIRED_COLUMNS),
			}
		finally:
			connection.close()

	def save_governance_event_record(
		self,
		statement: GovernanceStatement,
		*,
		followup_claim_token: str | None = None,
	) -> str:
		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor()
			_require_active_followup_claim(
				cursor,
				trace_id=statement.trace_id,
				claim_token=followup_claim_token,
			)
			if followup_claim_token is None:
				cursor.execute(
					"SELECT event_id FROM governance_events WHERE trace_id = %s AND agent = %s ORDER BY created_at",
					(statement.trace_id, statement.agent),
				)
				existing = [row[0] for row in cursor.fetchall()]
				if len(existing) > 1:
					raise CloudDatabaseError(f"{statement.trace_id}: multiple governance event rows already exist for {statement.agent}")
				event_id = existing[0] if existing else self._next_prefixed_id(
					cursor,
					"governance_events",
					"event_id",
					"GOV-STM-",
					f"{statement.trace_id}:{statement.agent}",
				)
			else:
				# A resumed customer turn is a distinct governance decision.  The
				# token-derived key also makes a same-attempt retry idempotent.
				event_id = self._next_prefixed_id(
					cursor,
					"governance_events",
					"event_id",
					"GOV-STM-",
					f"{statement.trace_id}:{statement.agent}:customer_followup:{followup_claim_token}",
				)
			owasp_category = _statement_owasp_category(statement)
			trigger_score = _statement_trigger_score(statement)
			interceptor_action = "block" if statement.status == "block" else "allow"
			flags_payload = _with_continuation_marker(
				_statement_flags_payload(statement),
				followup_claim_token,
			)
			offending_content = _statement_offending_content(statement)
			cursor.execute(
				"""
				INSERT INTO governance_events (
				  event_id, trace_id, agent, owasp_category, trigger_score, interceptor_action, flags_json, offending_content
				)
				VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
				ON DUPLICATE KEY UPDATE
				  owasp_category = VALUES(owasp_category),
				  trigger_score = VALUES(trigger_score),
				  interceptor_action = VALUES(interceptor_action),
				  flags_json = VALUES(flags_json),
				  offending_content = VALUES(offending_content),
				  created_at = CURRENT_TIMESTAMP
				""",
				(
					event_id,
					statement.trace_id,
					statement.agent,
					owasp_category,
					trigger_score,
					interceptor_action,
					json.dumps(flags_payload, ensure_ascii=False),
					offending_content,
				),
			)
			connection.commit()
			return event_id
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def get_governance_event_record(self, trace_id: str, agent: str) -> GovernanceStatement | None:
		connection = self._connect()
		try:
			cursor = connection.cursor(dictionary=True)
			cursor.execute(
				"""
				SELECT trace_id, agent, interceptor_action, flags_json, created_at
				FROM governance_events
				WHERE trace_id = %s AND agent = %s
				ORDER BY created_at DESC,
				         CASE WHEN flags_json LIKE '%%\"_continuation\"%%' THEN 1 ELSE 0 END DESC,
				         event_id DESC
				LIMIT 1
				""",
				(trace_id, agent),
			)
			row = cursor.fetchone()
			if row is None:
				return None
			payload = json.loads(row["flags_json"]) if row["flags_json"] else {}
			findings = []
			if isinstance(payload, dict) and payload.get("finding"):
				findings = [payload["finding"]]
			status = "block" if row["interceptor_action"] in {"block", "quarantine"} else "allow"
			return GovernanceStatement.model_validate(
				{
					"trace_id": row["trace_id"],
					"agent": row["agent"],
					"stage": row["agent"],
					"status": status,
					"summary": _statement_summary_from_payload(row["agent"], status, payload),
					"findings": findings,
					"created_at": row["created_at"],
				}
			)
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
		followup_claim_token: str | None = None,
	) -> str:
		trace_id = policy_input.case.trace_id
		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor()
			_require_active_followup_claim(
				cursor,
				trace_id=trace_id,
				claim_token=followup_claim_token,
			)
			# Lock and reject resolved review history before replacing Policy
			# handoffs, review events, or governance events.  This keeps a legacy
			# trigger FK (including an unexpected cascading FK) from deleting a
			# resolved approval before the rerun safety check can see it.
			self._assert_policy_approval_history_mutable(cursor, trace_id)
			handoff_id = self._upsert_handoff(
				cursor,
				policy_input,
				output,
				usage,
				followup_claim_token=followup_claim_token,
			)
			policy_event_id = self._persist_policy_review(
				cursor,
				output,
				followup_claim_token=followup_claim_token,
			)
			governance_event_ids = self._persist_governance(
				cursor,
				output,
				findings,
				followup_claim_token=followup_claim_token,
			)
			self._persist_human_approval(cursor, output, policy_event_id, governance_event_ids)
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
			audit_payload = _with_continuation_marker(audit_payload, followup_claim_token)
			if followup_claim_token is None:
				cursor.execute(
					"DELETE FROM audit_log WHERE trace_id = %s "
					"AND event_type = 'policy_agent_evaluated' AND agent = 'policy_agent'",
					(trace_id,),
				)
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
			_require_workflow_row(cursor, trace_id)
			connection.commit()
			return handoff_id
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def persist_agent_handoff(
		self,
		*,
		trace_id: str,
		ticket_id: str,
		from_agent: str,
		to_agent: str,
		input_payload: dict[str, Any],
		output_payload: dict[str, Any],
		input_tokens: int = 0,
		output_tokens: int = 0,
		audit_event_type: str,
		workflow_status: str,
		current_agent: str,
		followup_claim_token: str | None = None,
	) -> str:
		database_status = _database_workflow_status(workflow_status)
		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor()
			_require_active_followup_claim(
				cursor,
				trace_id=trace_id,
				claim_token=followup_claim_token,
			)
			handoff_id = self._upsert_generic_handoff(
				cursor,
				trace_id=trace_id,
				ticket_id=ticket_id,
				from_agent=from_agent,
				to_agent=to_agent,
				input_payload=input_payload,
				output_payload=output_payload,
				input_tokens=input_tokens,
				output_tokens=output_tokens,
				followup_claim_token=followup_claim_token,
			)
			# Retried stages replace their canonical audit event instead of
			# accumulating misleading duplicate evaluations.
			if followup_claim_token is None:
				cursor.execute(
					"DELETE FROM audit_log WHERE trace_id = %s AND event_type = %s AND agent = %s",
					(trace_id, audit_event_type, from_agent),
				)
			cursor.execute(
				"""
				INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
				VALUES (%s, %s, %s, %s)
				""",
				(
					trace_id,
					audit_event_type,
					from_agent,
					json.dumps(
						_with_continuation_marker({
							"handoff_id": handoff_id,
							"from_agent": from_agent,
							"to_agent": to_agent,
							"input": input_payload,
							"output": output_payload,
							"input_tokens": input_tokens,
							"output_tokens": output_tokens,
						}, followup_claim_token),
						ensure_ascii=False,
					),
				),
			)
			cursor.execute(
				"""
				UPDATE workflow_runs
				SET status = %s, current_agent = %s,
				    completed_at = CASE WHEN %s = 'completed' THEN CURRENT_TIMESTAMP ELSE NULL END,
				    updated_at = CURRENT_TIMESTAMP
				WHERE trace_id = %s
				""",
				(database_status, current_agent, database_status, trace_id),
			)
			_require_workflow_row(cursor, trace_id)
			connection.commit()
			return handoff_id
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def persist_refund_result(
		self,
		*,
		trace_id: str,
		ticket_id: str,
		policy_decision: dict[str, Any],
		order_lookup_result: dict[str, Any],
		refund_result: dict[str, Any],
		followup_claim_token: str | None = None,
	) -> tuple[str, str]:
		"""Persist one idempotent refund transaction, handoff, audit row, and route."""

		if not trace_id or not ticket_id:
			raise CloudDatabaseError("Refund persistence requires trace_id and ticket_id")
		runtime_status = str(refund_result.get("status") or "")
		transaction_status = {"success": "issued", "failed": "failed"}.get(runtime_status)
		if transaction_status is None:
			raise CloudDatabaseError(f"Unsupported refund result status: {runtime_status}")
		amount = float(refund_result.get("amount") or 0)
		if transaction_status == "issued" and amount <= 0:
			raise CloudDatabaseError("Issued refund requires a positive amount")
		transaction_identity = f"idox-refund:{trace_id}"
		if followup_claim_token is not None:
			transaction_identity += f":customer_followup:{followup_claim_token}"
		transaction_id = str(uuid.uuid5(uuid.NAMESPACE_URL, transaction_identity))

		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor()
			_require_active_followup_claim(
				cursor,
				trace_id=trace_id,
				claim_token=followup_claim_token,
			)
			cursor.execute(
				"""
				SELECT approval_id FROM human_approvals
				WHERE trace_id = %s AND status = 'approved'
				ORDER BY resolved_at DESC, created_at DESC
				LIMIT 1
				""",
				(trace_id,),
			)
			approval = cursor.fetchone()
			approval_id = approval[0] if approval else None
			cursor.execute(
				"""
				INSERT INTO refund_transactions (
				  transaction_id, trace_id, approval_id, amount, currency, status, external_ref
				)
				VALUES (%s, %s, %s, %s, %s, %s, %s)
				ON DUPLICATE KEY UPDATE
				  approval_id = VALUES(approval_id), amount = VALUES(amount),
				  currency = VALUES(currency), status = VALUES(status),
				  external_ref = VALUES(external_ref), updated_at = CURRENT_TIMESTAMP
				""",
				(
					transaction_id,
					trace_id,
					approval_id,
					amount,
					str(refund_result.get("currency") or "USD"),
					transaction_status,
					refund_result.get("refund_id"),
				),
			)
			handoff_id = self._upsert_generic_handoff(
				cursor,
				trace_id=trace_id,
				ticket_id=ticket_id,
				from_agent="refund_agent",
				to_agent="response_agent",
				input_payload={
					"policy_decision": policy_decision,
					"order_lookup_result": order_lookup_result,
				},
				output_payload={"refund_result": refund_result},
				input_tokens=0,
				output_tokens=0,
				followup_claim_token=followup_claim_token,
			)
			if followup_claim_token is None:
				cursor.execute(
					"DELETE FROM audit_log WHERE trace_id = %s AND agent = 'refund_agent'",
					(trace_id,),
				)
			cursor.execute(
				"""
				INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
				VALUES (%s, %s, 'refund_agent', %s)
				""",
				(
					trace_id,
					"refund_issued" if transaction_status == "issued" else "refund_failed",
					json.dumps(
						_with_continuation_marker({
							"transaction_id": transaction_id,
							"handoff_id": handoff_id,
							"refund_result": refund_result,
						}, followup_claim_token),
						ensure_ascii=False,
					),
				),
			)
			cursor.execute(
				"""
				UPDATE workflow_runs
				SET status = 'running', current_agent = 'response_agent', updated_at = CURRENT_TIMESTAMP
				WHERE trace_id = %s
				""",
				(trace_id,),
			)
			_require_workflow_row(cursor, trace_id)
			connection.commit()
			return transaction_id, handoff_id
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def ensure_human_approval(
		self,
		*,
		trace_id: str,
		reason: str,
		stage: str,
		policy_decision: dict[str, Any] | None = None,
		followup_claim_token: str | None = None,
	) -> str:
		"""Create the one pending review row for a routed workflow, idempotently."""

		_validate_demo_trace_id(trace_id)
		stage_agent = {
			"triage": "triage_agent",
			"policy": "policy_agent",
			"response": "response_agent",
		}.get(stage)
		if stage_agent is None:
			raise ValueError("stage must be triage, policy, or response")

		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor()
			_require_active_followup_claim(
				cursor,
				trace_id=trace_id,
				claim_token=followup_claim_token,
			)
			cursor.execute(
				"SELECT approval_id FROM human_approvals "
				"WHERE trace_id = %s AND status = 'pending' ORDER BY created_at FOR UPDATE",
				(trace_id,),
			)
			existing = [row[0] for row in cursor.fetchall()]
			if len(existing) > 1:
				raise HumanApprovalConflictError(
					f"{trace_id}: multiple pending human approval rows already exist"
				)
			if existing:
				connection.commit()
				return existing[0]

			cursor.execute(
				"""
				SELECT event_id FROM governance_events
				WHERE trace_id = %s AND agent = %s
				  AND interceptor_action IN ('block', 'quarantine')
				ORDER BY created_at DESC, event_id DESC LIMIT 1
				""",
				(trace_id, stage_agent),
			)
			governance_event = cursor.fetchone()
			if governance_event:
				trigger_type, trigger_id = "governance", governance_event[0]
			elif stage == "policy":
				cursor.execute(
					"""
					SELECT policy_review_event_id FROM policy_review_events
					WHERE trace_id = %s
					ORDER BY created_at DESC, policy_review_event_id DESC LIMIT 1
					""",
					(trace_id,),
				)
				policy_event = cursor.fetchone()
				if not policy_event:
					raise HumanApprovalStateError(
						f"{trace_id}: human approval has no governance or policy trigger"
					)
				trigger_type, trigger_id = "policy_review", policy_event[0]
			else:
				raise HumanApprovalStateError(
					f"{trace_id}: {stage} approval has no blocked {stage_agent} governance event"
				)

			cursor.execute(
				"""
				SELECT tickets.requested_amount
				FROM workflow_runs JOIN tickets ON tickets.ticket_id = workflow_runs.ticket_id
				WHERE workflow_runs.trace_id = %s
				""",
				(trace_id,),
			)
			amount_row = cursor.fetchone()
			if amount_row is None:
				raise HumanApprovalStateError(f"{trace_id}: workflow ticket is missing")
			decision = (policy_decision or {}).get("decision")
			approved_next_agent = {
				"triage": "policy_agent",
				"response": "end",
			}.get(stage)
			if approved_next_agent is None:
				approved_next_agent = (
					"refund_agent"
					if decision in {"approve", "partial_refund", "manual_review"}
					else "response_agent"
				)
			cursor.execute(
				"SELECT COUNT(*) FROM human_approvals "
				"WHERE trace_id = %s AND triggering_event_type = %s AND triggering_event_id = %s",
				(trace_id, trigger_type, trigger_id),
			)
			trigger_ordinal = int(cursor.fetchone()[0]) + 1
			approval_id = str(
				uuid.uuid5(
					uuid.NAMESPACE_URL,
					f"idox-approval:{trace_id}:{trigger_type}:{trigger_id}:{trigger_ordinal}",
				)
			)
			cursor.execute(
				"""
				INSERT INTO human_approvals (
				  approval_id, trace_id, triggering_event_id, triggering_event_type,
				  reason, amount_requested, status, approved_next_agent,
				  rejected_next_agent, notes
				)
				VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, 'response_agent', %s)
				""",
				(
					approval_id,
					trace_id,
					trigger_id,
					trigger_type,
					reason[:255],
					amount_row[0],
					approved_next_agent,
					json.dumps({"review_source": stage}, ensure_ascii=False),
				),
			)
			cursor.execute(
				"""
				UPDATE workflow_runs
				SET status = 'pending_human', current_agent = 'human_approval', updated_at = CURRENT_TIMESTAMP
				WHERE trace_id = %s
				""",
				(trace_id,),
			)
			_require_workflow_row(cursor, trace_id)
			connection.commit()
			return approval_id
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def resolve_human_approval(
		self,
		*,
		trace_id: str,
		decision: str,
		resolved_amount: float | Decimal | None,
		reviewer: str,
		notes: str,
		approval_id: str | None = None,
		continuation_stale_after_seconds: int = DEFAULT_CONTINUATION_LEASE_SECONDS,
	) -> HumanApprovalResolution:
		"""Resolve one pending demo review and claim its continuation atomically.

		The database transaction deliberately ends before Refund or Response runs.
		Those stages may involve a network call and therefore must never execute
		while the human-approval row lock is held.  Their writes are idempotent, and
		``mark_human_approval_continuation`` records the durable terminal marker.
		"""

		_validate_demo_trace_id(trace_id)
		if (
			isinstance(continuation_stale_after_seconds, bool)
			or not isinstance(continuation_stale_after_seconds, int)
			or not 1 <= continuation_stale_after_seconds <= 3600
		):
			raise ValueError("continuation_stale_after_seconds must be an integer from 1 to 3600")
		normalized_decision, normalized_amount, normalized_reviewer, normalized_notes = (
			_validate_review_request(
				decision=decision,
				resolved_amount=resolved_amount,
				reviewer=reviewer,
				notes=notes,
			)
		)
		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor(dictionary=True)
			cursor.execute(
				"""
				SELECT
				  approvals.approval_id, approvals.trace_id,
				  approvals.triggering_event_id, approvals.triggering_event_type,
				  approvals.reason, approvals.amount_requested,
				  approvals.resolved_amount, approvals.status, approvals.decision,
				  approvals.approved_next_agent, approvals.rejected_next_agent,
				  approvals.reviewer, approvals.notes, approvals.resolved_at,
				  workflows.ticket_id, workflows.status AS workflow_status,
				  workflows.current_agent, workflows.policy_version,
				  tickets.customer_id, tickets.raw_text, tickets.sanitized_text,
				  tickets.refund_reason, tickets.requested_amount,
				  tickets.currency AS ticket_currency,
				  customers.email AS customer_email,
				  customers.full_name AS customer_name,
				  governance.agent AS governance_agent
				FROM human_approvals approvals
				JOIN workflow_runs workflows ON workflows.trace_id = approvals.trace_id
				JOIN tickets ON tickets.ticket_id = workflows.ticket_id
				JOIN customers ON customers.customer_id = tickets.customer_id
				LEFT JOIN governance_events governance
				  ON approvals.triggering_event_type = 'governance'
				 AND governance.event_id = approvals.triggering_event_id
				WHERE approvals.trace_id = %s
				ORDER BY approvals.created_at DESC
				FOR UPDATE
				""",
				(trace_id,),
			)
			rows = list(cursor.fetchall())
			if not rows:
				raise HumanApprovalNotFoundError(f"{trace_id}: no human approval exists")
			if approval_id is not None:
				matching = [row for row in rows if row["approval_id"] == approval_id]
				if not matching:
					raise HumanApprovalNotFoundError(
						f"{trace_id}: approval_id does not match this workflow"
					)
				row = matching[0]
			else:
				pending = [row for row in rows if row["status"] == "pending"]
				if len(pending) > 1:
					raise HumanApprovalConflictError(
						f"{trace_id}: multiple pending human approvals exist"
					)
				row = pending[0] if pending else rows[0]

			state = _reconstruct_review_state(cursor, row)
			resolution_status = (
				"approved" if normalized_decision in {"approve", "partial_refund"} else "rejected"
			)
			next_agent = (
				row.get("approved_next_agent")
				if resolution_status == "approved"
				else row.get("rejected_next_agent")
			) or "response_agent"
			if next_agent not in {"policy_agent", "refund_agent", "response_agent", "end"}:
				raise HumanApprovalStateError(
					f"{trace_id}: unsupported human-approval continuation route {next_agent!r}"
				)
			normalized_amount = _validate_review_amount(
				trace_id=trace_id,
				decision=normalized_decision,
				resolved_amount=normalized_amount,
				next_agent=next_agent,
				amount_requested=_optional_decimal(row.get("amount_requested")),
				order_lookup=state["order_lookup_result"],
			)

			idempotent = row["status"] != "pending"
			if idempotent:
				stored = (
					str(row.get("status") or ""),
					str(row.get("decision") or ""),
					_optional_decimal(row.get("resolved_amount")),
					str(row.get("reviewer") or ""),
					str(row.get("notes") or ""),
				)
				requested = (
					resolution_status,
					normalized_decision,
					normalized_amount,
					normalized_reviewer,
					normalized_notes,
				)
				if stored != requested:
					raise HumanApprovalConflictError(
						f"{trace_id}: approval was already resolved with a different decision"
					)
			else:
				cursor.execute(
					"""
					UPDATE human_approvals
					SET status = %s, decision = %s, resolved_amount = %s,
					    reviewer = %s, notes = %s, resolved_at = CURRENT_TIMESTAMP,
					    updated_at = CURRENT_TIMESTAMP
					WHERE approval_id = %s AND status = 'pending'
					""",
					(
						resolution_status,
						normalized_decision,
						normalized_amount,
						normalized_reviewer,
						normalized_notes,
						row["approval_id"],
					),
				)
				if cursor.rowcount != 1:
					raise HumanApprovalConflictError(
						f"{trace_id}: pending approval was not resolved"
					)
				handoff_id = str(
					uuid.uuid5(
						uuid.NAMESPACE_URL,
						f"idox-handoff:{trace_id}:human_approval:{row['approval_id']}",
					)
				)
				resolution_payload = {
					"approval_id": row["approval_id"],
					"status": resolution_status,
					"decision": normalized_decision,
					"resolved_amount": _decimal_as_float(normalized_amount),
					"reviewer": normalized_reviewer,
					"notes": normalized_notes,
					"next_agent": next_agent,
				}
				cursor.execute(
					"""
					INSERT INTO agent_handoffs (
					  handoff_id, trace_id, ticket_id, from_agent, to_agent,
					  input_json, output_json, input_tokens, output_tokens
					)
					VALUES (%s, %s, %s, 'human_approval', %s, %s, %s, 0, 0)
					ON DUPLICATE KEY UPDATE
					  to_agent = VALUES(to_agent), input_json = VALUES(input_json),
					  output_json = VALUES(output_json), created_at = CURRENT_TIMESTAMP
					""",
					(
						handoff_id,
						trace_id,
						row["ticket_id"],
						next_agent,
						json.dumps(
							{"approval_id": row["approval_id"], "decision": normalized_decision},
							ensure_ascii=False,
						),
						json.dumps(resolution_payload, ensure_ascii=False),
					),
				)
				cursor.execute(
					"""
					INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
					VALUES (%s, 'human_approval_resolved', 'human_approval', %s)
					""",
					(trace_id, json.dumps({**resolution_payload, "handoff_id": handoff_id}, ensure_ascii=False)),
				)
				workflow_status = "completed" if next_agent == "end" else "running"
				workflow_agent = "completed" if next_agent == "end" else next_agent
				cursor.execute(
					"""
					UPDATE workflow_runs
					SET status = %s, current_agent = %s,
					    completed_at = CASE WHEN %s = 'completed' THEN CURRENT_TIMESTAMP ELSE NULL END,
					    updated_at = CURRENT_TIMESTAMP
					WHERE trace_id = %s
					""",
					(workflow_status, workflow_agent, workflow_status, trace_id),
				)
				_require_workflow_row(cursor, trace_id)

			continuation_complete = _approval_continuation_complete(
				cursor,
				trace_id=trace_id,
				approval_id=str(row["approval_id"]),
			)
			continuation_resumable = False
			if not continuation_complete:
				claim = _approval_continuation_claim(
					cursor,
					trace_id=trace_id,
					approval_id=str(row["approval_id"]),
				)
				claim_age = int(claim.get("age_seconds") or 0) if claim is not None else None
				continuation_resumable = (
					not idempotent
					or str(row.get("workflow_status") or "") == "failed"
					or claim is None
					or claim_age >= continuation_stale_after_seconds
				)
				if continuation_resumable:
					_upsert_approval_continuation_claim(
						cursor,
						trace_id=trace_id,
						approval_id=str(row["approval_id"]),
						next_agent=next_agent,
						existing_claim=claim,
					)
			state = _apply_review_resolution_to_state(
				state,
				approval_id=str(row["approval_id"]),
				status=resolution_status,
				decision=normalized_decision,
				resolved_amount=normalized_amount,
				reviewer=normalized_reviewer,
				notes=normalized_notes,
				next_agent=next_agent,
			)
			if idempotent:
				state["workflow_status"] = str(row.get("workflow_status") or "completed")
				state["current_stage"] = str(row.get("current_agent") or "completed")
			connection.commit()
			return HumanApprovalResolution(
				approval_id=str(row["approval_id"]),
				trace_id=trace_id,
				ticket_id=str(row["ticket_id"]),
				status=resolution_status,
				decision=normalized_decision,
				resolved_amount=_decimal_as_float(normalized_amount),
				reviewer=normalized_reviewer,
				notes=normalized_notes,
				next_agent=next_agent,
				review_trigger_stage=_review_trigger_stage(row),
				state=state,
				idempotent=idempotent,
				continuation_complete=continuation_complete,
				continuation_resumable=continuation_resumable,
			)
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def mark_human_approval_continuation(
		self,
		*,
		trace_id: str,
		approval_id: str,
		workflow_status: str,
		current_agent: str,
		summary: dict[str, Any],
	) -> bool:
		"""Persist the terminal marker for a successful continuation."""

		_validate_demo_trace_id(trace_id)
		if workflow_status not in {"completed", "waiting_user", "pending_human", "failed"}:
			raise CloudDatabaseError(f"Unsupported review continuation status: {workflow_status}")
		if not current_agent or len(current_agent) > 255:
			raise ValueError("current_agent is required and must be at most 255 characters")
		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor(dictionary=True)
			cursor.execute(
				"SELECT approval_id, status FROM human_approvals "
				"WHERE trace_id = %s AND approval_id = %s FOR UPDATE",
				(trace_id, approval_id),
			)
			row = cursor.fetchone()
			if row is None:
				raise HumanApprovalNotFoundError(f"{trace_id}: approval does not exist")
			if row["status"] == "pending":
				raise HumanApprovalConflictError(
					f"{trace_id}: pending approval cannot be marked continued"
				)
			existing_marker = _approval_continuation_record(
				cursor,
				trace_id=trace_id,
				approval_id=approval_id,
			)
			if existing_marker is not None:
				if (
					existing_marker.get("workflow_status") != workflow_status
					or existing_marker.get("current_agent") != current_agent
				):
					raise HumanApprovalConflictError(
						f"{trace_id}: continuation was already completed with a different terminal state"
					)
				connection.commit()
				return False
			else:
				cursor.execute(
					"""
					INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
					VALUES (%s, 'human_approval_continued', 'human_approval', %s)
					""",
					(
						trace_id,
						json.dumps(
							{
								"approval_id": approval_id,
								"workflow_status": workflow_status,
								"current_agent": current_agent,
								"summary": summary,
							},
							ensure_ascii=False,
							default=str,
						),
					),
				)
			cursor.execute(
				"""
				UPDATE workflow_runs
				SET status = %s, current_agent = %s,
				    completed_at = CASE WHEN %s = 'completed' THEN CURRENT_TIMESTAMP ELSE NULL END,
				    updated_at = CURRENT_TIMESTAMP
				WHERE trace_id = %s
				""",
				(workflow_status, current_agent, workflow_status, trace_id),
			)
			_require_workflow_row(cursor, trace_id)
			connection.commit()
			return True
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def record_human_approval_continuation_failure(
		self,
		*,
		trace_id: str,
		approval_id: str,
		error: Exception,
	) -> None:
		"""Fail closed if an approved continuation cannot finish."""

		_validate_demo_trace_id(trace_id)
		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor()
			payload = {
				"approval_id": approval_id,
				"error_type": type(error).__name__,
				"message": str(error)[:500],
			}
			cursor.execute(
				"""
				INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
				VALUES (%s, 'human_approval_continuation_failed', 'human_approval', %s)
				""",
				(trace_id, json.dumps(payload, ensure_ascii=False)),
			)
			cursor.execute(
				"""
				UPDATE workflow_runs
				SET status = 'failed', current_agent = 'human_approval',
				    completed_at = NULL, updated_at = CURRENT_TIMESTAMP
				WHERE trace_id = %s
				""",
				(trace_id,),
			)
			_require_workflow_row(cursor, trace_id)
			connection.commit()
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def reset_policy_agent_data(self) -> dict[str, int]:
		"""Prepare the 20 benchmark workflows for an idempotent Policy rerun.

		Event and approval rows keep their stable identities until each trace is
		replaced. Downstream tables may reference those approvals, so deleting them
		would either violate foreign keys or remove data owned by another agent.
		"""

		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor()
			cursor.execute(
				"""
				SELECT COUNT(*) FROM agent_handoffs
				WHERE from_agent = 'triage_agent' AND to_agent = 'policy_agent'
				  AND trace_id REGEXP %s
				""",
				(BENCHMARK_TRACE_PATTERN,),
			)
			counts = {"source_handoffs": int(cursor.fetchone()[0])}

			deletions = (
				(
					"audit_log",
					"""
					DELETE logs FROM audit_log logs
					JOIN agent_handoffs source ON source.trace_id = logs.trace_id
					WHERE source.from_agent = 'triage_agent'
					  AND source.to_agent = 'policy_agent'
					  AND source.trace_id REGEXP %s
					  AND logs.agent = 'policy_agent'
					""",
				),
				(
					"policy_handoffs",
					"""
					DELETE target FROM agent_handoffs target
					JOIN agent_handoffs source ON source.trace_id = target.trace_id
					WHERE source.from_agent = 'triage_agent'
					  AND source.to_agent = 'policy_agent'
					  AND source.trace_id REGEXP %s
					  AND target.from_agent = 'policy_agent'
					""",
				),
			)
			for name, statement in deletions:
				cursor.execute(statement, (BENCHMARK_TRACE_PATTERN,))
				counts[name] = cursor.rowcount

			for name, table, condition in (
				("retained_policy_review_events", "policy_review_events", "1 = 1"),
				("retained_governance_events", "governance_events", "agent = 'policy_agent'"),
				("retained_human_approvals", "human_approvals", "approval_id LIKE 'POL-APP-%%'"),
			):
				cursor.execute(
					f"SELECT COUNT(*) FROM {table} WHERE trace_id REGEXP %s AND {condition}",
					(BENCHMARK_TRACE_PATTERN,),
				)
				counts[name] = int(cursor.fetchone()[0])

			cursor.execute(
				"""
				UPDATE workflow_runs workflow
				JOIN agent_handoffs source ON source.trace_id = workflow.trace_id
				SET workflow.status = 'running',
				    workflow.current_agent = 'policy_agent',
				    workflow.completed_at = NULL,
				    workflow.updated_at = CURRENT_TIMESTAMP
				WHERE source.from_agent = 'triage_agent'
				  AND source.to_agent = 'policy_agent'
				  AND source.trace_id REGEXP %s
				""",
				(BENCHMARK_TRACE_PATTERN,),
			)
			counts["workflow_runs"] = cursor.rowcount
			connection.commit()
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
			queries = {
				"source_handoffs": """
					SELECT handoff_id FROM agent_handoffs
					WHERE from_agent = 'triage_agent' AND to_agent = 'policy_agent'
					  AND trace_id REGEXP %s
					ORDER BY CAST(handoff_id AS UNSIGNED)
				""",
				"policy_handoffs": """
					SELECT handoff_id FROM agent_handoffs
					WHERE from_agent = 'policy_agent' AND trace_id REGEXP %s
					ORDER BY CAST(handoff_id AS UNSIGNED)
				""",
				"policy_review_events": """
					SELECT policy_review_event_id FROM policy_review_events
					WHERE trace_id REGEXP %s
					ORDER BY created_at, policy_review_event_id
				""",
				"governance_events": """
					SELECT event_id FROM governance_events
					WHERE agent = 'policy_agent' AND trace_id REGEXP %s
					ORDER BY created_at, event_id
				""",
				"human_approvals": """
					SELECT approval_id FROM human_approvals
					WHERE approval_id LIKE 'POL-APP-%%' AND trace_id REGEXP %s
					ORDER BY created_at, approval_id
				""",
			}
			result: dict[str, list[Any]] = {}
			for name, query in queries.items():
				cursor.execute(query, (BENCHMARK_TRACE_PATTERN,))
				result[name] = [row[0] for row in cursor.fetchall()]
			cursor.execute(
				"""
				SELECT log_id, event_type FROM audit_log
				WHERE agent = 'policy_agent' AND trace_id REGEXP %s
				ORDER BY log_id
				""",
				(BENCHMARK_TRACE_PATTERN,),
			)
			result["audit_log"] = list(cursor.fetchall())
			return result
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
				WHERE p.from_agent = 'policy_agent' AND p.trace_id REGEXP %s
				GROUP BY
				  p.handoff_id, p.trace_id, p.ticket_id, p.to_agent,
				  p.input_json, p.output_json, p.input_tokens, p.output_tokens,
				  w.status, w.current_agent
				ORDER BY p.trace_id
				""",
				(BENCHMARK_TRACE_PATTERN,),
			)
			records = []
			for row in cursor.fetchall():
				row["input_json"] = json.loads(row["input_json"])
				row["output_json"] = json.loads(row["output_json"])
				for key in (
					"input_tokens",
					"output_tokens",
					"workflow_input_tokens",
					"workflow_output_tokens",
				):
					row[key] = int(row[key])
				records.append(CloudCaseRecord(**row))
			return records
		finally:
			connection.close()

	def fetch_review_records(self) -> dict[str, list[dict[str, Any]]]:
		connection = self._connect()
		try:
			cursor = connection.cursor(dictionary=True)
			cursor.execute(
				"""
				SELECT * FROM policy_review_events
				WHERE trace_id REGEXP %s
				ORDER BY policy_review_event_id
				""",
				(BENCHMARK_TRACE_PATTERN,),
			)
			policy = cursor.fetchall()
			for row in policy:
				row["policy_ids_json"] = json.loads(row["policy_ids_json"])
				row["evidence_json"] = json.loads(row["evidence_json"])
			cursor.execute(
				"""
				SELECT * FROM governance_events
				WHERE agent = 'policy_agent' AND trace_id REGEXP %s
				ORDER BY event_id
				""",
				(BENCHMARK_TRACE_PATTERN,),
			)
			governance = cursor.fetchall()
			for row in governance:
				row["flags_json"] = json.loads(row["flags_json"])
			cursor.execute(
				"""
				SELECT * FROM human_approvals
				WHERE approval_id LIKE 'POL-APP-%%' AND trace_id REGEXP %s
				ORDER BY approval_id
				""",
				(BENCHMARK_TRACE_PATTERN,),
			)
			approvals = cursor.fetchall()
			for row in approvals:
				row["notes"] = json.loads(row["notes"]) if row["notes"] else None
			return {
				"policy": policy,
				"governance": governance,
				"approvals": approvals,
			}
		finally:
			connection.close()

	def integrity_counts(self) -> dict[str, int]:
		connection = self._connect()
		try:
			cursor = connection.cursor()
			result: dict[str, int] = {}
			for table in (
				"audit_log",
				"governance_events",
				"policy_review_events",
				"human_approvals",
			):
				cursor.execute(
					f"""
					SELECT COUNT(*) FROM {table} child
					LEFT JOIN workflow_runs workflow ON workflow.trace_id = child.trace_id
					WHERE workflow.trace_id IS NULL
					"""
				)
				result[f"orphan_{table}"] = int(cursor.fetchone()[0])
			cursor.execute(
				"""
				SELECT COUNT(*) FROM human_approvals
				WHERE approval_id LIKE 'POL-APP-%%' AND trace_id REGEXP %s
				""",
				(BENCHMARK_TRACE_PATTERN,),
			)
			result["policy_agent_human_approvals"] = int(cursor.fetchone()[0])
			cursor.execute(
				"""
				SELECT COUNT(*)
				FROM human_approvals approvals
				LEFT JOIN governance_events governance
				  ON approvals.triggering_event_type = 'governance'
				 AND approvals.triggering_event_id = governance.event_id
				LEFT JOIN policy_review_events reviews
				  ON approvals.triggering_event_type = 'policy_review'
				 AND approvals.triggering_event_id = reviews.policy_review_event_id
				WHERE (approvals.triggering_event_type = 'governance'
				       AND governance.event_id IS NULL)
				   OR (approvals.triggering_event_type = 'policy_review'
				       AND reviews.policy_review_event_id IS NULL)
				   OR approvals.triggering_event_type NOT IN ('governance', 'policy_review')
				"""
			)
			result["orphan_human_approval_trigger"] = int(cursor.fetchone()[0])
			return result
		finally:
			connection.close()

	def record_workflow_failure(
		self,
		*,
		trace_id: str,
		ticket_id: str,
		error_type: str,
		error_message: str,
	) -> int:
		"""Atomically fail one seeded demo workflow and upsert its audit event."""

		_validate_demo_trace_id(trace_id)
		normalized_ticket = str(ticket_id or "").strip()
		normalized_type = (str(error_type or "").strip() or "Error")[:255]
		normalized_message = str(error_message or "").strip()[:4000]
		if not normalized_ticket or len(normalized_ticket) > 36:
			raise ValueError("ticket_id is required and must be at most 36 characters")
		payload = json.dumps(
			{
				"ticket_id": normalized_ticket,
				"error_type": normalized_type,
				"error_message": normalized_message,
			},
			ensure_ascii=False,
		)
		connection = self._connect()
		try:
			connection.start_transaction()
			cursor = connection.cursor()
			cursor.execute(
				"SELECT ticket_id FROM workflow_runs WHERE trace_id = %s FOR UPDATE",
				(trace_id,),
			)
			workflow = cursor.fetchone()
			if workflow is None:
				raise CloudDatabaseError(f"{trace_id}: workflow does not exist")
			if str(workflow[0]) != normalized_ticket:
				raise CloudDatabaseError(
					f"{trace_id}: ticket_id does not match the persisted workflow"
				)
			cursor.execute(
				"SELECT log_id FROM audit_log "
				"WHERE trace_id = %s AND event_type = 'workflow_failed' ORDER BY log_id FOR UPDATE",
				(trace_id,),
			)
			existing = [int(row[0]) for row in cursor.fetchall()]
			if len(existing) > 1:
				raise CloudDatabaseError(f"{trace_id}: multiple workflow_failed audit rows exist")
			if existing:
				log_id = existing[0]
				cursor.execute(
					"""
					UPDATE audit_log
					SET agent = 'workflow', payload_json = %s, created_at = CURRENT_TIMESTAMP
					WHERE log_id = %s AND trace_id = %s AND event_type = 'workflow_failed'
					""",
					(payload, log_id, trace_id),
				)
				if cursor.rowcount != 1:
					raise CloudDatabaseError(f"{trace_id}: workflow_failed audit changed during update")
			else:
				cursor.execute(
					"""
					INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
					VALUES (%s, 'workflow_failed', 'workflow', %s)
					""",
					(trace_id, payload),
				)
				log_id = int(cursor.lastrowid)
			cursor.execute(
				"""
				UPDATE workflow_runs
				SET status = 'failed', current_agent = 'failed',
				    completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
				WHERE trace_id = %s AND ticket_id = %s
				""",
				(trace_id, normalized_ticket),
			)
			_require_workflow_row(cursor, trace_id)
			connection.commit()
			return log_id
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

	def _upsert_handoff(
		self,
		cursor: Any,
		policy_input: PolicyAgentInput,
		output: PolicyAgentOutput,
		usage: TokenUsage,
		*,
		followup_claim_token: str | None = None,
	) -> str:
		if followup_claim_token is None:
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
			handoff_id = existing[0] if existing else self._next_handoff_id(
				policy_input.case.trace_id,
				"policy_agent",
			)
		else:
			handoff_id = self._next_handoff_id(
				policy_input.case.trace_id,
				"policy_agent",
				followup_claim_token=followup_claim_token,
			)
		input_payload = _with_continuation_marker(
			policy_input.model_dump(mode="json"),
			followup_claim_token,
		)
		output_payload = _with_continuation_marker(
			output.model_dump(mode="json"),
			followup_claim_token,
		)
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
				json.dumps(input_payload, ensure_ascii=False),
				json.dumps(output_payload, ensure_ascii=False),
				usage.input_tokens,
				usage.output_tokens,
			),
		)
		return handoff_id

	def _upsert_generic_handoff(
		self,
		cursor: Any,
		*,
		trace_id: str,
		ticket_id: str,
		from_agent: str,
		to_agent: str,
		input_payload: dict[str, Any],
		output_payload: dict[str, Any],
		input_tokens: int,
		output_tokens: int,
		followup_claim_token: str | None = None,
	) -> str:
		if followup_claim_token is None:
			cursor.execute(
				"""
				SELECT handoff_id FROM agent_handoffs
				WHERE trace_id = %s AND ticket_id = %s AND from_agent = %s
				ORDER BY created_at DESC
				""",
				(trace_id, ticket_id, from_agent),
			)
			existing = [row[0] for row in cursor.fetchall()]
			if len(existing) > 1:
				raise CloudDatabaseError(f"{trace_id}: multiple {from_agent} handoffs already exist")
			handoff_id = existing[0] if existing else self._next_handoff_id(trace_id, from_agent)
		else:
			handoff_id = self._next_handoff_id(
				trace_id,
				from_agent,
				followup_claim_token=followup_claim_token,
			)
		marked_input = _with_continuation_marker(input_payload, followup_claim_token)
		marked_output = _with_continuation_marker(output_payload, followup_claim_token)
		cursor.execute(
			"""
			INSERT INTO agent_handoffs (
			  handoff_id, trace_id, ticket_id, from_agent, to_agent,
			  input_json, output_json, input_tokens, output_tokens
			)
			VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
			ON DUPLICATE KEY UPDATE
			  to_agent = VALUES(to_agent), input_json = VALUES(input_json),
			  output_json = VALUES(output_json), input_tokens = VALUES(input_tokens),
			  output_tokens = VALUES(output_tokens), created_at = CURRENT_TIMESTAMP
			""",
			(
				handoff_id,
				trace_id,
				ticket_id,
				from_agent,
				to_agent,
				json.dumps(marked_input, ensure_ascii=False),
				json.dumps(marked_output, ensure_ascii=False),
				input_tokens,
				output_tokens,
			),
		)
		return handoff_id

	def _persist_policy_review(
		self,
		cursor: Any,
		output: PolicyAgentOutput,
		*,
		followup_claim_token: str | None = None,
	) -> str | None:
		existing: list[str] = []
		if followup_claim_token is None:
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
		event_id = existing[0] if existing else self._next_prefixed_id(
			cursor,
			"policy_review_events",
			"policy_review_event_id",
			"POL-REV-",
			(
				output.case.trace_id
				if followup_claim_token is None
				else f"{output.case.trace_id}:customer_followup:{followup_claim_token}"
			),
		)
		gaps = output.policy_evaluation.gaps_or_conflicts
		review_type = "low_confidence" if any(gap.type == "low_confidence" for gap in gaps) else "policy_rule"
		review_policies = [policy.policy_id for policy in output.policy_evaluation.matched_policies if policy.effect == "requires_review"]
		if not review_policies:
			review_policies = [policy.policy_id for policy in output.policy_evaluation.matched_policies]
		evidence = {
			"matched_policies": [policy.model_dump(mode="json") for policy in output.policy_evaluation.matched_policies],
			"gaps_or_conflicts": [gap.model_dump(mode="json") for gap in gaps],
		}
		evidence = _with_continuation_marker(evidence, followup_claim_token)
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

	def _persist_governance(
		self,
		cursor: Any,
		output: PolicyAgentOutput,
		findings: list[GovernanceFinding],
		*,
		followup_claim_token: str | None = None,
	) -> list[str]:
		existing: list[str] = []
		if followup_claim_token is None:
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
			identity = f"{output.case.trace_id}:{index}:{finding.flag}"
			if followup_claim_token is not None:
				identity += f":customer_followup:{followup_claim_token}"
			event_id = existing[index] if index < len(existing) else self._next_prefixed_id(
				cursor,
				"governance_events",
				"event_id",
				"POL-GOV-",
				identity,
			)
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
					json.dumps(
						_with_continuation_marker(
							{"finding": finding.model_dump(mode="json"), "governance": output.governance.model_dump(mode="json")},
							followup_claim_token,
						),
						ensure_ascii=False,
					),
					finding.offending_content,
				),
			)
		return event_ids

	def _persist_human_approval(
		self,
		cursor: Any,
		output: PolicyAgentOutput,
		policy_event_id: str | None,
		governance_event_ids: list[str],
	) -> None:
		self._assert_policy_approval_history_mutable(cursor, output.case.trace_id)
		cursor.execute(
			"SELECT approval_id, status FROM human_approvals "
			"WHERE trace_id = %s AND approval_id LIKE 'POL-APP-%%' "
			"ORDER BY created_at FOR UPDATE",
			(output.case.trace_id,),
		)
		existing_rows = list(cursor.fetchall())
		pending_rows = [row for row in existing_rows if row[1] == "pending"]
		if output.handoff.next_agent != "human_approval":
			cursor.execute(
				"DELETE FROM human_approvals WHERE trace_id = %s "
				"AND approval_id LIKE 'POL-APP-%%' AND status = 'pending'",
				(output.case.trace_id,),
			)
			return
		approval_id = pending_rows[0][0] if pending_rows else self._next_prefixed_id(
			cursor,
			"human_approvals",
			"approval_id",
			"POL-APP-",
			output.case.trace_id,
		)
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
			  decision = NULL, resolved_amount = NULL, reviewer = NULL,
			  resolved_at = NULL, updated_at = CURRENT_TIMESTAMP
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
	def _assert_policy_approval_history_mutable(cursor: Any, trace_id: str) -> None:
		cursor.execute(
			"SELECT approval_id, status FROM human_approvals "
			"WHERE trace_id = %s AND approval_id LIKE 'POL-APP-%%' "
			"ORDER BY created_at FOR UPDATE",
			(trace_id,),
		)
		existing_rows = list(cursor.fetchall())
		if any(row[1] in {"approved", "rejected"} for row in existing_rows):
			raise HumanApprovalConflictError(
				f"{trace_id}: resolved Policy approval history prevents an unsafe Policy rerun"
			)
		if sum(row[1] == "pending" for row in existing_rows) > 1:
			raise HumanApprovalConflictError(
				f"{trace_id}: multiple pending Policy approvals exist"
			)

	@staticmethod
	def _single_prefixed_id(cursor: Any, query: str, trace_id: str, label: str) -> str | None:
		cursor.execute(query, (trace_id,))
		existing = [row[0] for row in cursor.fetchall()]
		if len(existing) > 1:
			raise CloudDatabaseError(f"{trace_id}: multiple {label} rows already exist")
		return existing[0] if existing else None

	@staticmethod
	def _next_handoff_id(
		trace_id: str,
		from_agent: str,
		*,
		followup_claim_token: str | None = None,
	) -> str:
		identity = f"idox-handoff:{trace_id}:{from_agent}"
		if followup_claim_token is not None:
			identity += f":customer_followup:{followup_claim_token}"
		return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))

	@staticmethod
	def _next_prefixed_id(
		cursor: Any,
		table: str,
		column: str,
		prefix: str,
		identity: str,
	) -> str:
		allowed = {
			("policy_review_events", "policy_review_event_id", "POL-REV-"),
			("governance_events", "event_id", "POL-GOV-"),
			("human_approvals", "approval_id", "POL-APP-"),
			("governance_events", "event_id", "GOV-STM-"),
		}
		if (table, column, prefix) not in allowed:
			raise ValueError("Unsupported sequential ID target")
		digest = uuid.uuid5(uuid.NAMESPACE_URL, f"idox:{table}:{identity}").hex
		return prefix + digest[: 36 - len(prefix)]

	def _table_exists(self, cursor: Any, table: str) -> bool:
		cursor.execute(
			"SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
			(self.database_name, table),
		)
		return bool(cursor.fetchone()[0])

	def _table_columns(self, cursor: Any, table: str) -> set[str]:
		cursor.execute(
			"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
			(self.database_name, table),
		)
		return {row[0] for row in cursor.fetchall()}

	def _constraint_exists(self, cursor: Any, table: str, constraint: str) -> bool:
		cursor.execute(
			"""
			SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
			WHERE CONSTRAINT_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = %s
			""",
			(self.database_name, table, constraint),
		)
		return bool(cursor.fetchone()[0])

	def _connect(self):
		last_error: mysql.connector.Error | None = None
		for attempt in range(1, MYSQL_CONNECT_ATTEMPTS + 1):
			try:
				return mysql.connector.connect(**self.connection_config)
			except mysql.connector.Error as error:
				last_error = error
				if error.errno not in TRANSIENT_MYSQL_ERRORS or attempt == MYSQL_CONNECT_ATTEMPTS:
					break
				time.sleep(attempt)
		raise CloudDatabaseError(
			f"Could not connect to GCP MySQL {self.database_name} after {attempt} attempt(s): {last_error}"
		) from last_error


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


def _validate_demo_trace_id(trace_id: str) -> str:
	value = str(trace_id or "").strip()
	if not DEMO_TRACE_PATTERN.fullmatch(value):
		raise ValueError("trace_id must be one of demo01 through demo20")
	return value


def _with_continuation_marker(
	payload: dict[str, Any],
	claim_token: str | None,
) -> dict[str, Any]:
	"""Return a JSON payload whose resumed customer turn is queryable."""

	result = dict(payload)
	if claim_token is not None:
		result["_continuation"] = {
			"type": "customer_followup",
			"claim_token": claim_token,
		}
	return result


def _require_active_followup_claim(
	cursor: Any,
	*,
	trace_id: str,
	claim_token: str | None,
) -> None:
	"""Fence every continuation write against the newest durable lease."""

	if claim_token is None:
		return
	_validate_demo_trace_id(trace_id)
	normalized_token = str(claim_token or "").strip()
	if not normalized_token or len(normalized_token) > 64:
		raise CloudDatabaseError(f"{trace_id}: invalid customer follow-up claim token")
	cursor.execute(
		"SELECT trace_id, status, current_agent FROM workflow_runs "
		"WHERE trace_id = %s FOR UPDATE",
		(trace_id,),
	)
	workflow = cursor.fetchone()
	if workflow is None:
		raise CloudDatabaseError(f"{trace_id}: workflow does not exist")
	cursor.execute(
		"SELECT payload_json FROM audit_log "
		"WHERE trace_id = %s AND event_type = 'customer_followup_claimed' "
		"ORDER BY log_id DESC LIMIT 1 FOR UPDATE",
		(trace_id,),
	)
	row = cursor.fetchone()
	if row is None:
		raise CloudDatabaseError(f"{trace_id}: customer follow-up claim is missing")
	payload = _json_object(row[0] if not isinstance(row, dict) else row.get("payload_json"))
	if payload.get("claim_token") != normalized_token:
		raise CloudDatabaseError(f"{trace_id}: stale customer follow-up claim token")
	cursor.execute(
		"SELECT payload_json FROM audit_log "
		"WHERE trace_id = %s AND event_type = 'customer_followup_failed' "
		"ORDER BY log_id DESC LIMIT 1 FOR UPDATE",
		(trace_id,),
	)
	failure_row = cursor.fetchone()
	if failure_row is not None:
		failure_payload = _json_object(
			failure_row[0]
			if not isinstance(failure_row, dict)
			else failure_row.get("payload_json")
		)
		if failure_payload.get("claim_token") == normalized_token:
			raise CloudDatabaseError(
				f"{trace_id}: failed customer follow-up claim token is revoked"
			)
	workflow_status = (
		workflow.get("status") if isinstance(workflow, dict) else workflow[1]
	)
	if workflow_status != "running":
		raise CloudDatabaseError(
			f"{trace_id}: customer follow-up claim is not active"
		)
	cursor.execute(
		"SELECT log_id FROM audit_log "
		"WHERE trace_id = %s AND event_type = 'customer_followup_completed' "
		"ORDER BY log_id FOR UPDATE",
		(trace_id,),
	)
	if cursor.fetchone() is not None:
		raise CloudDatabaseError(f"{trace_id}: customer follow-up is already completed")


def _validate_review_request(
	*,
	decision: str,
	resolved_amount: float | Decimal | None,
	reviewer: str,
	notes: str,
) -> tuple[str, Decimal | None, str, str]:
	normalized_decision = str(decision or "").strip().lower()
	if normalized_decision not in {"approve", "partial_refund", "deny"}:
		raise ValueError("decision must be approve, partial_refund, or deny")
	normalized_reviewer = str(reviewer or "").strip()
	if not normalized_reviewer or len(normalized_reviewer) > 255:
		raise ValueError("reviewer is required and must be at most 255 characters")
	normalized_notes = str(notes or "").strip()
	if not normalized_notes or len(normalized_notes) > 4000:
		raise ValueError("notes are required and must be at most 4000 characters")
	return (
		normalized_decision,
		_optional_decimal(resolved_amount),
		normalized_reviewer,
		normalized_notes,
	)


def _optional_decimal(value: Any) -> Decimal | None:
	if value is None or value == "":
		return None
	try:
		amount = Decimal(str(value))
	except (InvalidOperation, ValueError) as error:
		raise ValueError("resolved_amount must be a finite monetary value") from error
	if not amount.is_finite():
		raise ValueError("resolved_amount must be a finite monetary value")
	if amount != amount.quantize(Decimal("0.01")):
		raise ValueError("resolved_amount must have at most two decimal places")
	return amount.quantize(Decimal("0.01"))


def _validate_review_amount(
	*,
	trace_id: str,
	decision: str,
	resolved_amount: Decimal | None,
	next_agent: str,
	amount_requested: Decimal | None,
	order_lookup: dict[str, Any],
) -> Decimal | None:
	if decision == "deny":
		if resolved_amount not in {None, Decimal("0.00")}:
			raise ValueError("deny decisions cannot include a resolved refund amount")
		return None
	if next_agent != "refund_agent":
		if resolved_amount not in {None, Decimal("0.00")}:
			raise ValueError("non-refund continuations cannot include a resolved refund amount")
		return None
	if resolved_amount is None or resolved_amount <= 0:
		raise ValueError("approved refund continuations require a positive resolved_amount")
	amount_paid = _optional_decimal(order_lookup.get("amount_paid")) or Decimal("0.00")
	prior_refund = _optional_decimal(order_lookup.get("prior_refund_total")) or Decimal("0.00")
	remaining = max(Decimal("0.00"), amount_paid - prior_refund)
	if resolved_amount > remaining:
		raise ValueError(
			f"{trace_id}: resolved_amount exceeds the order's remaining refundable amount {remaining:.2f}"
		)
	if amount_requested is not None and resolved_amount > amount_requested:
		raise ValueError(f"{trace_id}: resolved_amount exceeds the requested amount")
	if decision == "partial_refund" and amount_requested is not None and resolved_amount >= amount_requested:
		raise ValueError("partial_refund must be less than the requested amount")
	return resolved_amount


def _decimal_as_float(value: Decimal | None) -> float | None:
	return float(value) if value is not None else None


def _json_object(value: Any) -> dict[str, Any]:
	if isinstance(value, dict):
		return value
	if not value:
		return {}
	try:
		parsed = json.loads(value)
	except (TypeError, ValueError):
		return {}
	return parsed if isinstance(parsed, dict) else {}


def _json_scalar(value: Any) -> Any:
	if isinstance(value, Decimal):
		return float(value)
	if hasattr(value, "isoformat"):
		return value.isoformat()
	return value


def _reconstruct_review_state(cursor: Any, approval_row: dict[str, Any]) -> dict[str, Any]:
	"""Rebuild only the durable parent state needed by Refund and Response."""

	trace_id = str(approval_row["trace_id"])
	cursor.execute(
		"""
		SELECT handoff_id, from_agent, to_agent, input_json, output_json
		FROM agent_handoffs
		WHERE trace_id = %s
		ORDER BY created_at, handoff_id
		""",
		(trace_id,),
	)
	handoffs = list(cursor.fetchall())
	preferred_order_ids: list[str] = []
	triage_output: dict[str, Any] = {}
	policy_decision: dict[str, Any] = {}
	response_result: dict[str, Any] = {}
	requested_order_id = ""
	for handoff in handoffs:
		input_payload = _json_object(handoff.get("input_json"))
		output_payload = _json_object(handoff.get("output_json"))
		candidate = input_payload.get("requested_order_id")
		if isinstance(candidate, str) and candidate:
			preferred_order_ids.append(candidate)
			requested_order_id = candidate
		if handoff.get("from_agent") == "triage_agent":
			projected = output_payload.get("triage_output")
			if isinstance(projected, dict):
				triage_output = projected
				order_facts = projected.get("order_facts")
				if isinstance(order_facts, dict) and order_facts.get("order_id"):
					preferred_order_ids.append(str(order_facts["order_id"]))
		if handoff.get("from_agent") == "policy_agent":
			projected = output_payload.get("policy_decision") or output_payload.get("decision")
			if isinstance(projected, dict):
				policy_decision = dict(projected)
				if "decision" not in policy_decision and policy_decision.get("type"):
					policy_decision["decision"] = policy_decision["type"]
		if handoff.get("from_agent") == "response_agent":
			projected = output_payload.get("response_result")
			if isinstance(projected, dict):
				response_result = projected

	cursor.execute(
		"""
		SELECT orders.order_id, orders.customer_id AS order_customer_id,
		       orders.product_type, orders.purchase_date, orders.item_status,
		       orders.amount_paid, orders.prior_refund_total, orders.currency,
		       customers.customer_id AS contact_customer_id,
		       customers.email AS contact_email, customers.full_name AS contact_name
		FROM orders
		JOIN customers ON customers.customer_id = orders.customer_id
		WHERE orders.customer_id = %s
		ORDER BY orders.created_at, orders.order_id
		""",
		(approval_row["customer_id"],),
	)
	orders = list(cursor.fetchall())
	if not orders:
		raise HumanApprovalStateError(f"{trace_id}: review continuation has no customer order")
	orders_by_id = {str(order["order_id"]): order for order in orders}
	selected_order = next(
		(orders_by_id[order_id] for order_id in reversed(preferred_order_ids) if order_id in orders_by_id),
		None,
	)
	if selected_order is None:
		if len(orders) != 1:
			raise HumanApprovalStateError(
				f"{trace_id}: review continuation cannot choose among customer orders"
			)
		selected_order = orders[0]
	requested_order_id = str(selected_order["order_id"])
	order_lookup = {key: _json_scalar(value) for key, value in selected_order.items()}
	if not policy_decision:
		policy_decision = {
			"decision": "manual_review",
			"refund_amount": _json_scalar(approval_row.get("requested_amount")) or 0,
			"reason": str(approval_row.get("reason") or "Human review required."),
		}
	message = str(approval_row.get("sanitized_text") or approval_row.get("raw_text") or "")
	return {
		"trace_id": trace_id,
		"ticket_id": str(approval_row["ticket_id"]),
		"user_id": str(approval_row["customer_id"]),
		"message": message,
		"request_context": {"selected_order_id": requested_order_id},
		"requested_order_id": requested_order_id,
		"order_lookup_result": order_lookup,
		"triage_output": triage_output,
		"policy_decision": policy_decision,
		"response_result": response_result,
		"review_trigger_stage": _review_trigger_stage(approval_row),
		"review_trigger_reason": str(approval_row.get("reason") or "human_review"),
		"workflow_status": str(approval_row.get("workflow_status") or "pending_human"),
		"current_stage": str(approval_row.get("current_agent") or "human_approval"),
		"llm_input_tokens": 0,
		"llm_output_tokens": 0,
		"llm_usage_events": [],
	}


def _review_trigger_stage(row: dict[str, Any]) -> str:
	agent = str(row.get("governance_agent") or "")
	if agent.startswith("triage"):
		return "triage"
	if agent.startswith("response"):
		return "response"
	return "policy"


def _apply_review_resolution_to_state(
	state: dict[str, Any],
	*,
	approval_id: str,
	status: str,
	decision: str,
	resolved_amount: Decimal | None,
	reviewer: str,
	notes: str,
	next_agent: str,
) -> dict[str, Any]:
	result = dict(state)
	policy_decision = dict(result.get("policy_decision") or {})
	if next_agent == "refund_agent" or status == "rejected":
		policy_decision.update(
			{
				"decision": decision,
				"type": decision,
				"refund_amount": _decimal_as_float(resolved_amount) or 0,
				"reason": notes,
			}
		)
	resolved_outcome = (
		"denied"
		if status == "rejected"
		else "partial_refund" if decision == "partial_refund" else "approved"
	)
	if next_agent == "end":
		persisted_response = result.get("response_result") or {}
		resolved_outcome = str(persisted_response.get("final_outcome") or resolved_outcome)
	result.update(
		{
			"current_stage": "human_approval",
			"workflow_status": "completed" if next_agent == "end" else "running",
			"policy_decision": policy_decision,
			"human_review_required": False,
			"human_review": {
				"approval_id": approval_id,
				"status": status,
				"decision": decision,
				"resolved_amount": _decimal_as_float(resolved_amount),
				"reviewer": reviewer,
				"notes": notes,
				"approved_next_agent": next_agent if status == "approved" else None,
				"rejected_next_agent": next_agent if status == "rejected" else None,
			},
			"final_outcome": resolved_outcome,
		}
	)
	return result


def _approval_continuation_complete(cursor: Any, *, trace_id: str, approval_id: str) -> bool:
	return _approval_continuation_record(
		cursor,
		trace_id=trace_id,
		approval_id=approval_id,
	) is not None


def _approval_continuation_claim(
	cursor: Any,
	*,
	trace_id: str,
	approval_id: str,
) -> dict[str, Any] | None:
	cursor.execute(
		"""
		SELECT log_id, payload_json, created_at,
		       TIMESTAMPDIFF(SECOND, created_at, CURRENT_TIMESTAMP) AS age_seconds
		FROM audit_log
		WHERE trace_id = %s AND event_type = 'human_approval_continuation_claimed'
		ORDER BY log_id DESC
		LIMIT 1
		""",
		(trace_id,),
	)
	row = cursor.fetchone()
	if row is None:
		return None
	payload_value = row.get("payload_json") if isinstance(row, dict) else row[1]
	payload = _json_object(payload_value)
	if payload.get("approval_id") != approval_id:
		return None
	if isinstance(row, dict):
		return {
			"log_id": row["log_id"],
			"age_seconds": row.get("age_seconds"),
			"payload": payload,
		}
	return {"log_id": row[0], "age_seconds": row[3], "payload": payload}


def _upsert_approval_continuation_claim(
	cursor: Any,
	*,
	trace_id: str,
	approval_id: str,
	next_agent: str,
	existing_claim: dict[str, Any] | None,
) -> None:
	attempt = int(((existing_claim or {}).get("payload") or {}).get("attempt") or 0) + 1
	payload = json.dumps(
		{
			"approval_id": approval_id,
			"next_agent": next_agent,
			"attempt": attempt,
		},
		ensure_ascii=False,
	)
	if existing_claim is None:
		cursor.execute(
			"""
			INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
			VALUES (%s, 'human_approval_continuation_claimed', 'human_approval', %s)
			""",
			(trace_id, payload),
		)
		return
	cursor.execute(
		"""
		UPDATE audit_log
		SET payload_json = %s, created_at = CURRENT_TIMESTAMP
		WHERE log_id = %s AND trace_id = %s
		  AND event_type = 'human_approval_continuation_claimed'
		""",
		(payload, existing_claim["log_id"], trace_id),
	)
	if cursor.rowcount != 1:
		raise HumanApprovalConflictError(
			f"{trace_id}: continuation claim changed while it was being refreshed"
		)


def _approval_continuation_record(
	cursor: Any,
	*,
	trace_id: str,
	approval_id: str,
) -> dict[str, Any] | None:
	cursor.execute(
		"""
		SELECT payload_json FROM audit_log
		WHERE trace_id = %s AND event_type = 'human_approval_continued'
		ORDER BY log_id
		""",
		(trace_id,),
	)
	for row in cursor.fetchall():
		payload_value = row.get("payload_json") if isinstance(row, dict) else row[0]
		payload = _json_object(payload_value)
		if payload.get("approval_id") == approval_id:
			return payload
	return None


def _workflow_state(output: PolicyAgentOutput) -> tuple[str, str]:
	if output.handoff.next_agent == "human_approval":
		return "pending_human", "human_approval"
	if output.handoff.next_agent == "refund_agent":
		return "running", "refund_agent"
	return "running", output.handoff.next_agent


def _database_workflow_status(runtime_status: str) -> str:
	"""Translate AppState vocabulary to the relational workflow enum."""

	status = "pending_human" if runtime_status == "waiting_human" else runtime_status
	allowed = {
		"running",
		"waiting_user",
		"paused_governance",
		"pending_human",
		"completed",
		"failed",
	}
	if status not in allowed:
		raise CloudDatabaseError(f"Unsupported workflow status: {runtime_status}")
	return status


def _approved_next_agent(output: PolicyAgentOutput) -> str:
	if output.decision.type in {"approve", "partial_refund", "manual_review"}:
		return "refund_agent"
	return "response_agent"


def _approval_trigger(trace_id: str, policy_event_id: str | None, governance_event_ids: list[str]) -> tuple[str, str]:
	if governance_event_ids:
		return "governance", governance_event_ids[0]
	if policy_event_id:
		return "policy_review", policy_event_id
	raise CloudDatabaseError(f"{trace_id}: human approval requires a governance or policy review event")


def _require_workflow_row(cursor: Any, trace_id: str) -> None:
	"""Accept an idempotent update while still failing if the workflow is absent."""

	if cursor.rowcount == 1:
		return
	if cursor.rowcount != 0:
		raise CloudDatabaseError(f"{trace_id}: workflow_runs update affected {cursor.rowcount} rows")
	cursor.execute("SELECT COUNT(*) FROM workflow_runs WHERE trace_id = %s", (trace_id,))
	count_row = cursor.fetchone()
	count_value = next(iter(count_row.values())) if isinstance(count_row, dict) else count_row[0]
	if int(count_value) != 1:
		raise CloudDatabaseError(f"{trace_id}: workflow_runs row was not updated")


def _statement_owasp_category(statement: GovernanceStatement) -> str:
	if statement.findings:
		return OWASP_BY_FLAG[statement.findings[0].flag]
	return "ASI00"


def _statement_trigger_score(statement: GovernanceStatement) -> float | None:
	if not statement.findings:
		return None
	return statement.findings[0].score


def _statement_flags_payload(statement: GovernanceStatement) -> dict[str, Any]:
	payload: dict[str, Any] = {
		"summary": statement.summary,
		"stage": statement.stage,
	}
	if statement.findings:
		payload["finding"] = statement.findings[0].model_dump(mode="json")
		payload["governance"] = {
			"interceptor_action": "block" if statement.status == "block" else "allow",
			"flags": [finding.flag for finding in statement.findings],
		}
	return payload


def _statement_offending_content(statement: GovernanceStatement) -> str | None:
	for finding in statement.findings:
		if finding.offending_content:
			return finding.offending_content
	return None


def _statement_summary_from_payload(agent: str, status: str, payload: dict[str, Any]) -> str:
	if isinstance(payload, dict) and isinstance(payload.get("summary"), str) and payload["summary"]:
		return payload["summary"]
	return f"{agent} governance {'blocked' if status == 'block' else 'passed'}"


def _migration_statements(path: Path, expected: int) -> list[str]:
	statements = [statement.strip() for statement in path.read_text(encoding="utf-8").split(";") if statement.strip()]
	if len(statements) != expected:
		raise CloudDatabaseError(f"{path.name} must contain exactly {expected} SQL statements")
	return statements
