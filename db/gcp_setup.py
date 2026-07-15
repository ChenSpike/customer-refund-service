"""
One-time setup for Jenny's own triage input database on GCP Cloud SQL.

Derrick's idox_appdata_derrick is Policy-Agent-shaped: its customers table
dropped the contact PII columns (no phone, name renamed) that the Triage
Agent's ASI07 leak demo depends on. Rather than pollute his stable dataset,
we create our own database with the full contact schema, mirroring the local
SQLite tables so Order_Database_Lookup works identically on both backends.

Usage:
    python db/gcp_setup.py
"""
import os

import mysql.connector
from dotenv import load_dotenv

from db.seed import CUSTOMERS, ORDERS

load_dotenv()

# Hardcoded on purpose: this script DROPs and re-creates tables, so it must
# only ever touch our own throwaway sandbox DB — never the shared main_db
# (live pipeline writes to main_db go through db/pipeline_store.py).
DB_NAME = "idox_triage_appdata_jenny"

_CUSTOMERS_DDL = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id VARCHAR(64) PRIMARY KEY,
    full_name   VARCHAR(255) NOT NULL,
    email       VARCHAR(255) NOT NULL
)
"""

_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id           VARCHAR(64) PRIMARY KEY,
    customer_id        VARCHAR(64) NOT NULL,
    product_type       VARCHAR(100) NOT NULL,
    purchase_date      DATE NOT NULL,
    item_status        VARCHAR(32) NOT NULL,
    amount_paid        DECIMAL(10,2) NOT NULL,
    prior_refund_total DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id),
    CONSTRAINT chk_item_status CHECK
        (item_status IN ('delivered','damaged','returned','unknown'))
)
"""


def _server_config() -> dict:
    return {
        "host": os.environ["GCP_MYSQL_HOST"],
        "user": os.environ["GCP_MYSQL_USER"],
        "password": os.environ["GCP_MYSQL_PASSWORD"],
        "connection_timeout": int(os.environ.get("GCP_MYSQL_CONNECT_TIMEOUT", "5")),
    }


def main() -> None:
    # 1. Create the database (no DB selected yet).
    conn = mysql.connector.connect(**_server_config())
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
    cur.close()
    conn.close()
    print(f"database ready: {DB_NAME}")

    # 2. Create tables and seed inside it. Drop first so schema changes (e.g.
    #    dropping phone / renaming full_name) take effect on re-runs — this is
    #    our own throwaway triage DB, re-seeded every time.
    conn = mysql.connector.connect(database=DB_NAME, **_server_config())
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS orders")
    cur.execute("DROP TABLE IF EXISTS customers")
    cur.execute(_CUSTOMERS_DDL)
    cur.execute(_ORDERS_DDL)
    cur.executemany(
        "INSERT IGNORE INTO customers (customer_id, full_name, email) "
        "VALUES (%s, %s, %s)",
        CUSTOMERS,
    )
    cur.executemany(
        "INSERT IGNORE INTO orders (order_id, customer_id, product_type, "
        "purchase_date, item_status, amount_paid, prior_refund_total) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        ORDERS,
    )
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM customers")
    n_cust = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders")
    n_ord = cur.fetchone()[0]
    cur.close()
    conn.close()
    print(f"seeded: customers={n_cust}, orders={n_ord}")


if __name__ == "__main__":
    main()
