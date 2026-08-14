"""
Database service layer for AI Governance workflow.
Provides simple, typed methods for agents to log actions.
"""

import json
import os
import threading
import pymysql
from datetime import datetime
from typing import Optional, Dict, Any
from uuid import uuid4
from google.auth import identity_pool
from google.cloud.sql.connector import Connector

# Holds the current request's Vercel OIDC token in memory. main.py's
# sync_gcp_oidc_token middleware updates this on every request (production
# gets it from the x-vercel-oidc-token header; local dev from the
# VERCEL_OIDC_TOKEN env var seeded by `vercel env pull`); _VercelOidcSupplier
# reads it whenever the federated credentials need a fresh subject token.
_current_oidc_token = None
_oidc_token_lock = threading.Lock()


def set_current_oidc_token(token: str):
    global _current_oidc_token
    with _oidc_token_lock:
        _current_oidc_token = token


class _VercelOidcSupplier(identity_pool.SubjectTokenSupplier):
    def get_subject_token(self, context, request):
        with _oidc_token_lock:
            if not _current_oidc_token:
                raise RuntimeError(
                    "No Vercel OIDC token available yet, sync_gcp_oidc_token "
                    "middleware should run before any DB access."
                )
            return _current_oidc_token


def _build_gcp_credentials():
    """Federated GCP credentials exchanged from Vercel's OIDC token via
    Workload Identity Federation (see main.py's sync_gcp_oidc_token
    middleware, which feeds _VercelOidcSupplier above)."""
    project_number = os.environ["GCP_PROJECT_NUMBER"]
    pool_id = os.environ["GCP_WORKLOAD_IDENTITY_POOL_ID"]
    provider_id = os.environ["GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID"]
    service_account_email = os.environ["GCP_SERVICE_ACCOUNT_EMAIL"]
    return identity_pool.Credentials(
        audience=(
            f"//iam.googleapis.com/projects/{project_number}/locations/global/"
            f"workloadIdentityPools/{pool_id}/providers/{provider_id}"
        ),
        subject_token_type="urn:ietf:params:oauth:token-type:jwt",
        service_account_impersonation_url=(
            f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{service_account_email}:generateAccessToken"
        ),
        subject_token_supplier=_VercelOidcSupplier(),
    )


