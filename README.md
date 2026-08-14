# AI Governance Capstone - Refund Workflow Governance

A LangGraph-based multi-agent workflow with a comprehensive governance layer that detects OWASP agentic AI risks in real time, intervenes before damage spreads, and produces a queryable audit record.

## Agent Pipeline Architecture

An automated, state-driven multi-agent workflow for evaluating and processing e-commerce customer refund requests. This is the current design for the agent pipeline (`agents/`, `app/`, `governance/`, `db/`, `tools/`), the dashboard (`main.py`, `db_service.py`, `case_service.py`, `frontend/`) infers case status and pipeline progress from the rows this pipeline writes to `main_db`.

### System Workflow Pipeline

```text
                                    [ User Input ]
                                          |
                                          v
                                   [ Triage Agent ]
                                          |
                                          v
                                [ Triage Governance ]
                                          |
                                          v
                                  [ Triage Router ]
                                          |
               +--------------------------+--------------------------+
               | (governance block)       | (data missing)           | (allow & complete)
               v                          |                          v
     [ Human Approval ]                   |                   [ Policy Agent ]
               |                          |                          |
               |                          |                          v
               |                          |                [ Policy Governance ]
               |                          |                          |
               |                          |                          v
               |                          |                  [ Policy Router ]
               |                          |                          |
               |                          |        +-----------------+-----------------+
               |                          |        | (approve)       | (deny / info)   | (block / review)
               |                          |        v                 |                 |
               |                          |  [ Refund Agent ]        |                 |
               |                          |        |                 |                 |
               |                          v        v                 |                 v
               +----------------------> [ Response Agent ] <---------+---------[ Human Approval ]
                                          |
                                          v
                                       [ END ]
```

### Core State Table

Keep only the core workflow state.

| State Field | Type | Description | Main Stages |
|---|---|---|---|
| `user_id` | `str` | User who submitted the request | Input, Governance |
| `message` | `str` | Current user message | Triage |
| `conversation_history` | `list` | Prior conversation context | Triage |
| `request_context` | `dict` | External metadata such as channel, locale, or source | Input |
| `trace_id` | `str` | End-to-end workflow trace ID | All stages |
| `ticket_id` | `str` | Case or ticket identifier | All stages |
| `current_stage` | `str` | Current workflow node | All stages |
| `workflow_status` | `str` | Overall workflow status such as `running`, `waiting_user`, `waiting_human`, or `completed` | All stages |
| `missing_fields` | `list[str]` | Required fields that are still missing | Triage, Response |
| `user_action_required` | `bool` | Whether the workflow is waiting for user input | Triage Router, Response |
| `human_review_required` | `bool` | Whether the case must be reviewed by a human | Governance, Policy Router |
| `final_outcome` | `str` | Final case result such as `approved`, `denied`, `need_info`, or `manual_review` | End stages |
| `requested_order_id` | `str` | Order ID extracted from the user message | Triage |
| `clarification_question` | `str` | Follow-up question sent back to the user | Response |
| `order_lookup_result` | `dict` | Raw order data returned by the lookup tool | Triage, Audit |
| `triage_output` | `dict` | Structured case payload produced by triage | Triage Governance, Policy |
| `governance_result` | `dict` | Governance decision with allow or block result | Routers, Human Approval |
| `risk_flags` | `dict` | Consolidated risk signals such as PII, content filter, injection, or tool misuse | Governance |
| `policy_decision` | `dict` | Final policy decision for the refund case | Policy Governance, Routers, Downstream |
| `policy_context` | `dict` | Supporting policy metadata such as rule version or retrieval context | Policy, Audit |
| `refund_result` | `dict` | Output from the refund execution branch | Refund Agent |
| `response_result` | `dict` | Output from the user response branch | Response Agent |
| `human_review` | `dict` | Output from the human approval branch | Human Approval |
| `errors` | `list[dict]` | Collected workflow errors or exceptions | All stages |
| `audit_trail` | `list[dict]` | Audit records written across important steps | Governance, Persistence |
| `snapshots` | `list[dict]` | State snapshots for tracing and replay | Middleware, Observability |
| `llm_input_tokens` | `int` | Total input tokens used by LLM calls | Triage, Policy |
| `llm_output_tokens` | `int` | Total output tokens used by LLM calls | Triage, Policy |

Removed from the core state table:

