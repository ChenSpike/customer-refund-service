# Setup Instructions

Follow these steps to get the AI Governance capstone running locally.

## Prerequisites

- Python 3.9+
- Node.js 16+
- MySQL CLI
- Git

## Step 1: Clone & Setup Environment

```bash
cd /Users/sherinechally/Desktop/UCLA_works/summer_26/aigov_capstone

# Copy environment template
cp .env.example .env

# Verify .env has real values filled in (get them from your team's secret manager)
cat .env
```

## Step 2: Initialize Database

```bash
# Create database schema (loads host/user/password from .env)
source .env && mysql -h "$DB_HOST" -u "$DB_USER" -p < schema.sql
# When prompted, enter the password from your team's secret manager

# Verify tables were created
mysql -h "$DB_HOST" -u "$DB_USER" -p aigov_refund -e "SHOW TABLES;"
```

Should output:
```
+------------------+
| Tables_in_aigov_refund |
+------------------+
| agent_handoffs   |
| audit_log        |
| customers        |
| governance_events|
| human_approvals  |
| orders           |
| refund_transactions |
| tickets          |
| workflow_runs    |
+------------------+
```

## Step 3: Backend Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Test database connection (reads credentials from .env)
python -c "
import os
from dotenv import load_dotenv
from db_service import DBService
load_dotenv()
db = DBService(os.getenv('DB_HOST'), os.getenv('DB_USER'), os.getenv('DB_PASSWORD'), os.getenv('DB_NAME', 'main_db'))
print('✓ Database connected')
"

# Start FastAPI server
python main.py
```

You should see:
```
Uvicorn running on http://0.0.0.0:8000
```

Keep this terminal open.

## Step 4: Frontend Setup (New Terminal)

```bash
cd frontend

# Install dependencies
npm install

# Start React development server
npm start
```

You should see:
```
webpack compiled successfully
Compiled successfully!

You can now view aigov-dashboard in your browser.
  Local:            http://localhost:3000
```

## Step 5: Verify Everything Works

1. **Check API Health**:
   ```bash
   curl http://localhost:8000/health
   # {"status":"ok"}
   ```

2. **Open Dashboard**:
   - Navigate to http://localhost:3000 in your browser
   - You should see the AI Governance Dashboard with tabs

3. **Check API Docs**:
   - Visit http://localhost:8000/docs
   - Swagger UI shows all available endpoints

## Step 6: Test Agent Integration

In a new terminal:

```bash
python3 << 'EOF'
import requests
import json

API_URL = "http://localhost:8000"

# Start workflow
print("Starting workflow...")
response = requests.post(f"{API_URL}/api/workflows/start", json={
    "ticket_id": "test_ticket_001",
    "policy_version": "1.0"
})
trace_id = response.json()["trace_id"]
print(f"✓ Workflow started: {trace_id}")

# Log handoff
print("Logging handoff...")
requests.post(f"{API_URL}/api/handoffs", json={
    "trace_id": trace_id,
    "from_agent": "triage_agent",
    "to_agent": "policy_agent",
    "input_json": {"customer_id": "cust_001", "order_id": "order_001"},
    "output_json": {"policy_decision": "approve", "confidence": 0.95}
})
print("✓ Handoff logged")

# Log governance event
print("Logging governance event...")
requests.post(f"{API_URL}/api/governance-events", json={
    "trace_id": trace_id,
    "agent": "policy_agent",
    "owasp_category": "ASI01",
    "interceptor_action": "allow",
    "trigger_score": 0.45
})
print("✓ Governance event logged")

# Log audit event
print("Logging audit event...")
requests.post(f"{API_URL}/api/audit-log", json={
    "trace_id": trace_id,
    "event_type": "handoff",
    "agent": "triage_agent",
    "payload": {"step": 1}
})
print("✓ Audit event logged")

# Query audit log
print("\nQuerying audit log...")
response = requests.get(f"{API_URL}/api/audit-log/query?trace_id={trace_id}")
logs = response.json()
print(f"✓ Found {len(logs)} audit entries")
print(json.dumps(logs, indent=2, default=str))

