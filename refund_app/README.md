# Refund frontend (`refund_app`)

A customer-facing refund request UI + a thin FastAPI backend over the
state-driven pipeline (`app.graph.build_graph`). One endpoint, three modes
selected by env vars — the frontend contract is identical across all of them,
so going live is an env flip, not a code change.

## Modes

| `REFUND_AZURE` | `REFUND_DB` | Behaviour |
|----------------|-------------|-----------|
| `fake` (default) | `fake` | Deterministic simulator. No network, no credentials. Demos the governance paths (approve / deny / need-info / ASI07 leak → human review). |
| `real` | `fake` | Real `build_graph` + real Azure (real LLM reasoning) + in-memory repo + fixture orders. No DB writes. |
| `real` | `real` | Real pipeline against the live team `main_db`. Bootstraps the `tickets` + `workflow_runs` rows, then persists handoffs / governance / response. Writes to shared infra. |

## Run

```bash
# offline demo (default)
python -m uvicorn refund_app.api:app --port 8077
# open http://localhost:8077

# real AI, no DB (needs working Azure creds in .env; spends tokens)
REFUND_AZURE=real REFUND_DB=fake python -m uvicorn refund_app.api:app --port 8077

# full live (needs the DB reachable + your IP whitelisted on Cloud SQL)
REFUND_AZURE=real REFUND_DB=real python -m uvicorn refund_app.api:app --port 8077
```

## Env

Read from the repo-root `.env` (via `python-dotenv`). The backend normalises two
known mismatches at startup so the existing `.env` works unchanged:

- `AZURE_OPENAI_ENDPOINT` is trimmed to the resource base URL (the SDK appends
  the `/openai/responses` path itself).
- `GCP_MYSQL_*` keys are bridged to the `MYSQL_*` names the refactor DB layer
  reads. `AZURE_OPENAI_DEPLOYMENT` defaults to `gpt-5.4`.

## Files

- `api.py` — FastAPI app: `GET /`, `GET /api/health`, `POST /api/refund`.
- `simulator.py` — deterministic offline stand-in (`REFUND_AZURE=fake`).
- `fixtures.py` — canned orders for `REFUND_DB=fake` (incl. an ASI07 leak row).
- `fake_repo.py` — in-memory repository for `REFUND_DB=fake` (no DB writes).
- `static/index.html` — single-page UI (vanilla JS, no build step).

## Scope note

This is the customer **submission** side of the refund flow. The team's
`idox_dashboard` is the governance **monitoring** side — different surface, no
overlap. The simulator is a UI/demo stand-in; real reasoning happens only in
`REFUND_AZURE=real`.