class DBService:
    def __init__(self, instance_connection_name: Optional[str] = None, user: str = None,
                 password: str = None, database: str = "main_db",
                 host: Optional[str] = None, port: int = 3306):
        """Two connection modes:

        - Direct (`host` set): plain pymysql TCP connection to `host:port`
          using `user`/`password`/`database`. Used for local dev or whenever
          the MySQL instance (including a GCP Cloud SQL instance with a
          public IP / authorized network) is reachable directly, no GCP
          Workload Identity Federation setup required.
        - Cloud SQL Connector (`host` unset, `instance_connection_name` set):
          the production/Vercel path, federated via Workload Identity
          Federation (see `_build_gcp_credentials`).
        """
        self.instance_connection_name = instance_connection_name
        self.user = user
        self.password = password
        self.database = database
        self.host = host
        self.port = port
        self._connector = None if host else Connector(credentials=_build_gcp_credentials())
        self.conn = None
        # No eager connect here, for the Cloud SQL Connector path, the first
        # real request has already run main.py's OIDC-sync middleware by the
        # time _get_cursor() is called, so the token is guaranteed to exist.

    def _connect(self):
        """Establish the database connection (direct TCP or Cloud SQL Connector)."""
        try:
            if self.host:
                self.conn = pymysql.connect(
                    host=self.host, port=self.port,
                    user=self.user, password=self.password, database=self.database,
                    autocommit=True,
                )
            else:
                self.conn = self._connector.connect(
                    self.instance_connection_name, "pymysql",
                    user=self.user, password=self.password, db=self.database,
                    autocommit=True,
                )
        except pymysql.MySQLError as err:
            print(f"Error connecting to database: {err}")
            raise

    def _get_cursor(self):
        """Get a cursor, (re)connecting if needed."""
        if self.conn is None or not self.conn.open:
            self._connect()
        else:
            self.conn.ping(reconnect=True)
        return self.conn.cursor(pymysql.cursors.DictCursor)

    def close(self):
        """Close the database connection."""
        if self.conn and self.conn.open:
            self.conn.close()

    # ========== Workflow Runs ==========
    def create_workflow_run(self, ticket_id: str, policy_version: str = "1.0") -> str:
        """Create a new workflow run (LangGraph trace)."""
        trace_id = str(uuid4())
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                INSERT INTO workflow_runs (trace_id, ticket_id, status, policy_version)
                VALUES (%s, %s, 'running', %s)
            """, (trace_id, ticket_id, policy_version))
            self.conn.commit()
            return trace_id
        except pymysql.MySQLError as err:
            self.conn.rollback()
            print(f"Error creating workflow run: {err}")
            raise
        finally:
            cursor.close()

    def update_workflow_status(self, trace_id: str, status: str, current_agent: Optional[str] = None):
        """Update workflow run status and current agent."""
        cursor = self._get_cursor()
        try:
            if current_agent:
                cursor.execute("""
                    UPDATE workflow_runs
                    SET status = %s, current_agent = %s, updated_at = NOW()
                    WHERE trace_id = %s
                """, (status, current_agent, trace_id))
            else:
                cursor.execute("""
                    UPDATE workflow_runs
                    SET status = %s, updated_at = NOW()
                    WHERE trace_id = %s
                """, (status, trace_id))
            self.conn.commit()
        except pymysql.MySQLError as err:
            self.conn.rollback()
            print(f"Error updating workflow status: {err}")
            raise
        finally:
            cursor.close()

    # ========== Agent Handoffs ==========
    def log_agent_handoff(self, trace_id: str, from_agent: str, to_agent: str,
                         input_data: Dict[str, Any], output_data: Dict[str, Any]):
        """Log an agent handoff: input and output JSON at each boundary."""
        handoff_id = str(uuid4())
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                INSERT INTO agent_handoffs
                (handoff_id, trace_id, from_agent, to_agent, input_json, output_json)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                handoff_id,
                trace_id,
                from_agent,
                to_agent,
                json.dumps(input_data, default=str),
                json.dumps(output_data, default=str)
            ))
            self.conn.commit()
            return handoff_id
        except pymysql.MySQLError as err:
            self.conn.rollback()
            print(f"Error logging handoff: {err}")
            raise
        finally:
            cursor.close()

    # ========== Governance Events ==========
    def log_governance_event(self, trace_id: str, agent: str, owasp_category: str,
                            interceptor_action: str, trigger_score: Optional[float] = None,
                            flags: Optional[Dict[str, Any]] = None,
                            offending_content: Optional[str] = None):
        """Log a governance event (OWASP detection, quarantine, block, etc.)."""
        event_id = str(uuid4())
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                INSERT INTO governance_events
                (event_id, trace_id, agent, owasp_category, trigger_score,
                 interceptor_action, flags_json, offending_content)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                event_id,
                trace_id,
                agent,
                owasp_category,
                trigger_score,
                interceptor_action,
                json.dumps(flags, default=str) if flags else None,
                offending_content
            ))
            self.conn.commit()
            return event_id
        except pymysql.MySQLError as err:
            self.conn.rollback()
            print(f"Error logging governance event: {err}")
            raise
        finally:
            cursor.close()

    # ========== Human Approvals (HITL Queue) ==========
    def create_human_approval(self, trace_id: str, reason: str,
                             amount_requested: Optional[float] = None,
                             triggering_event_id: Optional[str] = None) -> str:
        """Create a pending human approval request (pause & wait)."""
        approval_id = str(uuid4())
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                INSERT INTO human_approvals
                (approval_id, trace_id, triggering_event_id, reason, amount_requested, status)
                VALUES (%s, %s, %s, %s, %s, 'pending')
            """, (approval_id, trace_id, triggering_event_id, reason, amount_requested))
            self.conn.commit()
            return approval_id
        except pymysql.MySQLError as err:
            self.conn.rollback()
            print(f"Error creating human approval: {err}")
            raise
        finally:
            cursor.close()

    def resolve_human_approval(self, approval_id: str, status: str,
                              reviewer: str, notes: Optional[str] = None,
                              decision: Optional[str] = None,
                              resolved_amount: Optional[float] = None):
        """Resolve a human approval. `status` is the coarse pending/approved/
        rejected outcome; `decision` (approve/partial_refund/deny/request_info)
        and `resolved_amount` capture the reviewer's actual choice for cases
        driven by the 4-way admin decision UI."""
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                UPDATE human_approvals
                SET status = %s, reviewer = %s, notes = %s, decision = %s,
                    resolved_amount = %s, resolved_at = NOW()
                WHERE approval_id = %s
            """, (status, reviewer, notes, decision, resolved_amount, approval_id))
            self.conn.commit()
        except pymysql.MySQLError as err:
            self.conn.rollback()
            print(f"Error resolving approval: {err}")
            raise
        finally:
            cursor.close()

    def get_pending_approvals(self, limit: int = 10):
        """Get pending human approvals for the dashboard, with the triggering
        governance event's OWASP context joined in for reviewer context."""
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                SELECT ha.*,
                       ge.owasp_category AS event_owasp_category,
                       ge.trigger_score AS event_trigger_score,
                       ge.interceptor_action AS event_interceptor_action,
                       ge.offending_content AS event_offending_content
                FROM human_approvals ha
                LEFT JOIN governance_events ge ON ha.triggering_event_id = ge.event_id
                WHERE ha.status = 'pending'
                ORDER BY ha.created_at DESC
                LIMIT %s
            """, (limit,))
            return cursor.fetchall()
        except pymysql.MySQLError as err:
            print(f"Error fetching pending approvals: {err}")
            return []
        finally:
            cursor.close()

    # ========== Audit Log ==========
    def log_audit_event(self, trace_id: str, event_type: str, agent: Optional[str] = None,
                       payload: Optional[Dict[str, Any]] = None):
        """Append to audit log."""
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                INSERT INTO audit_log (trace_id, event_type, agent, payload_json)
                VALUES (%s, %s, %s, %s)
            """, (
                trace_id,
                event_type,
                agent,
                json.dumps(payload, default=str) if payload else None
            ))
            self.conn.commit()
        except pymysql.MySQLError as err:
            self.conn.rollback()
            print(f"Error logging audit event: {err}")
            raise
        finally:
            cursor.close()

    def query_audit_log(self, trace_id: Optional[str] = None,
                       event_type: Optional[str] = None,
                       agent: Optional[str] = None,
                       limit: int = 100) -> list:
        """Query audit log with optional filters."""
        cursor = self._get_cursor()
        try:
            query = "SELECT * FROM audit_log WHERE 1=1"
            params = []

            if trace_id:
                query += " AND trace_id = %s"
                params.append(trace_id)
            if event_type:
                query += " AND event_type = %s"
                params.append(event_type)
            if agent:
                query += " AND agent = %s"
                params.append(agent)

            query += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)

            cursor.execute(query, params)
            return cursor.fetchall()
        except pymysql.MySQLError as err:
            print(f"Error querying audit log: {err}")
            return []
        finally:
            cursor.close()

    # ========== Refund Transactions ==========
    def create_refund_transaction(self, trace_id: str, amount: float,
                                 currency: str = "USD",
                                 approval_id: Optional[str] = None) -> str:
        """Record a refund transaction (pending)."""
        transaction_id = str(uuid4())
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                INSERT INTO refund_transactions
                (transaction_id, trace_id, approval_id, amount, currency, status)
                VALUES (%s, %s, %s, %s, %s, 'pending')
            """, (transaction_id, trace_id, approval_id, amount, currency))
            self.conn.commit()
            return transaction_id
        except pymysql.MySQLError as err:
            self.conn.rollback()
            print(f"Error creating refund transaction: {err}")
            raise
        finally:
            cursor.close()

    def update_refund_transaction(self, transaction_id: str, status: str,
                                 external_ref: Optional[str] = None):
        """Update refund transaction status (issued/failed/blocked)."""
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                UPDATE refund_transactions
                SET status = %s, external_ref = %s, updated_at = NOW()
                WHERE transaction_id = %s
            """, (status, external_ref, transaction_id))
            self.conn.commit()
        except pymysql.MySQLError as err:
            self.conn.rollback()
            print(f"Error updating refund transaction: {err}")
            raise
        finally:
            cursor.close()

    # ========== Query Helpers ==========
    def get_workflow_run(self, trace_id: str) -> Optional[Dict]:
        """Get full workflow run details."""
        cursor = self._get_cursor()
        try:
            cursor.execute("SELECT * FROM workflow_runs WHERE trace_id = %s", (trace_id,))
            return cursor.fetchone()
        except pymysql.MySQLError as err:
            print(f"Error fetching workflow run: {err}")
            return None
        finally:
            cursor.close()

    def query_governance_events(self, owasp_category: Optional[str] = None,
                               interceptor_action: Optional[str] = None,
                               agent: Optional[str] = None,
                               limit: int = 100) -> list:
        """Query governance events across all workflow runs, with optional filters."""
        cursor = self._get_cursor()
        try:
            query = "SELECT * FROM governance_events WHERE 1=1"
            params = []

            if owasp_category:
                query += " AND owasp_category = %s"
                params.append(owasp_category)
            if interceptor_action:
                query += " AND interceptor_action = %s"
                params.append(interceptor_action)
            if agent:
                query += " AND agent = %s"
                params.append(agent)

            query += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)

            cursor.execute(query, params)
            return cursor.fetchall()
        except pymysql.MySQLError as err:
            print(f"Error querying governance events: {err}")
            return []
        finally:
            cursor.close()

    def get_governance_events_for_run(self, trace_id: str) -> list:
        """Get all governance events for a workflow run."""
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                SELECT * FROM governance_events
                WHERE trace_id = %s
                ORDER BY created_at ASC
            """, (trace_id,))
            return cursor.fetchall()
        except pymysql.MySQLError as err:
            print(f"Error fetching governance events: {err}")
            return []
        finally:
            cursor.close()

    def get_handoffs_for_run(self, trace_id: str) -> list:
        """Get all agent handoffs for a workflow run."""
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                SELECT * FROM agent_handoffs
                WHERE trace_id = %s
                ORDER BY created_at ASC
            """, (trace_id,))
            return cursor.fetchall()
        except pymysql.MySQLError as err:
            print(f"Error fetching handoffs: {err}")
            return []
        finally:
            cursor.close()

    # ========== Case Aggregation (Refund Ops Console) ==========

    def list_workflow_runs(self, limit: int = 200) -> list:
        """Base rows for the case queue: one per workflow run, joined with its
        ticket and customer for display fields."""
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                SELECT w.*, t.customer_id AS t_customer_id, t.raw_text, t.sanitized_text,
                       t.refund_reason, t.requested_amount, t.currency, t.status AS ticket_status,
                       t.injection_flag, t.created_at AS ticket_created_at, t.updated_at AS ticket_updated_at,
                       c.full_name, c.email
                FROM workflow_runs w
                LEFT JOIN tickets t ON w.ticket_id = t.ticket_id
                LEFT JOIN customers c ON t.customer_id = c.customer_id
                ORDER BY w.updated_at DESC
                LIMIT %s
            """, (limit,))
            return cursor.fetchall()
        except pymysql.MySQLError as err:
            print(f"Error listing workflow runs: {err}")
            return []
        finally:
            cursor.close()

    def get_workflow_run_with_context(self, trace_id: str) -> Optional[Dict]:
        """Single workflow run joined with its ticket and customer."""
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                SELECT w.*, t.customer_id AS t_customer_id, t.raw_text, t.sanitized_text,
                       t.refund_reason, t.requested_amount, t.currency, t.status AS ticket_status,
                       t.injection_flag, t.created_at AS ticket_created_at, t.updated_at AS ticket_updated_at,
                       c.full_name, c.email
                FROM workflow_runs w
                LEFT JOIN tickets t ON w.ticket_id = t.ticket_id
                LEFT JOIN customers c ON t.customer_id = c.customer_id
                WHERE w.trace_id = %s
            """, (trace_id,))
            return cursor.fetchone()
        except pymysql.MySQLError as err:
            print(f"Error fetching workflow run context: {err}")
            return None
        finally:
            cursor.close()

    def get_order_for_customer(self, customer_id: Optional[str]) -> Optional[Dict]:
        """Best-effort order lookup by customer, used as a fallback when a
        handoff's embedded order_facts snapshot isn't available yet."""
        if not customer_id:
            return None
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                SELECT * FROM orders WHERE customer_id = %s
                ORDER BY created_at DESC LIMIT 1
            """, (customer_id,))
            return cursor.fetchone()
        except pymysql.MySQLError as err:
            print(f"Error fetching order for customer: {err}")
            return None
        finally:
            cursor.close()

    def _bulk_by_trace(self, table: str, trace_ids: list, order_by: str = "created_at ASC") -> Dict[str, list]:
        """Fetch all rows from `table` for a batch of trace_ids in one query,
        grouped by trace_id. Used to avoid N+1 queries when building the queue."""
        result = {tid: [] for tid in trace_ids}
        if not trace_ids:
            return result
        cursor = self._get_cursor()
        try:
            placeholders = ",".join(["%s"] * len(trace_ids))
            cursor.execute(
                f"SELECT * FROM {table} WHERE trace_id IN ({placeholders}) ORDER BY {order_by}",
                tuple(trace_ids),
            )
            for row in cursor.fetchall():
                result.setdefault(row["trace_id"], []).append(row)
            return result
        except pymysql.MySQLError as err:
            print(f"Error bulk-fetching {table}: {err}")
            return result
        finally:
            cursor.close()

    def bulk_handoffs(self, trace_ids: list) -> Dict[str, list]:
        return self._bulk_by_trace("agent_handoffs", trace_ids)

    def bulk_governance_events(self, trace_ids: list) -> Dict[str, list]:
        return self._bulk_by_trace("governance_events", trace_ids)

    def bulk_approvals(self, trace_ids: list) -> Dict[str, list]:
        return self._bulk_by_trace("human_approvals", trace_ids)

    def bulk_refund_transactions(self, trace_ids: list) -> Dict[str, list]:
        return self._bulk_by_trace("refund_transactions", trace_ids)

    def bulk_audit_log(self, trace_ids: list) -> Dict[str, list]:
        return self._bulk_by_trace("audit_log", trace_ids, order_by="created_at DESC")

    def get_case_bundle(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """Assemble every row needed to render one full case."""
        workflow = self.get_workflow_run_with_context(trace_id)
        if not workflow:
            return None
        ticket = {
            "ticket_id": workflow.get("ticket_id"),
            "customer_id": workflow.get("t_customer_id"),
            "raw_text": workflow.get("raw_text"),
            "sanitized_text": workflow.get("sanitized_text"),
            "refund_reason": workflow.get("refund_reason"),
            "requested_amount": workflow.get("requested_amount"),
            "currency": workflow.get("currency"),
            "status": workflow.get("ticket_status"),
            "injection_flag": workflow.get("injection_flag"),
            "created_at": workflow.get("ticket_created_at"),
            "updated_at": workflow.get("ticket_updated_at"),
        }
        customer = {"full_name": workflow.get("full_name"), "email": workflow.get("email")} if workflow.get("full_name") else None
        order = self.get_order_for_customer(workflow.get("t_customer_id"))

        cursor = self._get_cursor()
        try:
            cursor.execute("SELECT * FROM agent_handoffs WHERE trace_id = %s ORDER BY created_at ASC", (trace_id,))
            handoffs = cursor.fetchall()
            cursor.execute("SELECT * FROM governance_events WHERE trace_id = %s ORDER BY created_at ASC", (trace_id,))
            governance_events = cursor.fetchall()
            cursor.execute("SELECT * FROM human_approvals WHERE trace_id = %s ORDER BY created_at DESC", (trace_id,))
            approvals = cursor.fetchall()
            cursor.execute("SELECT * FROM refund_transactions WHERE trace_id = %s ORDER BY created_at DESC", (trace_id,))
            refunds = cursor.fetchall()
            cursor.execute("SELECT * FROM audit_log WHERE trace_id = %s ORDER BY created_at DESC", (trace_id,))
            audit_log = cursor.fetchall()
        finally:
            cursor.close()

        pending_approval_id = next((a["approval_id"] for a in approvals if a["status"] == "pending"), None)

        return {
            "workflow": workflow,
            "ticket": ticket,
            "customer": customer,
            "order": order,
            "handoffs": handoffs,
            "governance_events": governance_events,
            "approvals": approvals,
            "refunds": refunds,
            "audit_log": audit_log,
            "pending_approval_id": pending_approval_id,
        }

    def get_latest_pending_approval(self, trace_id: str) -> Optional[Dict]:
        cursor = self._get_cursor()
        try:
            cursor.execute("""
                SELECT * FROM human_approvals
                WHERE trace_id = %s AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
            """, (trace_id,))
            return cursor.fetchone()
        except pymysql.MySQLError as err:
            print(f"Error fetching pending approval: {err}")
            return None
        finally:
            cursor.close()

    # ========== Admin Case Decisions ==========

    def apply_case_decision(self, trace_id: str, action: str, reviewer: str,
                             notes: Optional[str] = None,
                             amount: Optional[float] = None) -> Dict[str, Any]:
        """Apply an admin action from the case detail view.

        action is one of: approve, partial_refund, deny, request_info,
        confirm_block, release. approve/partial_refund/deny/request_info are
        the reviewer's 4-way decision on a manual_review/governance-blocked
        case (mirrors the Policy Agent's own decision vocabulary); confirm_block/
        release are unchanged, quarantine-specific actions. Resolves the case's
        pending human_approvals row (if any), updates workflow_runs.status,
        optionally issues a refund_transaction, and always appends an
        audit_log entry so the case timeline reflects the action.
        """
        approval = self.get_latest_pending_approval(trace_id)
        event_type = f"admin_{action}"
        payload: Dict[str, Any] = {"reviewer": reviewer}
        if notes:
            payload["notes"] = notes

        if action == "approve":
            resolved_amount = amount if amount is not None else (_to_float(approval.get("amount_requested")) if approval else None)
            if approval:
                self.resolve_human_approval(approval["approval_id"], "approved", reviewer, notes,
                                             decision="approve", resolved_amount=resolved_amount)
            if resolved_amount:
                txn_id = self.create_refund_transaction(trace_id, resolved_amount, approval_id=approval["approval_id"] if approval else None)
                self.update_refund_transaction(txn_id, "issued", external_ref=f"ADMIN-{reviewer}")
                payload["transaction_id"] = txn_id
                payload["amount"] = resolved_amount
            self.update_workflow_status(trace_id, "completed")

        elif action == "partial_refund":
            resolved_amount = _to_float(amount)
            if not resolved_amount or resolved_amount <= 0:
                raise ValueError("partial_refund requires a positive amount")
            if approval:
                self.resolve_human_approval(approval["approval_id"], "approved", reviewer, notes,
                                             decision="partial_refund", resolved_amount=resolved_amount)
            txn_id = self.create_refund_transaction(trace_id, resolved_amount, approval_id=approval["approval_id"] if approval else None)
            self.update_refund_transaction(txn_id, "issued", external_ref=f"ADMIN-{reviewer}")
            payload["transaction_id"] = txn_id
            payload["amount"] = resolved_amount
            self.update_workflow_status(trace_id, "completed")

        elif action == "deny":
            if approval:
                self.resolve_human_approval(approval["approval_id"], "rejected", reviewer, notes,
                                             decision="deny")
            self.update_workflow_status(trace_id, "failed")

        elif action == "request_info":
            if approval:
                self.resolve_human_approval(approval["approval_id"], "rejected", reviewer, notes,
                                             decision="request_info")
            self.update_workflow_status(trace_id, "waiting_user")

        elif action == "reject":
            # Back-compat alias for "deny" (older callers/tests).
            if approval:
                self.resolve_human_approval(approval["approval_id"], "rejected", reviewer, notes,
                                             decision="deny")
            self.update_workflow_status(trace_id, "failed")

        elif action == "confirm_block":
            if approval:
                self.resolve_human_approval(approval["approval_id"], "rejected", reviewer, notes or "Quarantine block confirmed.")
            self.update_workflow_status(trace_id, "failed")

        elif action == "release":
            # Override the quarantine hold and route to manual review; leave the
            # approval pending so a human still signs off, but flag the override
            # so status derivation stops treating this case as quarantined.
            event_type = "quarantine_override"

        else:
            raise ValueError(f"Unknown case decision action: {action}")

        self.log_audit_event(trace_id, event_type, agent=f"Admin ({reviewer})", payload=payload)
        return {"status": "applied", "action": action}


def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
