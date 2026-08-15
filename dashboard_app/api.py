"""FastAPI entrypoint for the operations dashboard.

Run with ``python -m uvicorn dashboard_app.api:app --port 8000``.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .approval import (
    ApprovalConflict,
    ApprovalContinuationFailed,
    ApprovalNotFound,
    ApprovalResolutionService,
    ApprovalServiceUnavailable,
    ApprovalValidationError,
    build_approval_resolution_service,
)
from .repository import DashboardRepository, DashboardRepositoryError, database_config_status
from .service import DashboardDataError, DashboardNotFound, DashboardService


DEMO_TRACE_PATTERN = r"^demo(?:0[1-9]|1[0-9]|20)$"


class ApprovalResolutionRequest(BaseModel):
    """Explicit reviewer decision for one persisted demo approval."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approval_id: str = Field(min_length=1, max_length=255)
    decision: Literal["approve", "partial_refund", "deny"]
    resolved_amount: Decimal | None = None
    reviewer: str = Field(min_length=1, max_length=255)
    notes: str = Field(min_length=1, max_length=4000)

    @field_validator("resolved_amount", mode="before")
    @classmethod
    def validate_amount_shape(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("resolved_amount must be a JSON number")
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("resolved_amount must be a finite monetary value") from exc
        if not amount.is_finite():
            raise ValueError("resolved_amount must be a finite monetary value")
        try:
            quantized = amount.quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError(
                "resolved_amount is outside the supported monetary range"
            ) from exc
        if amount != quantized:
            raise ValueError("resolved_amount must have at most two decimal places")
        return quantized

    @model_validator(mode="after")
    def validate_decision_amount(self) -> "ApprovalResolutionRequest":
        if self.decision == "deny" and self.resolved_amount is not None:
            raise ValueError("deny decisions cannot include resolved_amount")
        if self.decision == "partial_refund" and (
            self.resolved_amount is None or self.resolved_amount <= 0
        ):
            raise ValueError("partial_refund requires a positive resolved_amount")
        if self.decision == "approve" and (
            self.resolved_amount is not None and self.resolved_amount <= 0
        ):
            raise ValueError("approve resolved_amount must be positive when provided")
        return self


def _cors_origins() -> list[str]:
    configured = os.getenv(
        "DASHBOARD_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app = FastAPI(
    title="Customer Refund Operations Dashboard",
    description="Canonical workflow views with controlled human-approval resolution",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def get_dashboard_service() -> DashboardService:
    return DashboardService(DashboardRepository.from_env())


ServiceDependency = Annotated[DashboardService, Depends(get_dashboard_service)]


def get_approval_resolution_service() -> ApprovalResolutionService:
    return build_approval_resolution_service()


ApprovalServiceDependency = Annotated[
    ApprovalResolutionService,
    Depends(get_approval_resolution_service),
]


@app.exception_handler(DashboardNotFound)
async def handle_not_found(_: Request, exc: DashboardNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": f"Workflow {exc.args[0]} was not found"})


@app.exception_handler(DashboardRepositoryError)
async def handle_repository_error(_: Request, exc: DashboardRepositoryError) -> JSONResponse:
    cause = exc.__cause__
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Dashboard database is unavailable",
            "error_type": type(cause).__name__ if cause else type(exc).__name__,
        },
    )


@app.exception_handler(DashboardDataError)
async def handle_data_error(_: Request, exc: DashboardDataError) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"detail": "Persisted workflow data violates the dashboard contract", "reason": str(exc)},
    )


@app.exception_handler(ApprovalNotFound)
async def handle_approval_not_found(_: Request, exc: ApprovalNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc) or "Approval was not found"})


@app.exception_handler(ApprovalConflict)
async def handle_approval_conflict(_: Request, exc: ApprovalConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc) or "Approval state conflict"})


@app.exception_handler(ApprovalValidationError)
async def handle_approval_validation(_: Request, exc: ApprovalValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(ApprovalContinuationFailed)
async def handle_approval_continuation_failure(
    _: Request,
    exc: ApprovalContinuationFailed,
) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "detail": "Approval was recorded, but workflow continuation failed",
            "error_type": type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        },
    )


@app.exception_handler(ApprovalServiceUnavailable)
async def handle_approval_service_error(
    _: Request,
    exc: ApprovalServiceUnavailable,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Human approval service is unavailable",
            "error_type": type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        },
    )


def _health_response() -> JSONResponse:
    config = database_config_status()
    if config["status"] != "ok":
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "api": {"status": "ok"},
                "config": config,
                "database": {"status": "not_checked"},
            },
        )
    try:
        database = DashboardRepository.from_env().ping()
    except DashboardRepositoryError as exc:
        cause = exc.__cause__
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "api": {"status": "ok"},
                "config": config,
                "database": {
                    "status": "error",
                    "error_type": type(cause).__name__ if cause else type(exc).__name__,
                },
            },
        )
    if database.get("status") != "ok" or database.get("database") != "final":
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "api": {"status": "ok"},
                "config": config,
                "database": database,
            },
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "api": {"status": "ok"}, "config": config, "database": database},
    )


@app.get("/health", tags=["system"])
def health() -> JSONResponse:
    return _health_response()


@app.get("/api/health", tags=["system"])
def api_health() -> JSONResponse:
    return _health_response()


@app.get("/api/cases", tags=["cases"])
def list_cases(
    service: ServiceDependency,
    limit: int = Query(default=200, ge=1, le=500),
) -> list[dict]:
    return service.list_cases(limit)


@app.get("/api/cases/{trace_id}", tags=["cases"])
def get_case(trace_id: str, service: ServiceDependency) -> dict:
    return service.get_case(trace_id)


@app.get("/api/console-metrics", tags=["metrics"])
def console_metrics(
    service: ServiceDependency,
    limit: int = Query(default=500, ge=1, le=500),
) -> dict:
    return service.metrics(limit)


@app.get("/api/audit-log", tags=["audit"])
@app.get("/api/audit-log/query", tags=["audit"], include_in_schema=False)
def query_audit_log(
    service: ServiceDependency,
    trace_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict]:
    return service.audit(trace_id=trace_id, event_type=event_type, agent=agent, limit=limit)


@app.get("/api/governance-events", tags=["governance"])
def query_governance_events(
    service: ServiceDependency,
    trace_id: str | None = Query(default=None),
    owasp_category: str | None = Query(default=None),
    interceptor_action: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict]:
    return service.governance(
        trace_id=trace_id,
        owasp_category=owasp_category,
        interceptor_action=interceptor_action,
        agent=agent,
        limit=limit,
    )


@app.get("/api/approvals/pending", tags=["approvals"])
def pending_approvals(
    service: ServiceDependency,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return service.pending_approvals(limit)


@app.post("/api/approvals/{trace_id}/resolve", tags=["approvals"])
def resolve_pending_approval(
    payload: ApprovalResolutionRequest,
    service: ApprovalServiceDependency,
    trace_id: Annotated[str, Path(pattern=DEMO_TRACE_PATTERN)],
) -> dict[str, Any]:
    """Resolve one demo approval through the canonical lifecycle service.

    ``reviewer`` is mandatory and intentionally has no server-side default: in
    this demo it is the explicit identity attached to the durable review event.
    Repeating the exact request is safe because the lifecycle is idempotent.
    """

    return service.resolve(
        trace_id,
        approval_id=payload.approval_id,
        decision=payload.decision,
        resolved_amount=payload.resolved_amount,
        reviewer=payload.reviewer,
        notes=payload.notes,
    )