- `case` -> use `request_context`
- `awaiting_info` -> use `missing_fields` and `user_action_required`
- `awaiting_order_id` -> use `missing_fields` and `user_action_required`
- `next_agent` -> routing belongs to routers
- `buggy_db` -> test-only, not part of core workflow state
- `content_filter_blocked` -> use `risk_flags`
- `injection_flag` -> use `risk_flags`

### Stage Flow Using The New State Table

#### 1. User Input

This stage prepares the minimum context for the workflow.

- Input fields: `user_id`, `message`, `conversation_history`, `request_context`
- System fields created or continued here: `trace_id`, `ticket_id`
- Initial control state:
        - `current_stage = "triage"`
        - `workflow_status = "running"`
        - `missing_fields = []`
        - `user_action_required = False`
        - `human_review_required = False`
        - `final_outcome = ""`

#### 2. Triage Agent

Triage converts the raw user message into structured case data.

Reads:

- `user_id`
- `message`
- `conversation_history`
- `request_context`
- `trace_id`
- `ticket_id`

Writes:

- `current_stage = "triage"`
- `requested_order_id`
- `order_lookup_result`
- `triage_output`
- `clarification_question`
- `missing_fields`
- `user_action_required`
- `workflow_status`
- `conversation_history`
- `llm_input_tokens`
- `llm_output_tokens`
- `errors` if extraction or lookup fails unexpectedly

Typical results:

- If order data is complete:
        - `triage_output` is ready
        - `missing_fields = []`
        - `user_action_required = False`
        - `workflow_status = "running"`
- If data is missing:
        - `missing_fields = ["order_id"]`
        - `user_action_required = True`
        - `clarification_question` contains the follow-up question
        - `workflow_status = "waiting_user"`

#### 3. Triage Governance

This stage checks whether the triage result is safe before policy evaluation.

Reads:

- `triage_output`
- `trace_id`
- `ticket_id`
- `user_id`

Writes:

- `current_stage = "triage_governance"`
- `governance_result`
- `risk_flags`
- `audit_trail`
- `snapshots`
- `human_review_required` when blocked
- `workflow_status` when blocked

Typical results:

- If safe:
        - `governance_result.status = "allow"`
- If blocked:
        - `governance_result.status = "block"`
        - `human_review_required = True`
        - `workflow_status = "waiting_human"`

#### 4. Triage Router

This stage does not create business data. It only reads control state and selects the next node.

Reads:

- `governance_result`
- `missing_fields`
- `user_action_required`
- `triage_output`

Routing rules:

- If `governance_result.status == "block"` -> `Human Approval`
- If `user_action_required == True` -> `Response Agent`
- If `triage_output` is complete -> `Policy Agent`

#### 5. Policy Agent

Policy evaluates the structured case and decides the refund outcome.

Reads:

- `triage_output`
- `trace_id`
- `ticket_id`

Writes:

- `current_stage = "policy"`
- `policy_decision`
- `policy_context`
- `llm_input_tokens`
- `llm_output_tokens`
- `errors` if policy generation fails

Typical results:

- `policy_decision.decision` becomes one of:
        - `approve`
        - `partial_refund`
        - `deny`
        - `request_info`
        - `manual_review`

#### 6. Policy Governance

This stage checks whether the policy result is safe and valid.

Reads:

- `policy_decision`
- `policy_context`
- `trace_id`
- `ticket_id`
- `user_id`

Writes:

- `current_stage = "policy_governance"`
- `governance_result`
- `risk_flags`
- `audit_trail`
- `snapshots`
- `human_review_required` when blocked
- `workflow_status` when blocked

Typical results:

- If safe:
        - `governance_result.status = "allow"`
- If blocked:
        - `governance_result.status = "block"`
        - `human_review_required = True`
        - `workflow_status = "waiting_human"`

#### 7. Policy Router

This stage reads the policy result and sends the workflow to the correct final branch.

Reads:

- `governance_result`
- `policy_decision`

Routing rules:

- If `governance_result.status == "block"` -> `Human Approval`
- If `policy_decision.decision in {"approve", "partial_refund"}` -> `Refund Agent`
- If `policy_decision.decision in {"deny", "request_info"}` -> `Response Agent`
- If `policy_decision.decision == "manual_review"` -> `Human Approval`

#### 8. Refund Agent

This is the execution branch for approved refund outcomes.

Reads:

- `policy_decision`
- `trace_id`
- `ticket_id`

Writes:

- `current_stage = "refund_agent"`
- `refund_result`
- `final_outcome`
- `workflow_status = "completed"`

