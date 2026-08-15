"""Crash-safe persistence for the two customer follow-up continuations."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from db.database import DEFAULT_CONTINUATION_LEASE_SECONDS
from demo.catalog import DEMO_IDS, FINAL_DATABASE, DemoCase


_RECEIVED = "customer_followup_received"
_CLAIMED = "customer_followup_claimed"
_FAILED = "customer_followup_failed"
_COMPLETED = "customer_followup_completed"
_LIFECYCLE_EVENTS = (_RECEIVED, _CLAIMED, _FAILED, _COMPLETED)

_SNAPSHOT_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "agent_handoffs": (
        "handoff_id",
        (
            "handoff_id", "trace_id", "ticket_id", "from_agent", "to_agent",
            "input_json", "output_json", "input_tokens", "output_tokens", "created_at",
        ),
    ),
    "audit_log": (
        "log_id",
        ("log_id", "trace_id", "event_type", "agent", "payload_json", "created_at"),
    ),
    "governance_events": (
        "event_id",
        (
            "event_id", "trace_id", "agent", "owasp_category", "trigger_score",
            "interceptor_action", "flags_json", "offending_content", "created_at",
        ),
    ),
    "policy_review_events": (
        "policy_review_event_id",
        (
            "policy_review_event_id", "trace_id", "policy_version", "review_type",
            "policy_ids_json", "evidence_json", "detail", "created_at",
        ),
    ),
    "human_approvals": (
        "approval_id",
        (
            "approval_id", "trace_id", "triggering_event_id", "triggering_event_type",
            "reason", "amount_requested", "resolved_amount", "status", "decision",
            "approved_next_agent", "rejected_next_agent", "reviewer", "notes",
            "resolved_at", "created_at", "updated_at",
        ),
    ),
    "refund_transactions": (
        "transaction_id",
        (
            "transaction_id", "trace_id", "approval_id", "amount", "currency",
            "status", "external_ref", "created_at", "updated_at",
        ),
    ),
}


class CustomerFollowupStoreError(RuntimeError):
    """Persisted data cannot support a safe customer follow-up."""


class CustomerFollowupConflictError(CustomerFollowupStoreError):
    """A request conflicts with the immutable history or active lease."""


@dataclass(frozen=True)
class CustomerFollowupClaim:
    receipt_log_id: int
    claim_token: str | None = None
    assistant_response: str | None = None
    completed_result: dict[str, Any] | None = None
    recovered: bool = False

    @property
    def idempotent(self) -> bool:
        return self.completed_result is not None


class CustomerFollowupStore:
    """Own immutable history, leased claims, cleanup, and terminal proof."""

    def __init__(
        self,
        repository: Any,
        *,
        lease_seconds: int = DEFAULT_CONTINUATION_LEASE_SECONDS,
    ) -> None:
        self.repository = repository
        if str(getattr(repository, "database_name", "")) != FINAL_DATABASE:
            raise CustomerFollowupStoreError(
                "Customer follow-up requires the configured database 'final'"
            )
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        self.lease_seconds = lease_seconds

    def claim(self, case: DemoCase) -> CustomerFollowupClaim:
        request = _canonical_request(case)
        connection = self.repository._connect()
        cursor = None
        try:
            connection.start_transaction()
            cursor = connection.cursor(dictionary=True)
            self._require_exact_workflow_roots(cursor)
            workflow = self._workflow_identity(cursor, case, for_update=True)
            receipt = self._single_audit(cursor, case.trace_id, _RECEIVED)
            completion = self._single_audit(cursor, case.trace_id, _COMPLETED)

            receipt_payload: dict[str, Any] | None = None
            if receipt is not None:
                receipt_payload = _json_object(receipt.get("payload_json"), "receipt")
                self._validate_receipt(case, request, receipt_payload)
            if completion is not None:
                if receipt is None:
                    raise CustomerFollowupStoreError(
                        f"{case.trace_id}: completion exists without a customer receipt"
                    )
                completed = self._completed_result(completion, request, case.trace_id)
                connection.commit()
                return CustomerFollowupClaim(
                    receipt_log_id=int(receipt["log_id"]),
                    assistant_response=_snapshot_assistant_response(receipt_payload),
                    completed_result=completed,
                )

            latest_claim = self._latest_audit(cursor, case.trace_id, _CLAIMED)
            latest_failure = self._latest_audit(cursor, case.trace_id, _FAILED)
            if receipt is not None and latest_claim is None:
                raise CustomerFollowupStoreError(
                    f"{case.trace_id}: receipt exists without a continuation claim"
                )

            # A graph can commit all stage writes and crash before the final
            # marker. Recover from those durable rows without another Azure run.
            if (
                receipt is not None
                and latest_claim is not None
                and workflow.get("status") == "completed"
                and workflow.get("current_agent") == "completed"
            ):
                token = _claim_token(latest_claim, case.trace_id)
                try:
                    recovered, persistence = self._reconstruct_completed_result(
                        cursor,
                        case,
                        receipt_payload,
                        claim_token=token,
                    )
                except CustomerFollowupConflictError:
                    # Immutable receipt and canonical-root conflicts are never
                    # made recoverable by the passage of time.
                    raise
                except CustomerFollowupStoreError as error:
                    age_seconds = int(latest_claim.get("age_seconds") or 0)
                    if age_seconds < self.lease_seconds:
                        raise CustomerFollowupConflictError(
                            f"{case.trace_id}: completed continuation proof is "
                            "still within its active lease"
                        ) from error
                    # A crashed worker can leave a completed workflow whose
                    # stage proof is incomplete. Once its heartbeat expires,
                    # fall through to the normal snapshot restore/reclaim path.
                else:
                    stored = self._insert_completion(
                        cursor,
                        case,
                        request,
                        recovered,
                        persistence,
                        claim_token=token,
                        recovered=True,
                        receipt_log_id=int(receipt["log_id"]),
                    )
                    connection.commit()
                    return CustomerFollowupClaim(
                        receipt_log_id=int(receipt["log_id"]),
                        assistant_response=_snapshot_assistant_response(receipt_payload),
                        completed_result=stored,
                        recovered=True,
                    )

            if receipt is None:
                if (
                    workflow.get("status") != "waiting_user"
                    or workflow.get("current_agent") != "triage_agent"
                ):
                    raise CustomerFollowupConflictError(
                        f"{case.trace_id}: workflow must be waiting_user at triage_agent"
                    )
                history = self._snapshot_initial_history(cursor, case, workflow)
                receipt_payload = {
                    "version": 2,
                    "request": request,
                    "history": history,
                    "history_sha256": _snapshot_digest(history),
                }
                handoff_id = self._insert_customer_handoff(cursor, case, request)
                receipt_payload["handoff_id"] = handoff_id
                cursor.execute(
                    "INSERT INTO audit_log (trace_id, event_type, agent, payload_json) "
                    "VALUES (%s, %s, 'customer', %s)",
                    (
                        case.trace_id,
                        _RECEIVED,
                        json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                receipt_log_id = int(cursor.lastrowid)
                previous_attempt = 0
            else:
                receipt_log_id = int(receipt["log_id"])
                claim_payload = _json_object(latest_claim.get("payload_json"), "claim")
                previous_attempt = int(claim_payload.get("attempt") or 0)
                latest_token = _claim_token(latest_claim, case.trace_id)
                failure_token = (
                    _json_object(latest_failure.get("payload_json"), "failure").get("claim_token")
                    if latest_failure is not None
                    else None
                )
                failed_attempt = failure_token == latest_token
                age_seconds = int(latest_claim.get("age_seconds") or 0)
                if not failed_attempt and age_seconds < self.lease_seconds:
                    raise CustomerFollowupConflictError(
                        f"{case.trace_id}: customer follow-up is already in progress"
                    )
                self._restore_snapshot(cursor, case, receipt_payload)
                workflow = self._workflow_identity(cursor, case, for_update=True)
                if workflow.get("status") != "waiting_user":
                    raise CustomerFollowupStoreError(
                        f"{case.trace_id}: history restoration did not return waiting_user"
                    )

            claim_token = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO audit_log (trace_id, event_type, agent, payload_json) "
                "VALUES (%s, %s, 'workflow', %s)",
                (
                    case.trace_id,
                    _CLAIMED,
                    json.dumps(
                        {
                            "version": 1,
                            "claim_token": claim_token,
                            "attempt": previous_attempt + 1,
                            "lease_seconds": self.lease_seconds,
                            "request_sha256": _request_digest(request),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
            cursor.execute(
                "UPDATE workflow_runs SET status = 'running', current_agent = 'triage_agent', "
                "completed_at = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE trace_id = %s AND ticket_id = %s "
                "AND status = 'waiting_user' AND current_agent = 'triage_agent'",
                (case.trace_id, case.ticket_id),
            )
            if cursor.rowcount != 1:
                raise CustomerFollowupConflictError(
                    f"{case.trace_id}: waiting workflow changed while follow-up was claimed"
                )
            connection.commit()
            return CustomerFollowupClaim(
                receipt_log_id=receipt_log_id,
                claim_token=claim_token,
                assistant_response=_snapshot_assistant_response(receipt_payload),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()

    def heartbeat(self, case: DemoCase, claim_token: str) -> bool:
        """Refresh an active graph lease; return false once the graph is terminal."""

        connection = self.repository._connect()
        cursor = None
        try:
            connection.start_transaction()
            cursor = connection.cursor(dictionary=True)
            workflow = self._workflow_identity(cursor, case, for_update=True)
            self._require_current_token(cursor, case.trace_id, claim_token)
            if workflow.get("status") != "running":
                connection.commit()
                return False
            cursor.execute(
                "UPDATE workflow_runs SET updated_at = CURRENT_TIMESTAMP "
                "WHERE trace_id = %s AND status = 'running'",
                (case.trace_id,),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()

    def complete(
        self,
        case: DemoCase,
        result: dict[str, Any],
        claim_token: str,
    ) -> dict[str, Any]:
        request = _canonical_request(case)
        connection = self.repository._connect()
        cursor = None
        try:
            connection.start_transaction()
            cursor = connection.cursor(dictionary=True)
            self._require_exact_workflow_roots(cursor)
            self._require_current_token(cursor, case.trace_id, claim_token)
            receipt = self._single_audit(cursor, case.trace_id, _RECEIVED)
            if receipt is None:
                raise CustomerFollowupStoreError(f"{case.trace_id}: receipt is missing")
            receipt_payload = _json_object(receipt.get("payload_json"), "receipt")
            self._validate_receipt(case, request, receipt_payload)
            existing = self._single_audit(cursor, case.trace_id, _COMPLETED)
            if existing is not None:
                stored = self._completed_result(existing, request, case.trace_id)
                connection.commit()
                return stored
            workflow = self._workflow_identity(cursor, case, for_update=True)
            if workflow.get("status") != "completed" or workflow.get("current_agent") != "completed":
                raise CustomerFollowupStoreError(
                    f"{case.trace_id}: graph did not persist a completed workflow"
                )
            persistence = self._prove_completed_persistence(
                cursor,
                case,
                receipt_payload,
                claim_token=claim_token,
            )
            if not _result_matches(case, result, persistence["response_content_checks"]):
                raise CustomerFollowupStoreError(
                    f"{case.trace_id}: graph result disagrees with durable continuation output"
                )
            stored = self._insert_completion(
                cursor,
                case,
                request,
                result,
                persistence,
                claim_token=claim_token,
                recovered=False,
                receipt_log_id=int(receipt["log_id"]),
            )
            connection.commit()
            return stored
        except Exception:
            connection.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()

    def fail(self, case: DemoCase, error: Exception, claim_token: str) -> None:
        """Restore the initial cycle and release this exact token immediately."""

        connection = self.repository._connect()
        cursor = None
        try:
            connection.start_transaction()
            cursor = connection.cursor(dictionary=True)
            self._require_current_token(cursor, case.trace_id, claim_token)
            if self._single_audit(cursor, case.trace_id, _COMPLETED) is not None:
                raise CustomerFollowupConflictError(
                    f"{case.trace_id}: completed continuation cannot be failed"
                )
            receipt = self._single_audit(cursor, case.trace_id, _RECEIVED)
            if receipt is None:
                raise CustomerFollowupStoreError(f"{case.trace_id}: receipt is missing")
            receipt_payload = _json_object(receipt.get("payload_json"), "receipt")
            self._validate_receipt(case, _canonical_request(case), receipt_payload)
            latest_failure = self._latest_audit(cursor, case.trace_id, _FAILED)
            if latest_failure is not None:
                payload = _json_object(latest_failure.get("payload_json"), "failure")
                if payload.get("claim_token") == claim_token:
                    connection.commit()
                    return
            self._restore_snapshot(cursor, case, receipt_payload)
            cursor.execute(
                "INSERT INTO audit_log (trace_id, event_type, agent, payload_json) "
                "VALUES (%s, %s, 'workflow', %s)",
                (
                    case.trace_id,
                    _FAILED,
                    json.dumps(
                        {
                            "version": 1,
                            "claim_token": claim_token,
                            "error_type": type(error).__name__,
                            "message": "Customer follow-up continuation failed",
                            "history_restored": True,
                            "retryable": True,
                            "restored_workflow_status": "waiting_user",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
            # _restore_snapshot already moved this exact workflow back to
            # waiting_user and verified that row. Repeating the same UPDATE in
            # the same second can correctly produce MySQL affected-rowcount 0,
            # which used to roll back both the restoration and failure marker.
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()

    def _snapshot_initial_history(
        self,
        cursor: Any,
        case: DemoCase,
        workflow: dict[str, Any],
    ) -> dict[str, Any]:
        tables: dict[str, list[dict[str, Any]]] = {}
        for table, (order_column, columns) in _SNAPSHOT_TABLES.items():
            cursor.execute(
                f"SELECT {', '.join(columns)} FROM {table} "
                f"WHERE trace_id = %s ORDER BY {order_column} FOR UPDATE",
                (case.trace_id,),
            )
            tables[table] = [_json_safe(dict(row)) for row in cursor.fetchall()]
        handoffs = tables["agent_handoffs"]
        agents = {row.get("from_agent") for row in handoffs}
        if not {"triage_agent", "policy_agent", "response_agent"}.issubset(agents):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: initial request_info handoff history is incomplete"
            )
        assistant_response = _assistant_response_from_handoffs(handoffs, case.trace_id)
        return {
            "workflow": _json_safe(
                {
                    "trace_id": case.trace_id,
                    "ticket_id": case.ticket_id,
                    "status": workflow.get("status"),
                    "current_agent": workflow.get("current_agent"),
                    "policy_version": workflow.get("policy_version"),
                }
            ),
            "assistant_request_info_response": assistant_response,
            "tables": tables,
        }

    def _restore_snapshot(
        self,
        cursor: Any,
        case: DemoCase,
        receipt_payload: dict[str, Any],
    ) -> None:
        history = receipt_payload.get("history")
        if not isinstance(history, dict):
            raise CustomerFollowupStoreError(f"{case.trace_id}: receipt history is missing")
        expected_digest = str(receipt_payload.get("history_sha256") or "")
        if not expected_digest or _snapshot_digest(history) != expected_digest:
            raise CustomerFollowupConflictError(
                f"{case.trace_id}: immutable history snapshot failed its SHA-256 check"
            )
        tables = history.get("tables")
        if not isinstance(tables, dict):
            raise CustomerFollowupStoreError(f"{case.trace_id}: receipt tables are invalid")
        for table in _SNAPSHOT_TABLES:
            rows = tables.get(table)
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or row.get("trace_id") != case.trace_id for row in rows
            ):
                raise CustomerFollowupConflictError(
                    f"{case.trace_id}: snapshot contains out-of-scope {table} rows"
                )

        # Only trace-scoped child artifacts are replaced. Root customer/order/
        # ticket/workflow rows and every other demo trace are structurally out
        # of scope for this cleanup.
        for table in ("refund_transactions", "human_approvals", "policy_review_events", "governance_events"):
            cursor.execute(f"DELETE FROM {table} WHERE trace_id = %s", (case.trace_id,))
        cursor.execute(
            "DELETE FROM agent_handoffs WHERE trace_id = %s AND from_agent <> 'customer'",
            (case.trace_id,),
        )
        placeholders = ", ".join("%s" for _ in _LIFECYCLE_EVENTS)
        cursor.execute(
            f"DELETE FROM audit_log WHERE trace_id = %s AND event_type NOT IN ({placeholders})",
            (case.trace_id, *_LIFECYCLE_EVENTS),
        )
        for table, (_order_column, columns) in _SNAPSHOT_TABLES.items():
            rows = tables[table]
            if not rows:
                continue
            column_sql = ", ".join(columns)
            value_sql = ", ".join("%s" for _ in columns)
            for row in rows:
                cursor.execute(
                    f"INSERT INTO {table} ({column_sql}) VALUES ({value_sql})",
                    tuple(row.get(column) for column in columns),
                )
        cursor.execute(
            "UPDATE workflow_runs SET status = 'waiting_user', current_agent = 'triage_agent', "
            "policy_version = %s, completed_at = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE trace_id = %s AND ticket_id = %s",
            (
                (history.get("workflow") or {}).get("policy_version"),
                case.trace_id,
                case.ticket_id,
            ),
        )
        # mysql.connector exposes affected-row count. A legitimate restore of
        # an already restored retry therefore reports 0, just like the live
        # same-second failure path. Verify the locked identity and values
        # instead of treating a no-op as a missing workflow.
        restored = self._workflow_identity(cursor, case, for_update=True)
        expected_policy_version = (history.get("workflow") or {}).get("policy_version")
        if (
            restored.get("status") != "waiting_user"
            or restored.get("current_agent") != "triage_agent"
            or restored.get("policy_version") != expected_policy_version
        ):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: snapshot workflow was not restored"
            )

    def _reconstruct_completed_result(
        self,
        cursor: Any,
        case: DemoCase,
        receipt_payload: dict[str, Any],
        *,
        claim_token: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        persistence = self._prove_completed_persistence(
            cursor,
            case,
            receipt_payload,
            claim_token=claim_token,
        )
        records = persistence.pop("_records")
        policy_output = records["policy_output"]
        response_result = records["response_result"]
        refund = records["refund"]
        checks = persistence["response_content_checks"]
        decision = _json_object(policy_output.get("decision"), "policy decision")
        result = {
            "case_id": case.trace_id,
            "trace_id": case.trace_id,
            "ticket_id": case.ticket_id,
            "customer_id": case.customer_id,
            "order_id": case.order_id,
            "selected_order_id": case.order_id,
            "initial_message": case.message,
            "message": case.follow_up.message if case.follow_up is not None else None,
            "request_facts": _canonical_request(case),
            "order_resolution_source": "trusted_ui_selection",
            "route": "refund_agent",
            "policy_decision": decision.get("type") or decision.get("decision"),
            # Response speaks in customer-facing policy outcomes (normally
            # ``approved``), while the demo contract normalizes an issued
            # refund to the durable workflow outcome ``refund_issued``.
            "final_outcome": "refund_issued",
            "workflow_status": response_result.get("workflow_status") or "completed",
            "response_body": (_json_object(response_result.get("response"), "response")).get("body"),
            "response_content_checks": checks,
            "governance": {"triage": "allow", "policy": "allow", "response": "allow"},
            "refund_result": {
                "status": "success",
                "order_id": case.order_id,
                "amount": float(refund["amount"]),
                "currency": refund["currency"],
            },
            "expected": case.follow_up.expectations.as_dict() if case.follow_up is not None else {},
            "success": True,
            "matched_expectations": True,
            "timings_ms": {"recovered_without_graph": 0.0},
        }
        if not _result_matches(case, result, checks):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: persisted completed workflow cannot be safely reconstructed"
            )
        return result, persistence

    def _prove_completed_persistence(
        self,
        cursor: Any,
        case: DemoCase,
        receipt_payload: dict[str, Any],
        *,
        claim_token: str,
    ) -> dict[str, Any]:
        roots = self._require_exact_all_roots(cursor)
        history = receipt_payload.get("history")
        if not isinstance(history, dict) or _snapshot_digest(history) != receipt_payload.get("history_sha256"):
            raise CustomerFollowupConflictError(
                f"{case.trace_id}: immutable history snapshot failed its SHA-256 check"
            )
        initial_counts, live_counts = self._assert_initial_snapshot_preserved(
            cursor,
            case,
            history,
        )
        cursor.execute(
            "SELECT handoff_id, from_agent, to_agent, input_json, output_json, created_at "
            "FROM agent_handoffs "
            "WHERE trace_id = %s ORDER BY created_at, handoff_id",
            (case.trace_id,),
        )
        handoffs = list(cursor.fetchall())
        customer_handoffs = [
            row
            for row in handoffs
            if row["from_agent"] == "customer" and row["to_agent"] == "triage_agent"
        ]
        if (
            len(customer_handoffs) != 1
            or str(customer_handoffs[0]["handoff_id"])
            != str(receipt_payload.get("handoff_id") or "")
        ):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: persisted customer receipt handoff is not unique and exact"
            )
        continuation_handoffs = [
            row
            for row in handoffs
            if _payload_continuation_token(row.get("input_json")) == claim_token
            and _payload_continuation_token(row.get("output_json")) == claim_token
        ]
        by_agent: dict[str, list[dict[str, Any]]] = {}
        for row in continuation_handoffs:
            by_agent.setdefault(str(row["from_agent"]), []).append(row)
        required_agents = {
            "triage_agent", "policy_agent", "refund_agent", "response_agent"
        }
        if any(len(by_agent.get(agent, [])) != 1 for agent in required_agents):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: continuation stage history is not unique and complete"
            )
        continuation_routes = {
            (str(row["from_agent"]), str(row["to_agent"]))
            for row in continuation_handoffs
        }
        required_continuation_routes = {
            ("triage_agent", "policy_agent"),
            ("policy_agent", "refund_agent"),
            ("refund_agent", "response_agent"),
            ("response_agent", "end"),
        }
        if not required_continuation_routes.issubset(continuation_routes):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: persisted continuation route is incomplete"
            )
        policy_row = by_agent["policy_agent"][0]
        response_row = by_agent["response_agent"][0]
        policy_output = _json_object(policy_row.get("output_json"), "policy handoff")
        response_envelope = _json_object(response_row.get("output_json"), "response handoff")
        response_result = _json_object(
            response_envelope.get("response_result", response_envelope),
            "response result",
        )
        checks = _json_object(response_result.get("content_checks"), "response content checks")
        if not _response_checks_pass(checks):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: persisted response semantic checks did not pass"
            )
        cursor.execute(
            "SELECT event_id, agent, interceptor_action, flags_json, created_at "
            "FROM governance_events WHERE trace_id = %s ORDER BY created_at, event_id FOR UPDATE",
            (case.trace_id,),
        )
        governance_rows = list(cursor.fetchall())
        continuation_governance = [
            row
            for row in governance_rows
            if _payload_continuation_token(row.get("flags_json")) == claim_token
        ]
        governance_by_agent = {
            str(row["agent"]): row for row in continuation_governance
        }
        if any(
            governance_by_agent.get(agent, {}).get("interceptor_action") != "allow"
            for agent in ("triage_agent", "response_agent")
        ):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: continuation governance chronology is incomplete"
            )
        cursor.execute(
            "SELECT event_type, agent, payload_json FROM audit_log "
            "WHERE trace_id = %s ORDER BY log_id FOR UPDATE",
            (case.trace_id,),
        )
        continuation_audits = [
            row
            for row in cursor.fetchall()
            if _payload_continuation_token(row.get("payload_json")) == claim_token
        ]
        audit_agents = {str(row["agent"]) for row in continuation_audits}
        if not required_agents.issubset(audit_agents):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: continuation audit chronology is incomplete"
            )
        cursor.execute(
            "SELECT status, amount, currency FROM refund_transactions "
            "WHERE trace_id = %s FOR UPDATE",
            (case.trace_id,),
        )
        refunds = list(cursor.fetchall())
        if len(refunds) != 1:
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: expected one persisted refund transaction"
            )
        refund = refunds[0]
        follow_up = case.follow_up
        if follow_up is None or (
            refund.get("status") != "issued"
            or abs(float(refund.get("amount") or 0) - follow_up.requested_amount) >= 0.005
            or refund.get("currency") != follow_up.currency
        ):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: persisted refund does not match the follow-up"
            )
        return {
            "database": FINAL_DATABASE,
            "matched": True,
            "exact_root_counts": roots,
            "history": {
                "snapshot_sha256": receipt_payload["history_sha256"],
                "initial_handoff_count": initial_counts["agent_handoffs"],
                "initial_audit_count": initial_counts["audit_log"],
                "initial_governance_count": initial_counts["governance_events"],
                "live_handoff_count": live_counts["agent_handoffs"],
                "live_audit_count": live_counts["audit_log"],
                "live_governance_count": live_counts["governance_events"],
                "continuation_handoff_count": len(continuation_handoffs),
                "continuation_audit_count": len(continuation_audits),
                "continuation_governance_count": len(continuation_governance),
                "assistant_request_info_response": _snapshot_assistant_response(receipt_payload),
                "queryable_in_receipt_audit": True,
                "queryable_in_live_tables": True,
                "initial_rows_preserved": True,
            },
            "required_routes": sorted(
                f"{source}->{target}"
                for source, target in (
                    required_continuation_routes | {("customer", "triage_agent")}
                )
            ),
            "refund": {
                "status": refund["status"],
                "amount": float(refund["amount"]),
                "currency": refund["currency"],
            },
            "response_content_checks": checks,
            "ticket_raw_text_preserved": True,
            "_records": {
                "policy_output": policy_output,
                "response_result": response_result,
                "refund": refund,
            },
        }

    @staticmethod
    def _assert_initial_snapshot_preserved(
        cursor: Any,
        case: DemoCase,
        history: dict[str, Any],
    ) -> tuple[dict[str, int], dict[str, int]]:
        tables = history.get("tables")
        if not isinstance(tables, dict):
            raise CustomerFollowupStoreError(f"{case.trace_id}: receipt tables are invalid")
        initial_counts: dict[str, int] = {}
        live_counts: dict[str, int] = {}
        for table, (primary_key, columns) in _SNAPSHOT_TABLES.items():
            expected_rows = tables.get(table)
            if not isinstance(expected_rows, list):
                raise CustomerFollowupStoreError(
                    f"{case.trace_id}: receipt {table} history is invalid"
                )
            cursor.execute(
                f"SELECT {', '.join(columns)} FROM {table} "
                f"WHERE trace_id = %s ORDER BY {primary_key} FOR UPDATE",
                (case.trace_id,),
            )
            live_rows = [_json_safe(dict(row)) for row in cursor.fetchall()]
            live_by_id = {str(row.get(primary_key)): row for row in live_rows}
            for expected in expected_rows:
                if live_by_id.get(str(expected.get(primary_key))) != expected:
                    raise CustomerFollowupStoreError(
                        f"{case.trace_id}: initial {table} chronology was mutated"
                    )
            initial_counts[table] = len(expected_rows)
            live_counts[table] = len(live_rows)
        return initial_counts, live_counts

    def _insert_completion(
        self,
        cursor: Any,
        case: DemoCase,
        request: dict[str, Any],
        result: dict[str, Any],
        persistence: dict[str, Any],
        *,
        claim_token: str,
        recovered: bool,
        receipt_log_id: int,
    ) -> dict[str, Any]:
        public_persistence = {key: value for key, value in persistence.items() if key != "_records"}
        stored = {
            **result,
            "response_content_checks": public_persistence["response_content_checks"],
            "persistence": public_persistence,
            "follow_up": {
                "idempotent": False,
                "status": "recovered" if recovered else "completed",
                "receipt_log_id": receipt_log_id,
                "claim_token": claim_token,
                "recovered_without_graph": recovered,
            },
        }
        cursor.execute(
            "INSERT INTO audit_log (trace_id, event_type, agent, payload_json) "
            "VALUES (%s, %s, 'workflow', %s)",
            (
                case.trace_id,
                _COMPLETED,
                json.dumps(
                    {
                        "version": 2,
                        "request": request,
                        "claim_token": claim_token,
                        "recovered_without_graph": recovered,
                        "result": stored,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            ),
        )
        return stored

    @staticmethod
    def _insert_customer_handoff(
        cursor: Any,
        case: DemoCase,
        request: dict[str, Any],
    ) -> str:
        cursor.execute(
            "SELECT handoff_id FROM agent_handoffs "
            "WHERE trace_id = %s AND from_agent = 'customer' FOR UPDATE",
            (case.trace_id,),
        )
        if list(cursor.fetchall()):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: customer handoff exists before its receipt"
            )
        handoff_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"idox-handoff:{case.trace_id}:customer_followup")
        )
        cursor.execute(
            "INSERT INTO agent_handoffs (handoff_id, trace_id, ticket_id, from_agent, "
            "to_agent, input_json, output_json, input_tokens, output_tokens) "
            "VALUES (%s, %s, %s, 'customer', 'triage_agent', %s, %s, 0, 0)",
            (
                handoff_id,
                case.trace_id,
                case.ticket_id,
                json.dumps(request, ensure_ascii=False, sort_keys=True),
                json.dumps(
                    {"event": _RECEIVED, "next_agent": "triage_agent"},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        return handoff_id

    @staticmethod
    def _single_audit(cursor: Any, trace_id: str, event_type: str) -> dict[str, Any] | None:
        cursor.execute(
            "SELECT log_id, payload_json, created_at FROM audit_log "
            "WHERE trace_id = %s AND event_type = %s ORDER BY log_id FOR UPDATE",
            (trace_id, event_type),
        )
        rows = list(cursor.fetchall())
        if len(rows) > 1:
            raise CustomerFollowupStoreError(
                f"{trace_id}: multiple {event_type} audit records exist"
            )
        return rows[0] if rows else None

    @staticmethod
    def _latest_audit(cursor: Any, trace_id: str, event_type: str) -> dict[str, Any] | None:
        cursor.execute(
            "SELECT audit.log_id, audit.payload_json, audit.created_at, "
            "TIMESTAMPDIFF(SECOND, GREATEST(audit.created_at, workflow.updated_at), "
            "CURRENT_TIMESTAMP) AS age_seconds "
            "FROM audit_log audit JOIN workflow_runs workflow "
            "ON workflow.trace_id = audit.trace_id "
            "WHERE audit.trace_id = %s AND audit.event_type = %s "
            "ORDER BY audit.log_id DESC LIMIT 1 FOR UPDATE",
            (trace_id, event_type),
        )
        return cursor.fetchone()

    @classmethod
    def _require_current_token(cls, cursor: Any, trace_id: str, claim_token: str) -> None:
        latest = cls._latest_audit(cursor, trace_id, _CLAIMED)
        if latest is None or _claim_token(latest, trace_id) != claim_token:
            raise CustomerFollowupConflictError(
                f"{trace_id}: stale customer follow-up claim token"
            )
        failure = cls._latest_audit(cursor, trace_id, _FAILED)
        if failure is not None:
            payload = _json_object(failure.get("payload_json"), "failure")
            if payload.get("claim_token") == claim_token:
                raise CustomerFollowupConflictError(
                    f"{trace_id}: failed customer follow-up claim token is revoked"
                )

    @staticmethod
    def _workflow_identity(
        cursor: Any,
        case: DemoCase,
        *,
        for_update: bool,
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE" if for_update else ""
        cursor.execute(
            "SELECT workflow.ticket_id, workflow.status, workflow.current_agent, "
            "workflow.policy_version, ticket.customer_id, ticket.raw_text, orders.order_id "
            "FROM workflow_runs workflow JOIN tickets ticket ON ticket.ticket_id = workflow.ticket_id "
            "JOIN orders orders ON orders.customer_id = ticket.customer_id "
            "WHERE workflow.trace_id = %s AND orders.order_id = %s" + suffix,
            (case.trace_id, case.order_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: seeded workflow identity is missing"
            )
        expected = {
            "ticket_id": case.ticket_id,
            "customer_id": case.customer_id,
            "order_id": case.order_id,
            "raw_text": case.message,
        }
        if any(row.get(field) != value for field, value in expected.items()):
            raise CustomerFollowupStoreError(
                f"{case.trace_id}: seeded workflow identity does not match the fixture"
            )
        return row

    @staticmethod
    def _require_exact_workflow_roots(cursor: Any) -> None:
        cursor.execute("SELECT trace_id FROM workflow_runs ORDER BY trace_id FOR UPDATE")
        actual = tuple(str(row["trace_id"]) for row in cursor.fetchall())
        if actual != DEMO_IDS:
            raise CustomerFollowupStoreError(
                "final.workflow_runs must contain exactly demo01 through demo20"
            )

    @classmethod
    def _require_exact_all_roots(cls, cursor: Any) -> dict[str, int]:
        cls._require_exact_workflow_roots(cursor)
        expected = {
            "customers": tuple(f"customer-{case_id}" for case_id in DEMO_IDS),
            "orders": tuple(f"order-{case_id}" for case_id in DEMO_IDS),
            "tickets": tuple(f"ticket-{case_id}" for case_id in DEMO_IDS),
        }
        columns = {"customers": "customer_id", "orders": "order_id", "tickets": "ticket_id"}
        counts = {"workflow_runs": len(DEMO_IDS)}
        for table, identifiers in expected.items():
            column = columns[table]
            cursor.execute(f"SELECT {column} FROM {table} ORDER BY {column}")
            actual = tuple(str(row[column]) for row in cursor.fetchall())
            if actual != identifiers:
                raise CustomerFollowupConflictError(
                    f"final.{table} must contain exactly the canonical 20 roots"
                )
            counts[table] = len(actual)
        return counts

    @staticmethod
    def _validate_receipt(
        case: DemoCase,
        request: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        if payload.get("request") != request:
            raise CustomerFollowupConflictError(
                f"{case.trace_id}: follow-up was already received with different facts"
            )
        history = payload.get("history")
        digest = str(payload.get("history_sha256") or "")
        if not isinstance(history, dict) or not digest or _snapshot_digest(history) != digest:
            raise CustomerFollowupConflictError(
                f"{case.trace_id}: immutable history snapshot failed its SHA-256 check"
            )

    @staticmethod
    def _completed_result(
        row: dict[str, Any],
        request: dict[str, Any],
        trace_id: str,
    ) -> dict[str, Any]:
        payload = _json_object(row.get("payload_json"), "completion")
        if payload.get("request") != request or not isinstance(payload.get("result"), dict):
            raise CustomerFollowupConflictError(
                f"{trace_id}: completed follow-up conflicts with this request"
            )
        return dict(payload["result"])


def _canonical_request(case: DemoCase) -> dict[str, Any]:
    if case.trace_id not in {"demo10", "demo14"} or case.follow_up is None:
        raise CustomerFollowupStoreError(
            "Customer follow-up is allowlisted only for demo10 and demo14"
        )
    return case.follow_up.request_payload(case)


def _claim_token(row: dict[str, Any], trace_id: str) -> str:
    token = str(_json_object(row.get("payload_json"), "claim").get("claim_token") or "")
    if not token:
        raise CustomerFollowupStoreError(f"{trace_id}: continuation claim token is missing")
    return token


def _assistant_response_from_handoffs(
    handoffs: list[dict[str, Any]],
    trace_id: str,
) -> str:
    candidates = [row for row in handoffs if row.get("from_agent") == "response_agent"]
    if len(candidates) != 1:
        raise CustomerFollowupStoreError(
            f"{trace_id}: initial request_info response handoff must be unique"
        )
    envelope = _json_object(candidates[0].get("output_json"), "initial response handoff")
    response_result = _json_object(envelope.get("response_result", envelope), "initial response")
    response = _json_object(response_result.get("response"), "initial assistant response")
    body = str(response.get("body") or "").strip()
    if not body or response_result.get("workflow_status") != "waiting_user":
        raise CustomerFollowupStoreError(
            f"{trace_id}: persisted initial request_info response is invalid"
        )
    return body


def _snapshot_assistant_response(receipt_payload: dict[str, Any] | None) -> str | None:
    if not isinstance(receipt_payload, dict):
        return None
    history = receipt_payload.get("history")
    if not isinstance(history, dict):
        return None
    value = str(history.get("assistant_request_info_response") or "").strip()
    return value or None


def _snapshot_digest(history: dict[str, Any]) -> str:
    encoded = json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _request_digest(request: dict[str, Any]) -> str:
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError) as error:
        raise CustomerFollowupStoreError(f"Invalid {label} JSON") from error
    if not isinstance(parsed, dict):
        raise CustomerFollowupStoreError(f"{label} JSON must be an object")
    return parsed


def _payload_continuation_token(value: Any) -> str | None:
    if isinstance(value, dict):
        payload = value
    else:
        try:
            payload = json.loads(str(value))
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    marker = payload.get("_continuation")
    if not isinstance(marker, dict) or marker.get("type") != "customer_followup":
        return None
    token = str(marker.get("claim_token") or "").strip()
    return token or None


def _response_checks_pass(checks: dict[str, Any]) -> bool:
    required = (
        "decision_reflected",
        "missing_info_requested",
        "safe_summary_reflected",
        "outcome_anchor_reflected",
    )
    return (
        all(checks.get(name) is True for name in required)
        and not checks.get("semantic_errors")
        and not checks.get("pii_fields_detected")
        and not checks.get("forbidden_phrases")
    )


def _result_matches(
    case: DemoCase,
    result: dict[str, Any],
    persisted_checks: dict[str, Any],
) -> bool:
    follow_up = case.follow_up
    if follow_up is None:
        return False
    refund = result.get("refund_result") or {}
    return (
        result.get("trace_id") == case.trace_id
        and result.get("ticket_id") == case.ticket_id
        and result.get("customer_id") == case.customer_id
        and result.get("order_id") == case.order_id
        and result.get("message") == follow_up.message
        and result.get("route") == follow_up.expectations.route
        and result.get("policy_decision") == follow_up.expectations.policy_decision
        and result.get("final_outcome") == follow_up.expectations.outcome
        and result.get("workflow_status") == follow_up.expectations.terminal_state
        and result.get("order_resolution_source") == "trusted_ui_selection"
        and refund.get("status") == "success"
        and refund.get("order_id") == case.order_id
        and abs(float(refund.get("amount") or 0) - follow_up.requested_amount) < 0.005
        and refund.get("currency") == follow_up.currency
        and result.get("response_content_checks") == persisted_checks
        and _response_checks_pass(persisted_checks)
        and result.get("matched_expectations") is True
    )


__all__ = [
    "CustomerFollowupClaim",
    "CustomerFollowupConflictError",
    "CustomerFollowupStore",
    "CustomerFollowupStoreError",
]
