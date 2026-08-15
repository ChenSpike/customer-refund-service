"""Read-only MySQL queries for the operations dashboard.

The dashboard-v2 branch used a second database stack and silently converted
query failures to empty lists.  This adapter deliberately reuses the canonical
``GCPRepository`` connection configuration, opens one connection per read, and
raises a typed error whenever a database operation fails.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


ConnectionFactory = Callable[[], Any]
FINAL_DATABASE_NAME = "final"


class DashboardRepositoryError(RuntimeError):
    """A dashboard read failed; the original exception is retained as cause."""


def require_final_database_name(database_name: Any) -> str:
    """Reject dashboard access unless it is explicitly bound to ``final``.

    The dashboard is intentionally a single-purpose demo surface.  Falling
    back to ``main_db`` (or accepting a look-alike name) could expose or mutate
    a previous branch's data, so the comparison is deliberately exact.
    """

    if database_name != FINAL_DATABASE_NAME:
        raise DashboardRepositoryError(
            "Dashboard access is restricted to the final database"
        )
    return FINAL_DATABASE_NAME


def database_config_status() -> dict[str, Any]:
    """Return presence-only configuration metadata without exposing secrets."""

    from db.database import DEFAULT_DATABASE_NAME, load_database_env

    load_database_env()
    required = ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD")
    configured = {name: bool(os.getenv(name)) for name in required}
    missing = [name for name, present in configured.items() if not present]
    database_name = os.getenv("MYSQL_DATABASE", DEFAULT_DATABASE_NAME)
    if missing:
        status = "missing"
    elif database_name != FINAL_DATABASE_NAME:
        status = "unsafe_database"
    else:
        status = "ok"
    return {
        "status": status,
        "database": database_name,
        "configured": configured,
        "missing": missing,
    }


class DashboardRepository:
    """Query canonical workflow tables without owning any workflow mutations."""

    def __init__(self, connection_factory: ConnectionFactory, database_name: str) -> None:
        self._connection_factory = connection_factory
        self.database_name = require_final_database_name(database_name)

    @classmethod
    def from_env(cls) -> "DashboardRepository":
        from db.database import GCPRepository

        try:
            canonical = GCPRepository.from_env()
        except Exception as exc:
            raise DashboardRepositoryError("Dashboard database configuration is unavailable") from exc
        return cls(canonical._connect, canonical.database_name)

    @contextmanager
    def _cursor(self) -> Iterator[tuple[Any, Any]]:
        connection = None
        cursor = None
        try:
            # Re-check on every operation so changing the public attribute after
            # construction cannot bypass the single-database boundary.
            require_final_database_name(self.database_name)
            connection = self._connection_factory()
            cursor = connection.cursor(dictionary=True)
            cursor.execute("SELECT DATABASE() AS database_name")
            selected = dict(cursor.fetchone() or {}).get("database_name")
            require_final_database_name(selected)
            yield connection, cursor
        except DashboardRepositoryError:
            raise
        except Exception as exc:
            raise DashboardRepositoryError("Dashboard database read failed") from exc
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    @staticmethod
    def _rows(cursor: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    def ping(self) -> dict[str, Any]:
        with self._cursor() as (_, cursor):
            cursor.execute("SELECT DATABASE() AS database_name, 1 AS ok")
            row = dict(cursor.fetchone() or {})
        selected = row.get("database_name")
        return {
            "status": (
                "ok"
                if row.get("ok") == 1 and selected == FINAL_DATABASE_NAME
                else "error"
            ),
            "database": selected or self.database_name,
        }

    def list_case_bundles(self, limit: int = 200) -> list[dict[str, Any]]:
        """Fetch queue data in bulk so twenty demo cases do not cause N+1 reads."""

        with self._cursor() as (_, cursor):
            cursor.execute(
                """
                SELECT
                  w.*,
                  t.customer_id AS ticket_customer_id,
                  t.raw_text,
                  t.sanitized_text,
                  t.refund_reason,
                  t.requested_amount,
                  t.currency,
                  t.status AS ticket_status,
                  t.injection_flag,
                  t.created_at AS ticket_created_at,
                  t.updated_at AS ticket_updated_at,
                  c.full_name,
                  c.email
                FROM workflow_runs w
                LEFT JOIN tickets t ON t.ticket_id = w.ticket_id
                LEFT JOIN customers c ON c.customer_id = t.customer_id
                ORDER BY w.updated_at DESC, w.trace_id
                LIMIT %s
                """,
                (limit,),
            )
            workflows = self._rows(cursor)
            trace_ids = [str(row["trace_id"]) for row in workflows]
            grouped = {
                "handoffs": self._bulk_by_trace(cursor, "agent_handoffs", trace_ids),
                "governance_events": self._bulk_by_trace(cursor, "governance_events", trace_ids),
                "approvals": self._bulk_by_trace(cursor, "human_approvals", trace_ids),
                "refunds": self._bulk_by_trace(cursor, "refund_transactions", trace_ids),
                "audit_log": self._bulk_by_trace(cursor, "audit_log", trace_ids),
                "policy_reviews": self._bulk_by_trace(cursor, "policy_review_events", trace_ids),
            }

        bundles = []
        for workflow in workflows:
            trace_id = str(workflow["trace_id"])
            bundles.append(
                self._bundle_from_workflow(
                    workflow,
                    orders=[],
                    **{name: rows.get(trace_id, []) for name, rows in grouped.items()},
                )
            )
        return bundles

    def get_case_bundle(self, trace_id: str) -> dict[str, Any] | None:
        with self._cursor() as (_, cursor):
            cursor.execute(
                """
                SELECT
                  w.*,
                  t.customer_id AS ticket_customer_id,
                  t.raw_text,
                  t.sanitized_text,
                  t.refund_reason,
                  t.requested_amount,
                  t.currency,
                  t.status AS ticket_status,
                  t.injection_flag,
                  t.created_at AS ticket_created_at,
                  t.updated_at AS ticket_updated_at,
                  c.full_name,
                  c.email
                FROM workflow_runs w
                LEFT JOIN tickets t ON t.ticket_id = w.ticket_id
                LEFT JOIN customers c ON c.customer_id = t.customer_id
                WHERE w.trace_id = %s
                """,
                (trace_id,),
            )
            raw = cursor.fetchone()
            if raw is None:
                return None
            workflow = dict(raw)
            related = {
                "handoffs": self._for_trace(cursor, "agent_handoffs", trace_id),
                "governance_events": self._for_trace(cursor, "governance_events", trace_id),
                "approvals": self._for_trace(cursor, "human_approvals", trace_id),
                "refunds": self._for_trace(cursor, "refund_transactions", trace_id),
                "audit_log": self._for_trace(cursor, "audit_log", trace_id, descending=True),
                "policy_reviews": self._for_trace(cursor, "policy_review_events", trace_id),
            }
            orders = self._orders_for_customer(cursor, workflow.get("ticket_customer_id"))
        return self._bundle_from_workflow(workflow, orders=orders, **related)

    def query_audit(
        self,
        *,
        trace_id: str | None = None,
        event_type: str | None = None,
        agent: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        for column, value in (
            ("trace_id", trace_id),
            ("event_type", event_type),
            ("agent", agent),
        ):
            if value:
                conditions.append(f"{column} = %s")
                params.append(value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        with self._cursor() as (_, cursor):
            cursor.execute(
                f"SELECT * FROM audit_log {where} ORDER BY created_at DESC, log_id DESC LIMIT %s",
                tuple(params),
            )
            return self._rows(cursor)

    def query_governance(
        self,
        *,
        trace_id: str | None = None,
        owasp_category: str | None = None,
        interceptor_action: str | None = None,
        agent: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        for column, value in (
            ("trace_id", trace_id),
            ("owasp_category", owasp_category),
            ("interceptor_action", interceptor_action),
            ("agent", agent),
        ):
            if value:
                conditions.append(f"{column} = %s")
                params.append(value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        with self._cursor() as (_, cursor):
            cursor.execute(
                f"SELECT * FROM governance_events {where} ORDER BY created_at DESC, event_id DESC LIMIT %s",
                tuple(params),
            )
            return self._rows(cursor)

    def pending_approvals(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return typed approval triggers without assuming one polymorphic FK."""

        with self._cursor() as (_, cursor):
            cursor.execute(
                """
                SELECT
                  a.*,
                  g.owasp_category AS governance_owasp_category,
                  g.trigger_score AS governance_trigger_score,
                  g.interceptor_action AS governance_action,
                  g.offending_content AS governance_offending_content,
                  p.review_type AS policy_review_type,
                  p.detail AS policy_review_detail,
                  p.policy_ids_json AS policy_ids_json,
                  t.requested_amount AS ticket_requested_amount,
                  t.currency AS ticket_currency,
                  o.amount_paid AS order_amount_paid,
                  o.prior_refund_total AS order_prior_refund_total,
                  o.currency AS order_currency
                FROM human_approvals a
                JOIN workflow_runs w ON w.trace_id = a.trace_id
                JOIN tickets t ON t.ticket_id = w.ticket_id
                LEFT JOIN orders o
                  ON o.order_id = CONCAT('order-', a.trace_id)
                 AND o.customer_id = t.customer_id
                LEFT JOIN governance_events g
                  ON a.triggering_event_type = 'governance'
                 AND a.triggering_event_id = g.event_id
                LEFT JOIN policy_review_events p
                  ON a.triggering_event_type = 'policy_review'
                 AND a.triggering_event_id = p.policy_review_event_id
                WHERE a.status = 'pending'
                ORDER BY a.created_at DESC, a.approval_id
                LIMIT %s
                """,
                (limit,),
            )
            return self._rows(cursor)

    @staticmethod
    def _bulk_by_trace(cursor: Any, table: str, trace_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        allowed = {
            "agent_handoffs",
            "governance_events",
            "human_approvals",
            "refund_transactions",
            "audit_log",
            "policy_review_events",
        }
        if table not in allowed:
            raise ValueError(f"Unsupported dashboard table: {table}")
        primary_key = {
            "agent_handoffs": "handoff_id",
            "governance_events": "event_id",
            "human_approvals": "approval_id",
            "refund_transactions": "transaction_id",
            "audit_log": "log_id",
            "policy_review_events": "policy_review_event_id",
        }[table]
        grouped = {trace_id: [] for trace_id in trace_ids}
        if not trace_ids:
            return grouped
        placeholders = ",".join(["%s"] * len(trace_ids))
        cursor.execute(
            f"SELECT * FROM {table} WHERE trace_id IN ({placeholders}) "
            f"ORDER BY trace_id, created_at, {primary_key}",
            tuple(trace_ids),
        )
        for raw in cursor.fetchall():
            row = dict(raw)
            grouped.setdefault(str(row["trace_id"]), []).append(row)
        return grouped

    @staticmethod
    def _for_trace(
        cursor: Any,
        table: str,
        trace_id: str,
        *,
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        allowed = {
            "agent_handoffs",
            "governance_events",
            "human_approvals",
            "refund_transactions",
            "audit_log",
            "policy_review_events",
        }
        if table not in allowed:
            raise ValueError(f"Unsupported dashboard table: {table}")
        primary_key = {
            "agent_handoffs": "handoff_id",
            "governance_events": "event_id",
            "human_approvals": "approval_id",
            "refund_transactions": "transaction_id",
            "audit_log": "log_id",
            "policy_review_events": "policy_review_event_id",
        }[table]
        direction = "DESC" if descending else "ASC"
        cursor.execute(
            f"SELECT * FROM {table} WHERE trace_id = %s "
            f"ORDER BY created_at {direction}, {primary_key} {direction}",
            (trace_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _orders_for_customer(cursor: Any, customer_id: Any) -> list[dict[str, Any]]:
        if not customer_id:
            return []
        cursor.execute(
            "SELECT * FROM orders WHERE customer_id = %s ORDER BY created_at DESC, order_id",
            (customer_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _bundle_from_workflow(
        workflow: dict[str, Any],
        *,
        handoffs: list[dict[str, Any]],
        governance_events: list[dict[str, Any]],
        approvals: list[dict[str, Any]],
        refunds: list[dict[str, Any]],
        audit_log: list[dict[str, Any]],
        policy_reviews: list[dict[str, Any]],
        orders: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ticket = {
            "ticket_id": workflow.get("ticket_id"),
            "customer_id": workflow.get("ticket_customer_id"),
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
        customer = None
        if workflow.get("ticket_customer_id"):
            customer = {
                "customer_id": workflow.get("ticket_customer_id"),
                "full_name": workflow.get("full_name"),
                "email": workflow.get("email"),
            }
        return {
            "workflow": workflow,
            "ticket": ticket,
            "customer": customer,
            "orders": orders,
            "handoffs": handoffs,
            "governance_events": governance_events,
            "approvals": approvals,
            "refunds": refunds,
            "audit_log": audit_log,
            "policy_reviews": policy_reviews,
        }
