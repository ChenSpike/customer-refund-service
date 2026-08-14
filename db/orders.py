"""Read-side access to the shared `orders`/`customers` input tables in main_db.

Kept independent of db.database.GCPRepository on purpose: that module is
write-oriented AND pulls in agents.policy (there is a latent circular import
db.database -> agents.policy -> db.pipeline_store -> agents.policy), so importing
it just to read an order is both heavy and fragile. This reads the same MySQL
settings (MYSQL_*) directly.
"""
from __future__ import annotations

import datetime
import decimal
import os
from pathlib import Path
from typing import Any

import mysql.connector
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATABASE_NAME = "main_db"

# Correct JOIN: the contact belongs to the order's owner.
_NORMAL_SQL = """
    SELECT
        o.order_id,
        o.customer_id        AS order_customer_id,
        o.product_type,
        o.purchase_date,
        o.item_status,
        o.amount_paid,
        o.prior_refund_total,
        c.customer_id        AS contact_customer_id,
        c.email              AS contact_email,
        c.full_name          AS contact_name
    FROM orders o
    JOIN customers c ON o.customer_id = c.customer_id
    WHERE o.order_id = %s
"""

# BUGGY JOIN: `!=` attaches a different customer's contact even for a valid
# order. Exposed via `buggy` so the ASI07 ownership check has a deterministic
# leak to catch (governance: agents.triage.governance_node).
_BUGGY_SQL = """
    SELECT
        o.order_id,
        o.customer_id        AS order_customer_id,
        o.product_type,
        o.purchase_date,
        o.item_status,
        o.amount_paid,
        o.prior_refund_total,
        c.customer_id        AS contact_customer_id,
        c.email              AS contact_email,
        c.full_name          AS contact_name
    FROM orders o
    JOIN customers c ON c.customer_id != o.customer_id
    WHERE o.order_id = %s
    LIMIT 1
"""


def _config() -> dict[str, Any]:
    # Same env files and keys as GCPRepository, loaded without importing it.
    for env_file in (_REPO_ROOT / ".env",
                     _REPO_ROOT / "agents" / "policy" / ".env",
                     _REPO_ROOT / "database" / ".env"):
        if env_file.exists():
            load_dotenv(env_file)
    return {
        "host": os.environ["MYSQL_HOST"],
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.environ["MYSQL_USER"],
        "password": os.environ["MYSQL_PASSWORD"],
        "database": os.getenv("MYSQL_DATABASE", _DEFAULT_DATABASE_NAME),
        "connection_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "10")),
    }


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """MySQL returns DECIMAL as Decimal and DATE as date; the ASI07 schema check
    expects amounts as float and dates as str."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, decimal.Decimal):
            out[key] = float(value)
        elif isinstance(value, (datetime.date, datetime.datetime)):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def get_order(order_id: str, buggy: bool = False) -> dict[str, Any] | None:
    connection = mysql.connector.connect(**_config())
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(_BUGGY_SQL if buggy else _NORMAL_SQL, (order_id,))
        row = cursor.fetchone()
        cursor.close()
    finally:
        connection.close()
    return _normalize(row) if row else None
