from db.database import get_connection, init_db

CUSTOMERS = [
    ("CUST-001", "Alice Johnson", "alice@example.com"),
    ("CUST-002", "Bob Smith",     "bob@example.com"),
    ("CUST-003", "Carol White",   "carol@example.com"),
]

ORDERS = [
    ("ORD-001", "CUST-001", "Electronics", "2025-01-15", "delivered", 299.99, 0.0),
    ("ORD-002", "CUST-001", "Clothing",    "2025-02-20", "returned",   49.99, 49.99),
    ("ORD-003", "CUST-002", "Books",       "2025-03-10", "delivered",  24.99, 0.0),
    ("ORD-004", "CUST-002", "Electronics", "2025-04-05", "damaged",   199.99, 0.0),
    ("ORD-005", "CUST-003", "Home",        "2025-05-01", "delivered",  89.99, 0.0),
]


def seed() -> None:
    init_db()
    conn = get_connection()
    conn.executemany("INSERT OR IGNORE INTO customers VALUES (?,?,?)", CUSTOMERS)
    conn.executemany("INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,?,?)", ORDERS)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    seed()
    print("Database seeded.")