Typical results:

- If fully approved: `final_outcome = "approved"`
- If partially approved: `final_outcome = "partial_refund"`

#### 9. Response Agent

This is the user-facing response branch. It is used for missing data and final business responses.

Reads:

- `clarification_question`
- `missing_fields`
- `user_action_required`
- `policy_decision`

Writes:

- `current_stage = "response_agent"`
- `response_result`
- `final_outcome`
- `workflow_status`

Typical results:

- If waiting for user data:
        - `response_result` asks for the missing information
        - `final_outcome = "need_info"`
        - `workflow_status = "waiting_user"`
- If policy denied:
        - `response_result` explains the denial
        - `final_outcome = "denied"`
        - `workflow_status = "completed"`
- If policy requested more info:
        - `response_result` asks for the required information
        - `final_outcome = "need_info"`
        - `workflow_status = "waiting_user"`

#### 10. Human Approval

This is the manual review branch for blocked or ambiguous cases.

Reads:

- `governance_result`
- `policy_decision`
- `trace_id`
- `ticket_id`

Writes:

- `current_stage = "human_approval"`
- `human_review`
- `human_review_required = True`
- `final_outcome = "manual_review"`
- `workflow_status = "waiting_human"`

Typical result:

- The workflow stops and waits for a human reviewer to take over. In the dashboard, the reviewer's own decision is one of `approve` / `partial_refund` / `deny` / `request_info`, mirroring the Policy Agent's decision vocabulary, and resumes the paused pipeline accordingly (see `db_service.apply_case_decision`).

### Core Development Rules

1. **Agents**: Return business data patches only. Never include routing fields (`next_agent`) or database calls in agent nodes.
2. **Routers**: Read shared state and return the string key of the next target node. No business logic transformation inside routers.
3. **Governance**: Operates as standalone nodes (Triage Governance, Policy Governance). Runs security and compliance checks, logs audit events, and outputs `status: allow/block`.
4. **Middlewares**: Capture execution traces, token metrics, and state snapshots asynchronously.
5. **Tools**: Universal SDK wrappers for external APIs (Azure, DBs, RAG). Agents and governance modules must access external services via `tools/`.

### Agent Pipeline Directory Structure

```text
C:.
├─ agents/                # LLM reasoning nodes (State patch outputs only)
│  ├─ triage/             # Intent recognition & order facts extraction
│  ├─ policy/             # Refund rule evaluation & business decision
│  ├─ refund/             # Execution layer for payment/refund processing
│  └─ response/           # Uniform outbound text generation
├─ app/                   # Graph orchestration & runtime control
│  ├─ state.py            # Shared State Schema (Context, Payload, Control)
│  ├─ graph.py            # StateGraph definition & node wiring
│  ├─ routers/            # Conditional edges (triage_router, policy_router)
│  └─ middlewares/        # Async tracing & observability handlers
├─ governance/            # Security & compliance checks
│  ├─ node.py             # Entrypoint executing configured checkers
│  ├─ checkers.py         # Strategy implementations (Injection, PII, Rules)
│  └─ audit_logger.py     # Structured envelope audit log writer
├─ db/                    # Persistence layer
│  ├─ database.py         # DB connection engine & session management
│  ├─ pipeline_store.py   # State checkpointing & human approval persistence
│  └─ migrations/         # SQL schema migration scripts
├─ tools/                 # External service clients & utility functions
│  ├─ azure_client.py     # Azure OpenAI SDK wrapper
│  ├─ llm_helpers.py      # Structured output parser & retry wrappers
│  ├─ order_lookup.py     # Internal OMS/CRM data retrieval
│  └─ policy_retriever.py # RAG vector store interface
└─ tests/                 # Unit, integration, and live tests
```

## Project Structure

