"""
One-shot, read-only introspection of Derrick's shared GCP MySQL database
(idox_appdata_derrick). Output is pasted into AGENT_SPEC.md Appendix A so the
team has a documented column mapping between the shared tables and the local
SQLite schema.

Usage:
    python db/introspect_gcp.py
"""
import os

import mysql.connector
from dotenv import load_dotenv

load_dotenv()

TABLES = ["customers", "tickets", "orders", "workflow_runs", "agent_handoffs"]


def main() -> None:
    conn = mysql.connector.connect(
        host=os.environ["GCP_MYSQL_HOST"],
        user=os.environ["GCP_MYSQL_USER"],
        password=os.environ["GCP_MYSQL_PASSWORD"],
        database=os.environ["GCP_MYSQL_DATABASE"],
        connection_timeout=int(os.environ.get("GCP_MYSQL_CONNECT_TIMEOUT", "5")),
    )
    cursor = conn.cursor()

    cursor.execute("SHOW TABLES")
    print("== SHOW TABLES ==")
    for (table,) in cursor.fetchall():
        print(f"  {table}")

    for table in TABLES:
        print(f"\n== DESCRIBE {table} ==")
        try:
            cursor.execute(f"DESCRIBE {table}")
            for row in cursor.fetchall():
                print("  " + " | ".join(str(col) for col in row))
        except mysql.connector.Error as exc:
            print(f"  (error: {exc})")

    for table in TABLES:
        print(f"\n== SAMPLE {table} (LIMIT 3) ==")
        try:
            cursor.execute(f"SELECT * FROM {table} LIMIT 3")
            cols = [d[0] for d in cursor.description]
            print("  " + " | ".join(cols))
            for row in cursor.fetchall():
                print("  " + " | ".join(str(col) for col in row))
        except mysql.connector.Error as exc:
            print(f"  (error: {exc})")

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()