EOF
```

Expected output:
```
Starting workflow...
✓ Workflow started: <uuid>
✓ Handoff logged
✓ Governance event logged
✓ Audit event logged

Querying audit log...
✓ Found 1 audit entries
[...]
```

If you see errors, check:
- FastAPI is running (`python main.py`)
- Database is accessible (`mysql -h 34.132.101.99 -u root -p`)
- Frontend is running (`npm start` from frontend/)

## Step 7: Verify Dashboard Updates

1. Go back to dashboard (http://localhost:3000)
2. Click on **Audit Log** tab
3. You should see the test workflow data appear
4. Refresh to see updates (currently polling every 5 seconds)

## Troubleshooting

### "Connection refused to 34.132.101.99:3306"
- Check network connectivity: `ping 34.132.101.99`
- Verify MySQL is running: `mysql -h 34.132.101.99 -u root -p -e "SELECT 1"`
- Check firewall rules (GCP MySQL may require IP whitelist)

### "ModuleNotFoundError: No module named 'fastapi'"
- Run: `pip install -r requirements.txt`

### "npm ERR! 404 Not Found"
- Clear npm cache: `npm cache clean --force`
- Delete node_modules: `rm -rf node_modules package-lock.json`
- Reinstall: `npm install`

### Dashboard shows "API Offline"
- Ensure FastAPI is running on port 8000
- Check CORS is enabled (should be in main.py)
- Open browser console (F12) for specific errors

## Next Steps

1. **Review AGENT_INTEGRATION.md** to understand how to integrate agents
2. **Implement agents** (triage, policy, response, refund)
3. **Implement governance interceptor** to apply policy rules
4. **Design demo scenarios** for ASI06 (prompt injection) and ASI07 (data leakage)
5. **Test failure injection** to verify governance responses

## Common Workflows

### Start Fresh Database
```bash
# Drop and recreate database
mysql -h 34.132.101.99 -u root -p -e "DROP DATABASE aigov_refund;"
mysql -h 34.132.101.99 -u root -p < schema.sql
```

### View Database State
```bash
# Connect to database
mysql -h 34.132.101.99 -u root -p aigov_refund

# View all tables
SHOW TABLES;

# Count rows in each table
SELECT COUNT(*) FROM workflow_runs;
SELECT COUNT(*) FROM audit_log;
SELECT COUNT(*) FROM governance_events;
```

### Stop Services
```bash
# Stop FastAPI (Ctrl+C in terminal)
# Stop React (Ctrl+C in terminal)

# Verify ports are free
lsof -i :8000
lsof -i :3000
```

## Development Tips

- **Hot reload**: Both FastAPI and React support hot reload during development
- **API Docs**: Always check http://localhost:8000/docs for endpoint signatures
- **Database Queries**: Use `db_service.py` methods, don't write SQL directly
- **Logging**: Check terminal output for both backend and frontend errors
- **Browser Console**: F12 in React app for frontend errors

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                  React Dashboard (3000)                   │
│     [Overview] [Workflows] [Governance] [HITL] [Audit]   │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP Axios
                         ▼
┌─────────────────────────────────────────────────────────┐
│             FastAPI Backend (8000)                        │
│  /api/workflows/  /api/handoffs/  /api/governance-events/│
│  /api/approvals/  /api/audit-log/  /api/refund-txns/    │
└────────────────────────┬────────────────────────────────┘
                         │ SQL
                         ▼
┌─────────────────────────────────────────────────────────┐
│         MySQL Database (GCP 34.132.101.99)               │
│  [workflow_runs] [agent_handoffs] [governance_events]   │
│  [human_approvals] [audit_log] [refund_transactions]    │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│             Agents (LangGraph) - Coming Soon              │
│  [Triage Agent] [Policy Agent] [Response] [Refund]      │
│           ↓ HTTP Requests ↓                              │
│      FastAPI Backend (8000)                              │
└─────────────────────────────────────────────────────────┘
```

---

**All set! Start developing. See README.md for detailed architecture info.**
