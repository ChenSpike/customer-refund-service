"""Refund frontend backend.

One endpoint, three modes selected by env vars:

  REFUND_AZURE = fake (default) | real
  REFUND_DB    = fake (default) | real

  azure=fake            -> deterministic simulator, no network, no creds
  azure=real, db=fake   -> real build_graph + real Azure + in-memory repo + fixture orders
  azure=real, db=real   -> real build_graph + real Azure + live team DB (bootstraps ticket rows)

The frontend contract is identical across modes, so switching to live is just an
env flip — no UI change. See refund_app/README.md.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from refund_app.simulator import simulate

load_dotenv()

AZURE_MODE = os.getenv("REFUND_AZURE", "fake").lower()
DB_MODE = os.getenv("REFUND_DB", "fake").lower()
_STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Refund Service")


class RefundRequest(BaseModel):
    message: str
    order_id: str | None = None
    user_id: str = "CUST-001"


def _prepare_azure_env() -> None:
    """Trim the endpoint to the resource base and default the deployment name."""
    ep = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    if ep:
        parts = urlsplit(ep)
        os.environ["AZURE_OPENAI_ENDPOINT"] = f"{parts.scheme}://{parts.netloc}/"
    os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4")


def _bridge_mysql_env() -> None:
    for gcp, plain in [
        ("GCP_MYSQL_HOST", "MYSQL_HOST"),
        ("GCP_MYSQL_USER", "MYSQL_USER"),
        ("GCP_MYSQL_PASSWORD", "MYSQL_PASSWORD"),
        ("GCP_MYSQL_PORT", "MYSQL_PORT"),
        ("GCP_MYSQL_CONNECT_TIMEOUT", "MYSQL_CONNECT_TIMEOUT"),
    ]:
        if os.getenv(gcp) and not os.getenv(plain):
            os.environ[plain] = os.environ[gcp]


def _normalize(state: dict[str, Any]) -> dict[str, Any]:
    """Collapse the graph AppState into the frontend result contract."""
    response_result = state.get("response_result") or {}
    body = (
        response_result.get("response", {}).get("body")
        or response_result.get("body")
        or "(no response generated)"
    )
    tg = state.get("triage_governance_result") or {}
    pg = state.get("policy_governance_result") or {}
    detail = None
    for finding in (tg.get("findings") or []):
        if finding.get("detail"):
            detail = finding["detail"]
            break
    return {
        "final_outcome": state.get("final_outcome"),
        "workflow_status": state.get("workflow_status"),
        "response_body": body,
        "governance": {
            "triage": tg.get("status"),
            "policy": pg.get("status"),
            "detail": detail,
        },
        "human_review": state.get("human_review"),
        "trace_id": state.get("trace_id"),
    }


def _run_live_graph(req: RefundRequest) -> dict[str, Any]:
    _prepare_azure_env()

    # Import only now: tools.azure_client reads AZURE_OPENAI_ENDPOINT at import.
    from app.graph import build_graph
    from agents.policy.azure import AzureJsonClient

    trace_id = f"TRACE-{uuid.uuid4().hex[:8].upper()}"
    ticket_id = f"TICKET-{uuid.uuid4().hex[:8].upper()}"
    client = AzureJsonClient.from_env()

    if DB_MODE == "real":
        _bridge_mysql_env()
        from db.database import GCPRepository

        repository = GCPRepository.from_env()
        # Bootstrap the parent rows the persistence nodes expect (same as main.py).
        conn = repository._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO tickets (ticket_id, customer_id, raw_text) VALUES (%s, %s, %s)",
                (ticket_id, req.user_id, req.message),
            )
            cur.execute(
                """INSERT INTO workflow_runs (trace_id, ticket_id, status, current_agent, policy_version)
                   VALUES (%s, %s, 'running', 'triage_agent', 'v1.0')""",
                (trace_id, ticket_id),
            )
            conn.commit()
        finally:
            conn.close()
    else:
        # db=fake: in-memory repo + fixture order lookup (no DB, no writes).
        from refund_app.fake_repo import FakeRepository
        from refund_app.fixtures import get_order_fixture
        import agents.triage.node as triage_node_module

        repository = FakeRepository()
        triage_node_module.order_database_lookup = (
            lambda order_id, buggy=False: get_order_fixture(order_id, buggy)
        )

    graph = build_graph(client=client, repository=repository)
    state = graph.invoke({
        "user_id": req.user_id,
        "message": req.message,
        "conversation_history": [],
        "request_context": {"trace_id": trace_id, "ticket_id": ticket_id, "buggy_db": False},
        "trace_id": trace_id,
        "ticket_id": ticket_id,
    })
    return _normalize(state)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "azure": AZURE_MODE, "db": DB_MODE}


@app.post("/api/refund")
def refund(req: RefundRequest) -> dict[str, Any]:
    if AZURE_MODE == "real":
        result = _run_live_graph(req)
    else:
        result = simulate(req.message, req.order_id)
    result["mode"] = {"azure": AZURE_MODE, "db": DB_MODE}
    return result


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")
