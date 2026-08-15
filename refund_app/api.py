"""FastAPI adapter for the fixed ``demo01`` through ``demo20`` workflow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from db.followup_store import (
    CustomerFollowupConflictError,
    CustomerFollowupStore,
    CustomerFollowupStoreError,
)

from demo.catalog import (
    DEMO_IDS,
    DemoCatalogError,
    FINAL_DATABASE,
    load_demo_catalog,
    resolve_demo_case,
)
from demo.runner import DemoRunner
from refund_app.followup import CustomerFollowupExecutionError, CustomerFollowupService

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


class CustomerFollowupRequest(BaseModel):
    """Exact fixture-defined facts supplied by the waiting customer."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    customer_id: str = Field(min_length=1, max_length=36)
    order_id: str = Field(min_length=1, max_length=36)
    refund_reason: str = Field(min_length=1, max_length=255)
    requested_amount: float = Field(gt=0, allow_inf_nan=False)
    currency: str = Field(min_length=3, max_length=3)


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


def _create_followup_service(mode: str) -> CustomerFollowupService:
    if mode != "live":
        raise RuntimeError("Customer follow-up requires live mode; offline simulation cannot resume state")
    if _db_mode() != "real":
        raise RuntimeError("Customer follow-up requires REFUND_DB=real")
    _prepare_azure_env()
    _bridge_mysql_env()
    from db.database import GCPRepository

    repository = GCPRepository.from_env()
    return CustomerFollowupService(CustomerFollowupStore(repository))


def _verify_live_canonical_roots(connection: Any) -> str:
    from db.admin import load_fixture, verify_canonical_root_content

    cursor = connection.cursor()
    try:
        return verify_canonical_root_content(cursor, load_fixture(), phase="runtime")
    finally:
        cursor.close()


def _check_live_database() -> dict[str, Any]:
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
        cursor.execute(
            """
            SELECT
              workflow.trace_id,
              workflow.status,
              workflow.current_agent,
              (SELECT COUNT(*) FROM agent_handoffs h WHERE h.trace_id = workflow.trace_id) AS handoffs,
              (SELECT COUNT(*) FROM audit_log a WHERE a.trace_id = workflow.trace_id) AS audits,
              (SELECT COUNT(*) FROM governance_events g WHERE g.trace_id = workflow.trace_id) AS governance_events,
              (SELECT COUNT(*) FROM policy_review_events p WHERE p.trace_id = workflow.trace_id) AS policy_reviews,
              (SELECT COUNT(*) FROM human_approvals ha WHERE ha.trace_id = workflow.trace_id) AS approvals,
              (SELECT COUNT(*) FROM refund_transactions r WHERE r.trace_id = workflow.trace_id) AS refunds,
              (SELECT COUNT(*) FROM audit_log followup_received
                 WHERE followup_received.trace_id = workflow.trace_id
                   AND followup_received.event_type = 'customer_followup_received') AS followup_received,
              (SELECT COUNT(*) FROM audit_log followup_completed
                 WHERE followup_completed.trace_id = workflow.trace_id
                   AND followup_completed.event_type = 'customer_followup_completed') AS followup_completed,
              (SELECT TIMESTAMPDIFF(
                    SECOND, MAX(followup_claimed.created_at), CURRENT_TIMESTAMP
                 ) FROM audit_log followup_claimed
                 WHERE followup_claimed.trace_id = workflow.trace_id
                   AND followup_claimed.event_type = 'customer_followup_claimed') AS followup_claim_age_seconds
            FROM workflow_runs workflow
            ORDER BY workflow.trace_id
            """
        )
        rows = cursor.fetchall()
        root_fingerprint = _verify_live_canonical_roots(connection)
        actual_ids = tuple(str(row["trace_id"]) for row in rows)
        exact_roots = actual_ids == DEMO_IDS
        clean_ids = [
            str(row["trace_id"])
            for row in rows
            if row["status"] == "running"
            and row["current_agent"] == "triage_agent"
            and all(
                int(row[name] or 0) == 0
                for name in (
                    "handoffs",
                    "audits",
                    "governance_events",
                    "policy_reviews",
                    "approvals",
                    "refunds",
                )
            )
        ]
        from db.database import DEFAULT_CONTINUATION_LEASE_SECONDS

        case_states = {}
        for row in rows:
            trace_id = str(row["trace_id"])
            followup_status = "not_applicable"
            retry_after_seconds = None
            if trace_id in {"demo10", "demo14"}:
                received = int(row.get("followup_received") or 0)
                completed = int(row.get("followup_completed") or 0)
                age = int(row.get("followup_claim_age_seconds") or 0)
                if completed:
                    followup_status = "completed"
                elif not received:
                    followup_status = "not_started"
                elif row["status"] == "waiting_user":
                    followup_status = "waiting_user"
                elif row["status"] == "failed":
                    followup_status = "retryable"
                elif row["status"] == "running":
                    retry_after_seconds = max(
                        DEFAULT_CONTINUATION_LEASE_SECONDS - age,
                        0,
                    )
                    followup_status = (
                        "retryable" if retry_after_seconds == 0 else "in_progress"
                    )
            case_states[trace_id] = {
                "workflow_status": str(row["status"]),
                "current_agent": str(row["current_agent"]),
                "followup_status": followup_status,
                "followup_retry_after_seconds": retry_after_seconds,
            }
        return {
            "status": "ok",
            "database": FINAL_DATABASE,
            "exact_demo_roots": exact_roots,
            "canonical_root_data": True,
            "canonical_root_fingerprint": root_fingerprint,
            "clean_case_count": len(clean_ids),
            "clean_case_ids": clean_ids,
            "case_states": case_states,
            "dirty_case_count": len(rows) - len(clean_ids),
            "ready_for_full_run": exact_roots and tuple(clean_ids) == DEMO_IDS,
        }
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
                "clean_case_count": len(DEMO_IDS),
                "dirty_case_count": 0,
                "clean_case_ids": list(DEMO_IDS),
                "case_states": {
                    case_id: {
                        "workflow_status": "simulated",
                        "current_agent": "simulation",
                        "followup_status": "simulation_only",
                        "followup_retry_after_seconds": None,
                    }
                    for case_id in DEMO_IDS
                },
                "ready_for_full_run": True,
                "execution_notice": "OFFLINE SIMULATION — Azure and GCP are not used",
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
            "exact_demo_roots": database["exact_demo_roots"],
            "canonical_root_data": database["canonical_root_data"],
            "canonical_root_fingerprint": database["canonical_root_fingerprint"],
            "clean_case_count": database["clean_case_count"],
            "clean_case_ids": database["clean_case_ids"],
            "case_states": database["case_states"],
            "dirty_case_count": database["dirty_case_count"],
            "ready_for_full_run": database["ready_for_full_run"],
            "execution_notice": (
                "LIVE — ready for all 20 cases"
                if database["ready_for_full_run"]
                else "LIVE — connected, but reset is required before a full rerun"
            ),
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

    result["selected_case"] = {
        "case_id": case.trace_id,
        "trace_id": case.trace_id,
        "ticket_id": case.ticket_id,
        "customer_id": case.customer_id,
        "order_id": case.order_id,
        "selected_order_id": case.selected_order_id,
        "message": case.message,
    }
    result["mode"] = {"workflow": mode, "db": _db_mode()}
    result["execution_boundary"] = {
        "entrypoint": "refund_http_api",
        "database": FINAL_DATABASE,
        "azure": "real" if mode == "live" else "not_used",
    }
    observed_identity = {
        "case_id": result.get("case_id"),
        "trace_id": result.get("trace_id"),
        "ticket_id": result.get("ticket_id"),
        "customer_id": result.get("customer_id"),
        "order_id": result.get("order_id"),
        "message": result.get("message"),
    }
    expected_identity = {
        "case_id": case.trace_id,
        "trace_id": case.trace_id,
        "ticket_id": case.ticket_id,
        "customer_id": case.customer_id,
        "order_id": case.order_id,
        "message": case.message,
    }
    identity_errors = [
        field
        for field, expected_value in expected_identity.items()
        if observed_identity.get(field) != expected_value
    ]
    if identity_errors:
        result["matched_expectations"] = False
        result["contract_errors"] = [
            "observed workflow identity mismatch: " + ", ".join(identity_errors)
        ]
    if not result.get("matched_expectations"):
        return JSONResponse(status_code=409, content=result)
    return result


