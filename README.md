# Customer Refund Service

A demo project for handling customer refund requests with a multi-step workflow. It shows how a case can move through triage, policy checks, follow-up, human approval, refund execution, and customer response instead of being treated as a single yes-or-no action.

- A refund API and UI for submitting cases.
- A dashboard for tracking case status, follow-up, and approvals.
- A workflow engine that decides what should happen next for each case.
- Human review and governance checks for cases that should not be auto-approved.

## Dashboard Preview

The dashboard is the main reviewer surface. It makes workflow state visible, separates cases that need follow-up from cases ready for action, and highlights governance-driven holds.

### Overview

A high-level summary of case status, workflow outcomes, and queue health.

![Dashboard overview](figures/dashboard-overview.png)

### Needs More Information

Cases waiting for customer follow-up are separated clearly from completed or review-ready work.

![Dashboard needs more information](figures/dashboard-needs-info.png)

### Governance Review

Governance-driven holds and escalations are visible so reviewers can inspect risky or blocked cases quickly.

![Dashboard governance review](figures/dashboard-governance-review.png)

## Core Experience

From a reader's perspective, the project has three main surfaces:

- Refund intake: the public-facing API and UI entrypoint for refund requests.
- Reviewer dashboard: the operational view for approvals, follow-up, and governance outcomes.
- Workflow engine: the state graph that decides how each case moves through the system.

## Repository Map

- `agents/`: domain agents for triage, policy, refund, and response behavior.
- `app/`: shared workflow graph, state definitions, and mapping logic.
- `dashboard_app/`: dashboard API and approval services.
- `refund_app/`: refund-facing API, static UI, and simulator helpers.
- `db/`: database access, admin commands, and persistence helpers.
- `demo/`: demo runner and HTTP acceptance harness.
- `frontend/`: React dashboard frontend.
- `tests/`: unit, integration, and UI-oriented test coverage.

## Quick Start

Run commands from the repository root. Python 3.11+ and Node.js 20+ are the supported targets.

### 1. Install Python dependencies

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Create a private environment file and replace placeholders with local values:

```powershell
Copy-Item .env.example .env
```

Azure OpenAI and MySQL-backed flows require the settings described in `.env.example`. Keep credentials out of source control.

### 2. Install frontend dependencies

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..
```

### 3. Start the services

Dashboard backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn dashboard_app.api:app --host 127.0.0.1 --port 8000
```

Dashboard frontend:

```powershell
Set-Location frontend
npm start
```

Refund API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn refund_app.api:app --host 127.0.0.1 --port 8077
```

The frontend defaults to `http://localhost:8000`. Set `REACT_APP_API_URL` before startup when the backend uses another origin.

## Run The Workflow

The CLI can list cases, run a single case, or execute a batch workflow:

```powershell
.\.venv\Scripts\python.exe main.py list
.\.venv\Scripts\python.exe main.py run demo01
.\.venv\Scripts\python.exe main.py --workers 2 run-all
```

Offline execution is the safe default. Live execution, guarded demo flows, and acceptance runs are documented separately.

## Verification

```powershell
.\.venv\Scripts\python.exe -m compileall -q agents app dashboard_app db demo governance refund_app tools
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m "not live"

Set-Location frontend
npm run build
```

## Documentation Map

- Demo workflow and acceptance flows: `demo/DEMO_WORKFLOW_GUIDE.md`
- Database setup, seed, verify, and reset operations: `db/DATABASE_OPERATIONS_GUIDE.md`
- Dashboard API, frontend, and approval flow: `dashboard_app/README.md`
- Refund adapter modes and API details: `refund_app/README.md`
- Policy agent internals: `agents/policy/README.md`

## System Workflow Pipeline

```mermaid
flowchart LR
        UI[User Input] --> TRIAGE[Triage Subgraph]

        TRIAGE -->|Complete| POLICY[Policy Subgraph]
        POLICY -->|Approved| REFUND[Refund Agent]
        REFUND -->|Refund Result| RESPONSE[Response Subgraph]
        RESPONSE -->|Completed| END[End]

        POLICY -->|Denied| RESPONSE
        TRIAGE -->|Missing Info| RESPONSE

        TRIAGE -->|Human Review| HUMAN[Human Approval]
        POLICY -->|Human Review| HUMAN
        RESPONSE -->|Human Review| HUMAN

        HUMAN -->|Reply Needed| RESPONSE
        HUMAN -->|Closed| END
```

The main graph routes between compiled subgraphs and parent-level nodes. Each subgraph completes its own internal steps, persists its result, and only then returns control to the parent graph for the next transition.

### Triage Subgraph

```mermaid
flowchart LR
        TA[Triage Agent] --> TG[Triage Governance]
        TG --> TH[Triage Handoff]
        TH --> TP[Triage Persistence]
```

This subgraph turns the incoming request into structured case data, checks whether the result is safe to continue, decides the semantic handoff, and persists the triage outcome before the parent graph chooses the next node.

### Policy Subgraph

```mermaid
flowchart LR
        PA[Policy Agent] --> PG[Policy Governance]
        PG --> PH[Policy Handoff]
        PH --> PP[Policy Persistence]
```

This subgraph evaluates refund policy, applies policy-stage governance, decides whether the case should go to refund, response, or human review, and persists that decision before the parent graph routes onward.

### Response Subgraph

```mermaid
flowchart LR
        RA[Response Agent] --> RG[Response Governance]
        RG --> RH[Response Handoff]
        RH --> RP[Response Persistence]
```

This subgraph prepares the customer-facing reply, checks whether the response is safe to send, decides whether the workflow can end or must pause for human review, and persists the response result before returning to the parent graph.