```
aigov_capstone/
├── schema.sql                 # Database schema (MySQL)
├── db_service.py             # Database service layer
├── main.py                   # FastAPI backend
├── requirements.txt          # Python dependencies
├── AGENT_INTEGRATION.md      # Agent integration guide
├── DASHBOARD.md              # What each dashboard tab shows and where its data comes from
├── frontend/
│   ├── package.json
│   ├── src/
│   │   ├── index.js
│   │   ├── App.jsx
│   │   ├── App.css
│   │   ├── pages/
│   │   │   ├── Dashboard.jsx
│   │   │   ├── Workflows.jsx
│   │   │   ├── GovernanceEvents.jsx
│   │   │   ├── HITLQueue.jsx
│   │   │   └── AuditLog.jsx
│   │   └── styles/
│   │       └── pages.css
│   └── public/
│       └── index.html
└── .env.example
```

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
```

Fill in `DB_HOST`, `DB_USER`, and `DB_PASSWORD` in `.env` with the shared GCP MySQL instance credentials (`DB_NAME=main_db`), get these from your team's secret manager, not from docs or chat.

### 2. Initialize the database (only if `main_db` doesn't already have the schema)

```bash
mysql -h $DB_HOST -u $DB_USER -p main_db < schema.sql
# you'll be prompted for the password, get it from your team's secret manager
```

This is idempotent (`CREATE TABLE` / `CREATE DATABASE IF NOT EXISTS`), safe to re-run. Skip this step if the tables already exist; running it won't touch existing rows.

### 3. Start the backend (FastAPI)

```bash
pip install -r requirements.txt
python main.py
# Server runs on http://localhost:8000
# Interactive API docs: http://localhost:8000/docs
```

### 4. Start the frontend (React)

```bash
cd frontend
npm install
npm start
# Dashboard runs on http://localhost:3000
```

### 5. Verify

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Then open http://localhost:3000, the sidebar should show "Connected" and the Overview tab should populate with real numbers within a few seconds.

## Architecture

### Database Layer (MySQL/GCP)

**9 Tables** tracking the full workflow lifecycle:

- **customers** - Who is asking for a refund (PII)
- **tickets** - Inbound refund requests
- **orders** - Mock order database for lookups
- **workflow_runs** - One row per LangGraph execution (trace_id)
- **agent_handoffs** - JSON in/out at each agent boundary
- **governance_events** - OWASP flags, quarantine holds, scores
- **human_approvals** - HITL queue (pause & wait)
- **audit_log** - Append-only compliance trail
- **refund_transactions** - Financial execution records

### Backend (FastAPI)

RESTful API endpoints for agents and dashboard:

- **Workflow Management**: `/api/workflows/start`, `/api/workflows/{trace_id}`, `/api/workflows/{trace_id}/status`
- **Agent Handoffs**: `/api/handoffs`, `/api/workflows/{trace_id}/handoffs`
- **Governance Events**: `/api/governance-events`, `/api/workflows/{trace_id}/governance`
- **HITL Queue**: `/api/approvals`, `/api/approvals/pending`, `/api/approvals/{approval_id}`
- **Audit Log**: `/api/audit-log`, `/api/audit-log/query`
- **Refund Transactions**: `/api/refund-transactions`, `/api/refund-transactions/{transaction_id}`
- **Dashboard**: `/api/dashboard/stats`

### Frontend (React)

**5 tabs**, grouped in the sidebar as PLATFORM / GOVERNANCE / ADMINISTRATION. Each polls its API endpoint on an interval (no websockets yet). See [DASHBOARD.md](./DASHBOARD.md) for the full breakdown of what each tab shows, its data source, and known gaps.

1. **Overview**: stat cards (active workflows, governance events, compliance rating, pending approvals), an audit event volume chart, and a risk-by-agent table. Source: `/api/dashboard/stats` + `/api/audit-log/query`.
2. **Live Monitoring**: list of workflow traces derived from the audit log, grouped by `trace_id`. Source: `/api/audit-log/query`.
3. **Violations**: every governance interceptor check (OWASP category, agent, action, trigger score, offending content), filterable by category/action. Source: `/api/governance-events`.
4. **Approvals**: the HITL queue: pending human approvals, each showing the OWASP context of the governance event that triggered it (if any), with Approve/Reject buttons. Source: `/api/approvals/pending`.
5. **Audit Logs**: the raw append-only compliance trail, filterable by trace/event type. Source: `/api/audit-log/query`.

## Agent Integration

Agents communicate via the FastAPI backend. **See [AGENT_INTEGRATION.md](./AGENT_INTEGRATION.md) for detailed examples.**

### Simple Pattern

```python
import requests

API_URL = "http://localhost:8000"

# 1. Start workflow
response = requests.post(f"{API_URL}/api/workflows/start", 
  json={"ticket_id": "ticket_123"})
trace_id = response.json()["trace_id"]

# 2. Do agent work
output = my_agent_logic(input_data)

# 3. Log handoff
requests.post(f"{API_URL}/api/handoffs", json={
  "trace_id": trace_id,
  "from_agent": "my_agent",
  "to_agent": "next_agent",
  "input_json": input_data,
  "output_json": output
})

