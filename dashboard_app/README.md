# Dashboard Guide

This guide documents the operations dashboard backend, frontend, and approval flow.

## Components

- `dashboard_app.api`: FastAPI backend for case views, metrics, audit, governance, and approvals.
- `frontend/`: React frontend for browsing cases and acting on pending approvals.

## Start The Dashboard

Backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn dashboard_app.api:app --host 127.0.0.1 --port 8000
```

Frontend:

```powershell
Set-Location frontend
npm start
```

The frontend defaults to `http://localhost:8000`. Set `REACT_APP_API_URL` before startup when needed. The backend accepts `http://localhost:3000` and `http://127.0.0.1:3000` by default; override `DASHBOARD_CORS_ORIGINS` with a comma-separated allowlist when required.

## Main Endpoints

- `GET /health`
- `GET /api/health`
- `GET /api/cases`
- `GET /api/cases/{trace_id}`
- `GET /api/console-metrics`
- `GET /api/audit-log/query`
- `GET /api/governance-events`
- `GET /api/approvals/pending`
- `POST /api/approvals/{trace_id}/resolve`

## Approval Flow

Resolve a pending approval with the `approval_id` returned by `GET /api/approvals/pending`:

```powershell
$body = @{
    approval_id = "<pending-approval-id>"
    decision = "partial_refund"
    resolved_amount = 199.99
    reviewer = "demo-reviewer"
    notes = "Approved during the dashboard demonstration."
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8000/api/approvals/demo07/resolve" `
    -ContentType "application/json" `
    -Body $body
```

Allowed decisions are `approve`, `partial_refund`, and `deny`. Amount rules and idempotent replay behavior are enforced by the backend service.

## What The Dashboard Shows

- Case details and workflow state
- Console metrics
- Audit log projections
- Governance events
- Pending approvals and continuation results

The dashboard is especially useful during demo and acceptance runs because it exposes the persisted workflow state through a separate API boundary.

## Related Guides

- Project entry and setup: `../README.md`
- Demo and acceptance flows: `../demo/DEMO_WORKFLOW_GUIDE.md`
- Database operations: `../db/DATABASE_OPERATIONS_GUIDE.md`
- Refund adapter behavior: `../refund_app/README.md`