"""
Dual-backend data access layer.

All queries in this project are written once, using SQLite `?` placeholders.
At runtime the active backend is either:

  - "mysql"  — GCP Cloud SQL (shared team input tables in idox_appdata_derrick)
  - "sqlite" — local db/refund_service.db

Backend selection: DB_BACKEND env var (default "mysql"). If the MySQL probe
connection fails (network down, bad credentials, timeout), we log a warning,
emit a `backend_fallback` audit event, and degrade to SQLite for the rest of
the process. The decision is cached module-level so each query doesn't pay the
connect timeout; tests reset it via reset_backend_cache().

Connect-per-call, no pooling: demo-scale traffic, and it avoids stale
Cloud SQL connections between long-lived notebook cells.
"""
import datetime
import decimal
import os
import sys

from dotenv import load_dotenv

from db.database import get_connection as _sqlite_connection

load_dotenv()

_active_backend: str | None = None  # None = not yet decided

# Tables the triage pipeline queries (order lookup JOIN + PII cross-reference).
# The MySQL probe verifies these exist — a reachable server with the shared
# tables not yet pushed must degrade to SQLite instead of crashing mid-query.
_REQUIRED_TABLES = {"customers", "orders"}


def reset_backend_cache() -> None:
    global _active_backend
    _active_backend = None


def _mysql_config() -> dict:
    return {
        "host": os.environ.get("GCP_MYSQL_HOST", ""),
        "user": os.environ.get("GCP_MYSQL_USER", ""),
        "password": os.environ.get("GCP_MYSQL_PASSWORD", ""),
        "database": os.environ.get("GCP_MYSQL_DATABASE", ""),
        "connection_timeout": int(os.environ.get("GCP_MYSQL_CONNECT_TIMEOUT", "5")),
    }


def _notify_fallback(error: Exception) -> None:
    print(
        f"[db.backend] WARNING: MySQL unavailable ({error}); "
        "falling back to local SQLite.",
        file=sys.stderr,
    )
    try:
        # Lazy import: audit_logger depends on nothing here, but keep the
        # coupling one-way and never let audit failures break data access.
        from governance.audit_logger import log_event

        log_event(
            trace_id="system",
            event_type="backend_fallback",
            agent="system",
            payload={"from": "mysql", "to": "sqlite", "error": str(error)[:200]},
        )
    except Exception:
        pass


def active_backend() -> str:
    """Resolve and cache which backend serves queries for this process."""
    global _active_backend
    if _active_backend is not None:
        return _active_backend

    requested = os.environ.get("DB_BACKEND", "mysql").strip().lower()
    if requested != "mysql":
        _active_backend = "sqlite"
        return _active_backend

    import mysql.connector

    try:
        conn = mysql.connector.connect(**_mysql_config())
        try:
            cursor = conn.cursor()
            cursor.execute("SHOW TABLES")
            tables = {row[0] for row in cursor.fetchall()}
            cursor.close()
        finally:
            conn.close()
        missing = _REQUIRED_TABLES - tables
        if missing:
            raise RuntimeError(f"required tables missing on MySQL: {sorted(missing)}")
        _active_backend = "mysql"
    except Exception as exc:  # mysql.connector.Error, timeout, missing tables
        # Cache BEFORE notifying: _notify_fallback logs an audit event, and the
        # audit logger consults active_backend() to route remote-vs-local — if
        # the cache were still None here, that would re-probe and recurse.
        _active_backend = "sqlite"
        _notify_fallback(exc)
    return _active_backend


def _normalize_row(row: dict) -> dict:
    """
    Make MySQL rows match the SQLite/contract shape: MySQL DECIMAL columns come
    back as Decimal and DATE/DATETIME as date objects, but the interceptor's
    schema check expects amounts as int/float and dates as str. SQLite (TEXT/
    REAL columns) already returns those, so this only fires on the MySQL path.
    """
    out = {}
    for key, value in row.items():
        if isinstance(value, decimal.Decimal):
            out[key] = float(value)
        elif isinstance(value, (datetime.date, datetime.datetime)):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _run(sql: str, params: tuple):
    """Execute a query on the active backend, returning a list of dicts."""
    if active_backend() == "mysql":
        import mysql.connector

        conn = mysql.connector.connect(**_mysql_config())
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql.replace("?", "%s"), params)
            rows = [_normalize_row(r) for r in cursor.fetchall()]
            cursor.close()
        finally:
            conn.close()
        return rows

    conn = _sqlite_connection()
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    return rows


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = _run(sql, params)
    return rows[0] if rows else None


def query_all(sql: str, params: tuple = ()) -> list[dict]:
    return _run(sql, params)
