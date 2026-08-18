# Demo Workflow Guide

This guide documents the repository's demo-oriented flows. It is intentionally separate from the root README so the main project documentation stays focused on setup and day-to-day development.

## Scope

The demo flow uses the canonical fixture records `demo01` through `demo20` from `database/fixtures/demo_cases.json`. These cases are used to exercise approval, denial, follow-up, governance, and refund paths in a repeatable way.

## Modes

- `offline`: deterministic fixture-backed execution with no Azure, MySQL, or network dependency.
- `live`: guarded execution against the real workflow graph and configured external services.

## CLI Demo Runs

Offline examples:

```powershell
.\.venv\Scripts\python.exe main.py list
.\.venv\Scripts\python.exe main.py run demo01
.\.venv\Scripts\python.exe main.py --workers 2 run-all
.\.venv\Scripts\python.exe main.py --workers 2 --json run-all
```

Live examples:

```powershell
.\.venv\Scripts\python.exe main.py --mode live --confirm-live final run demo01

.\.venv\Scripts\python.exe main.py --mode live --workers 2 --confirm-live final --json `
    --output reports/final-live-run.json run-all
```

Live runs are intended for guarded demo environments. They can write audit, handoff, approval, response, and refund artifacts for the seeded roots.

## Refund Demo Flow

Start the refund API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn refund_app.api:app --host 127.0.0.1 --port 8077
```

Offline mode is the default. To route requests through the live graph, set the required environment variables before startup:

```powershell
$env:REFUND_MODE = "live"
$env:REFUND_DB = "real"
$env:MYSQL_DATABASE = "final"
.\.venv\Scripts\python.exe -m uvicorn refund_app.api:app --host 127.0.0.1 --port 8077
```

The refund UI and API accept only selectors that resolve to one canonical case. For request formats and follow-up behavior, see `refund_app/README.md`.

## Dashboard Demo Flow

Start the dashboard backend and frontend in separate terminals:

```powershell
.\.venv\Scripts\python.exe -m uvicorn dashboard_app.api:app --host 127.0.0.1 --port 8000

Set-Location frontend
npm start
```

The dashboard is used to inspect case details, metrics, governance events, and pending approvals during a demo run. Approval resolution details are documented in `dashboard_app/README.md`.

## HTTP Acceptance

The strict acceptance harness exercises the public refund API and validates the resulting dashboard projections.

```powershell
.\.venv\Scripts\python.exe -m demo.http_acceptance `
    --confirm-live final `
    --output reports/final-http-e2e.json
```

Optional approval replay can be included in the same run:

```powershell
.\.venv\Scripts\python.exe -m demo.http_acceptance `
    --confirm-live final `
    --approval-decision partial_refund `
    --approval-amount 199.99 `
    --approval-reviewer "demo-reviewer" `
    --approval-notes "Approved during the final HTTP acceptance demonstration." `
    --output reports/final-http-e2e-with-approval.json
```

This flow is acceptance evidence, not the default developer workflow.

## Reports

Generated reports are written under `reports/`. These files capture demo and acceptance outputs such as live-run summaries and HTTP end-to-end evidence.

## Safety Notes

- Offline runs are safe for local development and do not exercise external services.
- Live runs can consume Azure quota and mutate demo output tables.
- Demo flows assume the guarded database and canonical fixture set are intact.