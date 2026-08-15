# Customer Refund Service — Final Demo

An automated, state-driven multi-agent workflow for evaluating and processing e-commerce customer refund requests. The `final` branch is a standalone demo project: its canonical fixture, MySQL schema and guarded administrator, workflow runner, dashboard API/UI, refund API/UI, and tests all live in this repository.

The only root demo records are `demo01` through `demo20`, defined in [`database/fixtures/demo_cases.json`](database/fixtures/demo_cases.json). The fixed policy evaluation date is `2026-07-01`, so the benchmark does not change with the wall clock.

## Standalone Setup

Run commands from the repository root. Python 3.12 and Node.js 24 were used for final verification; Python 3.11+ and Node.js 20+ are the supported setup targets.

### Python

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Create a private environment file and replace every placeholder. Never commit `.env` or credentials.

```powershell
Copy-Item .env.example .env
```

Live workflow and dashboard operations require the Azure OpenAI and MySQL settings shown in [`.env.example`](.env.example). Keep `MYSQL_DATABASE=final`. The administrator also accepts `--env <ignored-file>` or `IDOX_DB_ENV` when credentials must live elsewhere.

### Dashboard frontend

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..
```

`npm ci` uses the committed lockfile. The development server command appears below.

## GCP MySQL `final` Database

The canonical schema contains ten application tables. `customers`, `orders`, `tickets`, and `workflow_runs` each receive exactly 20 roots; all output tables start empty. The administrator validates the schema, exact IDs, foreign keys, allowed trace IDs, and orphan counts.

First test server reachability. This read-only command works whether or not `final` already exists:

```powershell
.\.venv\Scripts\python.exe -m db.admin doctor --database final
```

For a new database, create the database/schema and seed the canonical fixture:

```powershell
.\.venv\Scripts\python.exe -m db.admin create --database final --confirm final
.\.venv\Scripts\python.exe -m db.admin seed --database final --confirm final
.\.venv\Scripts\python.exe -m db.admin verify --database final --phase baseline
```

`create` fails closed if `final` already exists, and `seed` requires all ten application tables to be empty. Do not rerun either command against an existing populated database. To retain the 20 root cases while deleting only their generated handoffs, audits, governance events, approvals, policy reviews, and refund transactions, use the guarded reset:

```powershell
.\.venv\Scripts\python.exe -m db.admin reset --database final --confirm final
```

After live execution, use runtime verification; unlike baseline verification, output tables may be populated:

```powershell
.\.venv\Scripts\python.exe -m db.admin verify --database final --phase runtime
```

Every mutating administrator command requires both `--database final` and `--confirm final`. Reset also refuses to act if it finds a non-demo trace or if the root workflow set is not exactly `demo01`–`demo20`.

## Run the 20 Cases

Offline mode is the safe default. It uses deterministic fixture outcomes and makes no MySQL, Azure, or network call:

```powershell
.\.venv\Scripts\python.exe main.py list
.\.venv\Scripts\python.exe main.py run demo01
.\.venv\Scripts\python.exe main.py --workers 2 run-all
.\.venv\Scripts\python.exe main.py --workers 2 --json run-all
```

Live mode invokes the state-driven graph, Azure OpenAI, and the seeded GCP database. The runner verifies the selected database and exact 20-root allowlist before each case. Two workers give bounded concurrency and are recommended for the full demonstration; valid values are 1–4.

```powershell
# One live case
.\.venv\Scripts\python.exe main.py --mode live --confirm-live final run demo01

# Full live workflow
.\.venv\Scripts\python.exe main.py --mode live --workers 2 --confirm-live final --json `
    --output reports/final-live-run.json run-all
```

The process exits nonzero if an execution fails or any graph result or persisted artifact differs from its manifest expectation. `run-all` isolates failures and reports per-case workflow/total timing. The live runner requires each selected trace to be at its clean seeded baseline and refuses a dirty rerun; use the guarded `db.admin reset` command before another benchmark. `--output` keeps the complete result and per-case GCP evidence as a UTF-8 JSON report.

This CLI report is graph/Azure/GCP persistence evidence. It does not traverse
the public refund HTTP endpoint or read the result back through the dashboard
HTTP API; use the strict HTTP acceptance command below for that boundary-level
claim.