@app.post("/api/refund/{case_id}/follow-up")
def customer_followup(case_id: str, req: CustomerFollowupRequest) -> dict[str, Any]:
    """Resume demo10/demo14 from their durable waiting-customer boundary."""

    try:
        case = load_demo_catalog().get(case_id)
    except DemoCatalogError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if case.trace_id not in {"demo10", "demo14"} or case.follow_up is None:
        raise HTTPException(
            status_code=422,
            detail="Customer follow-up is allowlisted only for demo10 and demo14",
        )
    canonical = case.follow_up.request_payload(case)
    if req.model_dump() != canonical:
        raise HTTPException(
            status_code=422,
            detail="Follow-up message and facts must exactly match the canonical demo fixture",
        )

    try:
        mode = _configured_mode()
        result = _create_followup_service(mode).run(case)
    except CustomerFollowupConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except CustomerFollowupExecutionError as error:
        raise HTTPException(
            status_code=502,
            detail="Customer follow-up was recorded, but workflow continuation failed",
        ) from error
    except CustomerFollowupStoreError as error:
        raise HTTPException(
            status_code=503,
            detail="Customer follow-up persistence is unavailable or inconsistent",
        ) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    if not result.get("success") or not result.get("matched_expectations"):
        raise HTTPException(
            status_code=502,
            detail="Customer follow-up failed the strict workflow acceptance contract",
        )
    result["selected_case"] = {
        "case_id": case.trace_id,
        "trace_id": case.trace_id,
        "ticket_id": case.ticket_id,
        "customer_id": case.customer_id,
        "order_id": case.order_id,
        "message": case.follow_up.message,
    }
    result["mode"] = {"workflow": mode, "db": _db_mode()}
    result["execution_boundary"] = {
        "entrypoint": "refund_followup_http_api",
        "database": FINAL_DATABASE,
        "azure": "real",
        "continuation": "customer_to_triage",
    }
    return result


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")
