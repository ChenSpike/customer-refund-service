import uuid
from app.graph import build_graph
from db.database import GCPRepository

trace_id  = f"TRACE-{uuid.uuid4().hex[:8].upper()}"
ticket_id = f"TICKET-{uuid.uuid4().hex[:8].upper()}"

USER_ID   = "CUST-POL-001"
#MESSAGE   = "My headphones stopped working after two days. Order ORD-POL-001."
MESSAGE = "My headphones stopped working after two days. Order ORD-TEST-001."
repo = GCPRepository.from_env()
conn = repo._connect()
try:
    cursor = conn.cursor()

    # Ticket row — customer already exists in DB
    cursor.execute(
        """INSERT INTO tickets (ticket_id, customer_id, raw_text)
           VALUES (%s, %s, %s)""",
        (ticket_id, USER_ID, MESSAGE)
    )

    # Workflow run
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
    "user_id":              USER_ID,
    "message":              MESSAGE,
    "conversation_history": [],
    "request_context":      {"trace_id": trace_id, "ticket_id": ticket_id, "buggy_db": False},
    "trace_id":             trace_id,
    "ticket_id":            ticket_id,
})

print("final_outcome:  ", result.get("final_outcome"))
print("workflow_status:", result.get("workflow_status"))
print()
print("Response:")
print(result.get("response_result", {}).get("response", {}).get("body", ""))