## Run the APIs and UIs

Use separate terminals from the repository root.

### Operations dashboard

The dashboard backend reads the canonical `final` database and exposes case details, metrics, audit/governance views, pending approvals, and approval continuation:

```powershell
.\.venv\Scripts\python.exe -m uvicorn dashboard_app.api:app --host 127.0.0.1 --port 8000
```

Start the React UI at [http://localhost:3000](http://localhost:3000):

```powershell
Set-Location frontend
npm start
```

The frontend defaults to `http://localhost:8000`. Set `REACT_APP_API_URL` before `npm start` when the backend uses another origin. The dashboard backend accepts both `http://localhost:3000` and `http://127.0.0.1:3000` by default; override `DASHBOARD_CORS_ORIGINS` with a comma-separated allowlist when needed.

Useful dashboard endpoints:

- `GET /health` or `GET /api/health`
- `GET /api/cases` and `GET /api/cases/{trace_id}`
- `GET /api/console-metrics`
- `GET /api/audit-log/query`
- `GET /api/governance-events`
- `GET /api/approvals/pending`
- `POST /api/approvals/{trace_id}/resolve`

Resolve a pending review only with the `approval_id` returned by `GET /api/approvals/pending`:

```powershell
$body = @{
    approval_id = "<pending-approval-id>"
    decision = "partial_refund"
    resolved_amount = 199.99
    reviewer = "demo-reviewer"
    notes = "Approved during the final workflow demonstration."
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8000/api/approvals/demo07/resolve" `
    -ContentType "application/json" `
    -Body $body
```

Allowed decisions are `approve`, `partial_refund`, and `deny`. A refund continuation requires a positive `resolved_amount` with at most two decimal places and cannot exceed the remaining refundable amount; `partial_refund` must also be below the requested amount. A non-refund approval and every denial must omit the amount. `reviewer` and `notes` are required. Replaying the exact resolution is idempotent, while a conflicting resolution returns HTTP 409. A successful request records the reviewer decision transactionally, then resumes the appropriate Policy, Refund, or Response path and records continuation completion. If downstream continuation fails, the API returns HTTP 502 and the identical decision can be retried. A later governance block can create one new pending approval without recursion.

### Refund case selector

The refund adapter has a static UI and accepts only selectors that resolve to one canonical case. It is offline by default:

```powershell
.\.venv\Scripts\python.exe -m uvicorn refund_app.api:app --host 127.0.0.1 --port 8077
```

Open [http://127.0.0.1:8077](http://127.0.0.1:8077), or call:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8077/api/cases"
Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8077/api/refund" `
    -ContentType "application/json" `
    -Body '{"case_id":"demo01"}'
```

To make the adapter invoke the live graph, set the explicit live switches before starting it:

```powershell
$env:REFUND_MODE = "live"
$env:REFUND_DB = "real"
$env:MYSQL_DATABASE = "final"
.\.venv\Scripts\python.exe -m uvicorn refund_app.api:app --host 127.0.0.1 --port 8077
```

After the initial batch, `demo10` and `demo14` are durably `waiting_user` at
Triage. Their case cards expose the canonical missing facts and a **Continue
customer → Triage** control. The corresponding
`POST /api/refund/{case_id}/follow-up` boundary is live-only, accepts only the
exact fixture facts, keeps the same trace/ticket/customer/order roots, preserves
the first request-info cycle, and returns the stored completion on an identical
replay. The control remains available after a browser reload because readiness
comes from persisted workflow state rather than page memory.

### Strict HTTP acceptance

Start both live API processes above against a clean, guarded `final` baseline,
then run the acceptance client from a third terminal. It uses real TCP HTTP; it
does not import either FastAPI application or substitute `TestClient`:

```powershell
.\.venv\Scripts\python.exe -m demo.http_acceptance `
    --confirm-live final `
    --output reports/final-http-e2e.json
```

The harness rejects offline/fake/non-`final` health, requires the exact ordered
`demo01` through `demo20` catalog, submits each case through `POST /api/refund`,
and requires HTTP 200, `success=true`, `matched_expectations=true`, exact
identities/route/outcome/state, and complete persistence checks. It then reads
each case through `GET /api/cases/{trace_id}` and cross-checks dashboard cases,
metrics, audit, governance, and pending-approval endpoints. After the initial
20-case projection is proved, it submits the canonical customer facts for
`demo10` and `demo14` through their public follow-up endpoints, requires both
workflows to resume through Triage → Policy → Refund → Response, checks the
completed dashboard projections, and identically replays each request to prove
idempotency. The durable report
records the service URLs, Git commit/dirty state, fixture SHA-256, and raw JSON
evidence. The default proof intentionally leaves all manual-review cases
pending.

To include the optional demo07 approval and identical idempotent replay in the
same clean-baseline run, add explicit reviewer inputs:

```powershell
.\.venv\Scripts\python.exe -m demo.http_acceptance `
    --confirm-live final `
    --approval-decision partial_refund `
    --approval-amount 199.99 `
    --approval-reviewer "demo-reviewer" `
    --approval-notes "Approved during the final HTTP acceptance demonstration." `
    --output reports/final-http-e2e-with-approval.json
```

This is API-level system evidence. Browser interaction and visual rendering
remain a separate UI QA step.

## Verification

```powershell
.\.venv\Scripts\python.exe -m compileall -q agents app dashboard_app db demo governance refund_app tools
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m "not live"

Set-Location frontend
npm run build
```

Tests marked `live` are excluded deliberately; use the guarded live runner for the real Azure/GCP demonstration.

## Safety and External-Service Boundaries

- Offline results prove fixture and routing expectations without exercising Azure or GCP. Only a guarded live run proves external connectivity and persistence.
- Live runs and approval continuations can consume Azure quota and change output rows in GCP `final`. Request latency depends on the Azure deployment; the default per-request timeout is 60 seconds with at most two retries.
- The dashboard approval endpoint is a loopback-only demo control with a self-asserted reviewer field, not a production identity provider. Keep the documented `127.0.0.1` binding; add authenticated reviewer identity and authorization before any non-local deployment.
- The workflow requires a reachable Azure OpenAI deployment that returns the expected structured JSON and a MySQL account permitted to use `final`. Database creation additionally requires `CREATE DATABASE` permission.
- Refund execution is a deterministic local demo adapter (`RF-<ticket-id>`), not a payment-provider or banking integration. Successful results are persisted for end-to-end workflow evidence, but no real funds move.
- The fixture intentionally contains missing-data, invalid-reference, governance, denial, approval, and refund paths. Do not replace its identities or add arbitrary root cases for a demo run.
- Do not commit `.env`, cloud credentials, generated `node_modules`, or frontend build output.

---

## System Workflow Pipeline

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
                                  [ Triage Handoff ]
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
               |                          |                  [ Policy Handoff ]
               |                          |                          |
               |                          |                          v
               |                          |               [ Policy Persistence ]
               |                          |                          |
               |                          |        +-----------------+-----------------+
               |                          |        | (approve)       | (deny / info)   | (block / review)
               |                          |        v                 |                 |
               |                          |  [ Refund Agent ]        |                 |
               |                          |        |                 |                 |
               |                          |        |                 |                 |
               |                          v        v                 |                 v
               +----------------------> [ Response Agent ] <---------+---------[ Human Approval ]
                                          |                                            
                                          v                                            
                                       [ END ] 
```

Subgraph boundary note:

- `Triage Subgraph`: `Triage Agent` -> `Triage Governance` -> `Triage Handoff`
- `Policy Subgraph`: `Policy Agent` -> `Policy Governance` -> `Policy Handoff`
- `Policy Persistence`, `Refund Agent`, `Human Approval`, and `Response Agent` stay in the parent graph.
- Inside each subgraph, the handoff step writes a semantic handoff result. Policy persistence completes before the parent mapper selects the real next node.
- After `Refund Agent`, the parent graph always continues directly to `Response Agent`.
- The Policy subgraph receives a narrow JSON input so additive parent token events and risk flags are not counted twice.

## Core State Table

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
| `user_action_required` | `bool` | Whether the workflow is waiting for user input | Triage Handoff, Response |
| `human_review_required` | `bool` | Whether the case must be reviewed by a human | Governance, Policy Handoff |
| `final_outcome` | `str` | Final case result such as `approved`, `denied`, `need_info`, `refund_failed`, or `manual_review` | End stages |
| `requested_order_id` | `str` | Order ID extracted from the user message | Triage |
| `clarification_question` | `str` | Follow-up question sent back to the user | Response |
| `order_lookup_result` | `dict` | Raw order data returned by the lookup tool | Triage, Audit |
| `triage_output` | `dict` | Structured case payload produced by triage | Triage Governance, Policy |
| `triage_handoff` | `str` | Subgraph handoff result such as `policy`, `response`, or `human_review` | Triage Handoff, Parent Graph |
| `triage_governance_result` | `dict` | Triage governance decision with allow or block result | Triage Handoff, Human Approval |
| `policy_governance_result` | `dict` | Policy governance decision with allow or block result | Policy Handoff, Human Approval |
| `risk_flags` | `Annotated[list[dict], operator.add]` | Append-only, stage-tagged risk findings | Governance |
| `policy_result` | `dict` | Complete validated Policy reasoning result as JSON | Policy, Governance, Persistence |
| `policy_decision` | `dict` | Final policy decision for the refund case | Policy Governance, Mappers, Downstream |
| `policy_context` | `dict` | Supporting policy metadata such as rule version or retrieval context | Policy, Audit |
| `policy_handoff` | `str` | Subgraph handoff result such as `refund`, `response`, or `human_review` | Policy Handoff, Parent Graph |
| `policy_persistence_result` | `dict` | Persisted handoff ID, downstream agent, and event counts | Policy Persistence |
| `refund_result` | `dict` | Output from the refund execution branch | Refund Agent |
| `response_result` | `dict` | Output from the user response branch | Response Agent |
| `human_review` | `dict` | Output from the human approval branch | Human Approval |
| `errors` | `Annotated[list[dict], operator.add]` | Append-only list of workflow errors or exceptions | All stages |
| `audit_trail` | `Annotated[list[dict], operator.add]` | Append-only list of audit records written across important steps | Governance, Persistence |
| `snapshots` | `Annotated[list[dict], operator.add]` | Append-only list of state snapshots for tracing and replay | Middleware, Observability |
| `llm_input_tokens` | `Annotated[int, operator.add]` | Additive total of input tokens reported by each LLM node | Triage, Policy |
| `llm_output_tokens` | `Annotated[int, operator.add]` | Additive total of output tokens reported by each LLM node | Triage, Policy |

Removed from the core state table:

- `case` -> use `request_context`
- `awaiting_info` -> use `missing_fields` and `user_action_required`
- `awaiting_order_id` -> use `missing_fields` and `user_action_required`
- `next_agent` -> routing belongs to mappers
- `buggy_db` -> test-only, not part of core workflow state
- `content_filter_blocked` -> use `risk_flags`
- `injection_flag` -> use `risk_flags`

## Stage Flow Using The New State Table

### 1. User Input

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

### 2. Triage Agent

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

### 3. Triage Governance

This stage checks whether the triage result is safe before policy evaluation.

Reads:

- `triage_output`
- `trace_id`
- `ticket_id`
- `user_id`

Writes:

- `current_stage = "triage_governance"`
- `triage_governance_result`
- `risk_flags`
- `audit_trail`
- `snapshots`
- `human_review_required` when blocked
- `workflow_status` when blocked

Typical results:

- If safe:
        - `triage_governance_result.status = "allow"`
- If blocked:
        - `triage_governance_result.status = "block"`
        - `human_review_required = True`
        - `workflow_status = "waiting_human"`

### 4. Triage Handoff

This stage does not jump directly to parent-graph nodes. It reads control state and writes a `triage_handoff` value for the parent graph to map.

Reads:

- `triage_governance_result`
- `missing_fields`
- `user_action_required`
- `triage_output`

Routing rules:

- If `triage_governance_result.status == "block"` -> `triage_handoff = "human_review"`
- If `user_action_required == True` -> `triage_handoff = "response"`
- If `triage_output` is complete -> `triage_handoff = "policy"`

After Policy persistence succeeds, the parent graph maps:

- `policy` -> `Policy Agent`
- `response` -> `Response Agent`
- `human_review` -> `Human Approval`

### 5. Policy Agent

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
        - `deny`
        - `request_info`
        - `manual_review`

### 6. Policy Governance

This stage checks whether the policy result is safe and valid.

Reads:

- `policy_decision`
- `policy_context`
- `trace_id`
- `ticket_id`
- `user_id`

Writes:

- `current_stage = "policy_governance"`
- `policy_governance_result`
- `risk_flags`
- `audit_trail`
- `snapshots`
- `human_review_required` when blocked
- `workflow_status` when blocked

Typical results:

- If safe:
        - `policy_governance_result.status = "allow"`
- If blocked:
        - `policy_governance_result.status = "block"`
        - `human_review_required = True`
        - `workflow_status = "waiting_human"`

### 7. Policy Handoff

This stage reads the policy result and writes a `policy_handoff` value for the parent graph to map.

Reads:

- `policy_governance_result`
- `policy_decision`

Routing rules:

- If `policy_governance_result.status == "block"` -> `policy_handoff = "human_review"`
- If `policy_decision.decision == "approve"` -> `policy_handoff = "refund"`
- If `policy_decision.decision in {"deny", "request_info"}` -> `policy_handoff = "response"`
- If `policy_decision.decision == "manual_review"` -> `policy_handoff = "human_review"`

The parent graph maps:

- `refund` -> `Refund Agent`
- `response` -> `Response Agent`
- `human_review` -> `Human Approval`

### 8. Policy Persistence

This parent node reconstructs and revalidates the JSON Policy state, then writes the handoff, policy-review or OWASP events, typed human approval, audit row, workflow status, and Policy token totals in one GCP transaction. A write failure stops routing. The mapper routes from the persisted `next_agent` and rejects any disagreement with the subgraph handoff.

### 9. Refund Agent

This is the execution branch for approved refund outcomes.

Reads:

- `policy_decision`
- `trace_id`
- `ticket_id`
- `requested_order_id`
- `order_lookup_result`

Writes:

- `current_stage = "refund_agent"`
- `refund_result`
- `final_outcome`
- `workflow_status = "running"`

Typical results:

- If fully approved: `final_outcome = "approved"`
- If refund execution fails: `final_outcome = "refund_failed"`

### 10. Response Agent

This is the user-facing response branch. It is used for missing data and final business responses.

Reads:

- `clarification_question`
- `missing_fields`
- `user_action_required`
- `policy_decision`
- `refund_result`

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
- If refund succeeded:
        - `response_result` confirms the refund was processed
        - `final_outcome = "approved"`
        - `workflow_status = "completed"`
- If refund failed:
        - `response_result` explains the refund could not be completed
        - `final_outcome = "refund_failed"`
        - `workflow_status = "completed"`
- If policy denied:
        - `response_result` explains the denial
        - `final_outcome = "denied"`
        - `workflow_status = "completed"`
- If policy requested more info:
        - `response_result` asks for the required information
        - `final_outcome = "need_info"`
        - `workflow_status = "waiting_user"`

### 11. Human Approval

This is the durable pause/resume branch for blocked or ambiguous cases.

Reads:

- `policy_governance_result`
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

- The initial graph records a pending approval and a user-facing review response, then stops with durable status `pending_human` in MySQL (`waiting_human` in runtime state).
- A reviewer resolves that exact row through `POST /api/approvals/{trace_id}/resolve`.
- The lifecycle service records the decision transactionally, then resumes the correct Policy, Refund, or Response continuation and records its completion. Identical retries are idempotent; conflicting retries are rejected.
- If Response governance blocks the resumed response, one new pending approval is recorded and execution stops instead of recursing.

## Core Development Rules

1. **Agents**: Return JSON-serializable business data patches only. Never include routing fields (`next_agent`) or database calls in agent nodes.
2. **Handoff and mapping**: Inside subgraphs, the handoff step reads shared state and writes a handoff value. The parent graph mapper maps that handoff to the real next node. No business logic transformation happens in the parent mapper.
3. **Governance**: Operates as standalone nodes. Policy Governance emits OWASP findings only and never rewrites the Policy decision or confidence.
4. **Persistence**: The parent `policy_persistence` node owns the single transactional Policy write and runs before downstream routing.
5. **Middlewares**: Capture execution traces, token metrics, and state snapshots asynchronously.
6. **Tools**: Universal SDK wrappers for external APIs (Azure, DBs, RAG). Agents and governance modules must access external services via `tools/`.
