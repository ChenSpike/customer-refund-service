import uuid
from app.graph import build_graph
from db.database import GCPRepository

trace_id  = f"TRACE-{uuid.uuid4().hex[:8].upper()}"
ticket_id = f"TICKET-{uuid.uuid4().hex[:8].upper()}"

# Insert the workflow_runs row first so governance events can reference it
repo = GCPRepository.from_env()
conn = repo._connect()
try:
    cursor = conn.cursor()
    cursor.execute("DESCRIBE tickets")
    for row in cursor.fetchall():
        print(row)

    cursor.execute("DESCRIBE customers")
    for row in cursor.fetchall():
        print(row)

    # 1. Customer first
    cursor.execute(
        """INSERT IGNORE INTO customers (customer_id, email, full_name)
           VALUES (%s, %s, %s)""",
        ("CUST-001", "alice@example.com", "Alice Johnson")
    )
    
    # 2. Ticket
    cursor.execute(
        """INSERT INTO tickets (ticket_id, customer_id, raw_text)
           VALUES (%s, %s, %s)""",
        (ticket_id, "CUST-001", "My headphones stopped working after two days. Order ORD-001.")
    )
    
    # 3. Workflow run
    cursor.execute(
        """INSERT INTO workflow_runs (trace_id, ticket_id, status, current_agent, policy_version)
           VALUES (%s, %s, 'running', 'triage_agent', 'v1.0')""",
        (trace_id, ticket_id)
    )
    
    conn.commit()
finally:
    conn.close()

graph = build_graph()

result = graph.invoke({
    "user_id":              "CUST-001",
    "message":              "My headphones stopped working after two days. Order ORD-001.",
    "conversation_history": [],
    "request_context":      {"trace_id": trace_id, "ticket_id": ticket_id},
    "buggy_db":             False,
    "trace_id":             trace_id,
    "ticket_id":            ticket_id,
})

print("final_outcome:  ", result.get("final_outcome"))
print("workflow_status:", result.get("workflow_status"))
print()
print("Response:")
print(result.get("response_result", {}).get("response", {}).get("body", ""))
