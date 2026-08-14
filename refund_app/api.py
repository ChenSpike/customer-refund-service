"""FastAPI adapter for the fixed ``demo01`` through ``demo20`` workflow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from demo.catalog import DemoCatalogError, FINAL_DATABASE, load_demo_catalog, resolve_demo_case
from demo.runner import DemoRunner

load_dotenv()

_STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Refund Service Final Demo")


class RefundHealthError(RuntimeError):
    """A controlled, non-secret live-health validation failure."""


class RefundRequest(BaseModel):
    case_id: str | None = None
    message: str | None = None
    order_id: str | None = None
    customer_id: str | None = None
    # Retained for older clients, but it must equal the seeded customer when set.
    user_id: str | None = None


def _configured_mode() -> str:
    configured = os.getenv("REFUND_MODE")
    if configured:
        mode = configured.strip().lower()
    else:
        mode = "live" if os.getenv("REFUND_AZURE", "fake").lower() == "real" else "offline"
    if mode not in {"offline", "live"}:
        raise RuntimeError("REFUND_MODE must be 'offline' or 'live'")
    return mode


def _db_mode() -> str:
    return os.getenv("REFUND_DB", "fake").strip().lower()


def _prepare_azure_env() -> None:
    """Normalize Azure settings immediately before an explicitly live call."""

    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    if endpoint:
        parts = urlsplit(endpoint)
        os.environ["AZURE_OPENAI_ENDPOINT"] = f"{parts.scheme}://{parts.netloc}/"
    os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4")


def _bridge_mysql_env() -> None:
    for gcp, plain in (
        ("GCP_MYSQL_HOST", "MYSQL_HOST"),
        ("GCP_MYSQL_USER", "MYSQL_USER"),
        ("GCP_MYSQL_PASSWORD", "MYSQL_PASSWORD"),
        ("GCP_MYSQL_PORT", "MYSQL_PORT"),
        ("GCP_MYSQL_DATABASE", "MYSQL_DATABASE"),
        ("GCP_MYSQL_CONNECT_TIMEOUT", "MYSQL_CONNECT_TIMEOUT"),
    ):
        if os.getenv(gcp) and not os.getenv(plain):
            os.environ[plain] = os.environ[gcp]
    os.environ.setdefault("MYSQL_DATABASE", FINAL_DATABASE)


def _create_runner(mode: str) -> DemoRunner:
    if mode == "live":
        if _db_mode() != "real":
            raise RuntimeError("Live demo mode requires REFUND_DB=real and the seeded final database")
        _prepare_azure_env()
        _bridge_mysql_env()
    return DemoRunner(mode=mode)


def _check_live_database() -> dict[str, str]:
    """Verify that live mode can reach the actually selected ``final`` database."""

    if _db_mode() != "real":
        raise RefundHealthError("Live mode requires REFUND_DB=real")
    _bridge_mysql_env()
    if os.getenv("MYSQL_DATABASE") != FINAL_DATABASE:
        raise RefundHealthError("Live mode requires MYSQL_DATABASE=final")

    from db.database import GCPRepository

    repository = GCPRepository.from_env()
    if repository.database_name != FINAL_DATABASE:
        raise RefundHealthError("Live repository is not configured for database 'final'")

    connection = None
    cursor = None
    try:
        connection = repository._connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT DATABASE() AS database_name")
        row = cursor.fetchone()
        selected = row.get("database_name") if isinstance(row, dict) else (row[0] if row else None)
        if selected != FINAL_DATABASE:
            raise RefundHealthError("Connected database is not 'final'")
        return {"status": "ok", "database": FINAL_DATABASE}
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


def _resolve_request(req: RefundRequest):
    catalog = load_demo_catalog()
    customer_selector = req.customer_id or req.user_id
    if req.customer_id and req.user_id and req.customer_id != req.user_id:
        raise DemoCatalogError("customer_id and user_id must identify the same seeded customer")
    return resolve_demo_case(
        catalog,
        case_id=req.case_id,
        order_id=req.order_id,
        customer_id=customer_selector,
        message=req.message,
    )


@app.get("/api/health")
def health() -> JSONResponse:
    try:
        mode = _configured_mode()
    except RuntimeError as error:
        return JSONResponse(
            status_code=503,
            content={
                "status": "misconfigured",
                "mode": "invalid",
                "database": FINAL_DATABASE,
                "detail": str(error),
            },
        )

    if mode == "offline":
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "mode": mode,
                "database": FINAL_DATABASE,
                "database_status": "not_checked",
            },
        )

    try:
        database = _check_live_database()
    except RefundHealthError as error:
        return JSONResponse(
            status_code=503,
            content={
                "status": "misconfigured",
                "mode": mode,
                "database": FINAL_DATABASE,
                "detail": str(error),
            },
        )
    except Exception as error:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "mode": mode,
                "database": FINAL_DATABASE,
                "detail": "Live database check failed",
                "error_type": type(error).__name__,
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "mode": mode,
            "database": database["database"],
            "database_status": database["status"],
        },
    )


@app.get("/api/cases")
def cases() -> dict[str, Any]:
    catalog = load_demo_catalog()
    return {
        "database": catalog.database,
        "evaluation_date": catalog.evaluation_date,
        "cases": [case.public_summary() for case in catalog.cases],
    }


@app.post("/api/refund")
def refund(req: RefundRequest) -> dict[str, Any]:
    try:
        case = _resolve_request(req)
    except DemoCatalogError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    try:
        mode = _configured_mode()
        result = _create_runner(mode).run_case(case.trace_id)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not result["success"]:
        error = result.get("error") or {}
        raise HTTPException(
            status_code=503,
            detail=f"{error.get('type', 'DemoRunError')}: {error.get('message', 'execution failed')}",
        )

    # These identities always come from the selected fixture, never client text
    # or a newly generated root record.
    result.update(
        {
            "case_id": case.trace_id,
            "trace_id": case.trace_id,
            "ticket_id": case.ticket_id,
            "customer_id": case.customer_id,
            "order_id": case.order_id,
            "selected_order_id": case.selected_order_id,
            "message": case.message,
            "mode": {"workflow": mode, "db": _db_mode()},
        }
    )
    return result


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")