# 4. Update status
requests.put(f"{API_URL}/api/workflows/{trace_id}/status", 
  json={"status": "running", "current_agent": "next_agent"})
```

## OWASP Risk Categories

The governance layer monitors these agentic AI risks:

| Category | Operational Form | Enforcement |
|----------|-----------------|-------------|
| **ASI01** Goal Hijack | Agent pursues wrong goal confidently | Embedding similarity, prompt injection detection |
| **ASI02** Tool Misuse | Tool returns unexpected format; agent proceeds | Tool schema validation |
| **ASI03** Identity & Privilege Abuse | Agent reaches unintended services | Allowlist/denylist validation |
| **ASI07** Data Leakage | Cross-customer PII in responses | Regex/schema validation for PII patterns |
| **ASI08** Excessive Autonomy | Financial transactions without approval | Mandatory human confirmation (pause & wait) |

## Demo Scenarios

### Scenario 1: Adversarial - Prompt Injection (ASI06 + ASI01)

**Setup**: Customer ticket contains embedded instruction to ignore policy and approve all refunds.

**Detection**: 
- Input sanitization detects instruction pattern
- Policy agent embedding similarity check (score > 0.65) flags goal drift

**Response**: Output quarantined, downstream agents paused, dashboard alert fired

### Scenario 2: Operational - Data Leakage (ASI07)

**Setup**: Triage agent DB query bug returns foreign customer's email in state object.

**Detection**: Governance interceptor regex/schema validation detects cross-customer pattern

**Response**: State routed to human approval before policy agent executes

## Running the Demo

1. **Populate test data**: Load sample tickets/customers into database
2. **Trigger scenario**: Submit test ticket containing prompt injection
3. **Observe governance**: Dashboard shows real-time detection and intervention
4. **Query audit log**: Audience asks questions about what happened; query system answers live

Example query:
```
"What did the policy agent do 2 minutes ago and what was the governance judgment?"
→ Audit log returns exact handoff JSON + governance event with OWASP category + trigger score
```

## Key Design Decisions

### MySQL Over Neo4j
- GCP MySQL already provisioned and available
- Simpler schema for MVP; graph db adds complexity without immediate benefit
- Can upgrade to graph DB later for advanced traversal queries

### FastAPI Over Flask
- Better performance for concurrent agent requests
- Built-in OpenAPI docs for agent developers
- Async support for future webhook callbacks

### React Dashboard
- Team collaboration: easier for multiple people to work on UI
- Real-time updates with polling / future websocket support
- Professional tooling (components, state management, deployment)

## Testing

### Unit Tests (TODO)

```bash
pytest tests/ -v
```

### Integration Tests (Manual)

1. Start backend + frontend
2. Run AGENT_INTEGRATION.md examples
3. Verify data appears in audit log
4. Query with filters

## Environment Variables

See `.env.example` for the full template. Get real values from your team's secret manager:

```bash
# Database
DB_HOST=your-db-host
DB_USER=your-db-user
DB_PASSWORD=your-db-password
DB_NAME=main_db

# FastAPI
FASTAPI_ENV=development
DEBUG=True
API_HOST=0.0.0.0
API_PORT=8000

# Frontend
REACT_APP_API_URL=http://localhost:8000

# LangGraph / OpenAI
OPENAI_API_KEY=your-key-here
```

## Deployment

### Backend (FastAPI + Gunicorn)

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:8000 main:app
```

### Frontend (React Build + Serve)

```bash
cd frontend
npm run build
# Deploy frontend/build to static file hosting or CDN
```

### Database (GCP MySQL)

- Already configured, get the host/credentials from your team's secret manager
- Ensure firewall allows connections from API server

## Next Steps

1. **Implement governance interceptor** - Runs after each agent, applies policy rules
2. **Add embedding similarity detection** - For ASI01 goal drift
3. **Build failure injection tests** - Verify governance responds correctly under trigger conditions
4. **Deploy to demo environment** - Ensure live performance under audience questions
5. **Advanced features** (if time):
   - LLM-as-judge for semantic-level violations
   - Tamper-evident audit log (hash chain)
   - Cross-agent authorization tracing

## Questions?

See [AGENT_INTEGRATION.md](./AGENT_INTEGRATION.md) for agent-specific examples.

API documentation available at `http://localhost:8000/docs` when backend is running.
