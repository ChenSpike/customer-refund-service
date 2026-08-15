# Final refund demo adapter

The API and CLI expose only the canonical `demo01` through `demo20` records in
`database/fixtures/demo_cases.json`. They reuse each case's seeded trace,
ticket, customer, order, and original message. Neither entry point creates a
new root ticket or workflow run.

## Modes

| Mode | Behaviour |
|---|---|
| `offline` (default) | Deterministic results from the 20-case manifest. No DB, Azure, or network call. |
| `live` | Invokes the real state-driven graph after read-only verification that the target is database `final` and its root workflow set is exactly `demo01` through `demo20`. |

The runner supplies the UI-selected canonical order explicitly in both
`requested_order_id` and `request_context.selected_order_id`. This preserves
the fixture message for missing/wrong-ID scenarios such as `demo04`, `demo10`,
`demo14`, and `demo18`. It also scopes `POLICY_EVALUATION_DATE` to the fixture's
evaluation date for each execution and restores the prior environment value.

## Safe CLI

```powershell
# No arguments prints help and performs no cloud action.
.venv\Scripts\python.exe main.py

# Offline, deterministic execution.
.venv\Scripts\python.exe main.py list
.venv\Scripts\python.exe main.py run demo01
.venv\Scripts\python.exe main.py run-all

# Live graph: both flags are intentional safeguards.
$env:MYSQL_DATABASE = "final"
.venv\Scripts\python.exe main.py --mode live --confirm-live final run demo01
```

`run-all` isolates failures and reports workflow and total timings per case.
Live execution can write downstream audit, handoff, approval, response, and
refund artifacts for the existing roots, but never inserts a new root case.

## API

```powershell
# Offline by default.
.venv\Scripts\python.exe -m uvicorn refund_app.api:app --port 8077
# Then open http://127.0.0.1:8077; the page loads exactly demo01-demo20.

# Full live graph and seeded GCP database.
$env:REFUND_MODE = "live"
$env:REFUND_DB = "real"
$env:MYSQL_DATABASE = "final"
.venv\Scripts\python.exe -m uvicorn refund_app.api:app --port 8077
```

Endpoints:

- `GET /api/health`
- `GET /api/cases` — the exact allowlist and canonical selectors
- `POST /api/refund`

The preferred request is:

```json
{"case_id": "demo18"}
```

Clients may additionally send `order_id`, `customer_id` (or legacy `user_id`),
and the exact fixture `message`. Every supplied selector must resolve to the
same canonical case; arbitrary or mixed identities receive HTTP 422.

For compatibility, `REFUND_AZURE=real` selects live mode when `REFUND_MODE` is
unset. `GCP_MYSQL_*` variables are bridged to the existing `MYSQL_*` names.
Live mode also requires `REFUND_DB=real`; a non-`final` repository is rejected
before graph invocation.
