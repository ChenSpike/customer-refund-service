"""
FastAPI backend for AI Governance workflow.
Agents call these endpoints to log actions, query state, and request human approval.
"""

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any, List, Literal
from db_service import DBService, set_current_oidc_token
import case_service
import os
from dotenv import load_dotenv
import json

load_dotenv()

app = FastAPI(
    title="AI Governance Workflow API",
    description="Backend for refund workflow governance tracking",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def sync_gcp_oidc_token(request: Request, call_next):
    """Keeps DBService's in-memory OIDC token current so its federated GCP
    credentials always exchange a fresh one. In production Vercel Functions
    the token arrives per-request as the x-vercel-oidc-token header (NOT an
    env var); locally, `vercel env pull` seeds VERCEL_OIDC_TOKEN into
    .env.local instead, since there's no Vercel edge in front of a local
    server."""
    token = request.headers.get("x-vercel-oidc-token") or os.environ.get("VERCEL_OIDC_TOKEN")
    if token:
        set_current_oidc_token(token)
    return await call_next(request)


_db_host = os.getenv("DB_HOST")
if _db_host:
    # Direct connection mode: DB_HOST/DB_USER/DB_PASSWORD reach the MySQL
    # instance over plain TCP (local dev, or a Cloud SQL instance that's
    # directly reachable), no GCP Workload Identity Federation needed.
    required_env_vars = ["DB_HOST", "DB_USER", "DB_PASSWORD"]
else:
    # Cloud SQL Connector + Workload Identity Federation (production/Vercel).
    required_env_vars = [
        "GCP_PROJECT_NUMBER",
        "GCP_SERVICE_ACCOUNT_EMAIL",
        "GCP_WORKLOAD_IDENTITY_POOL_ID",
        "GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID",
        "DB_INSTANCE_CONNECTION_NAME",
        "DB_USER",
        "DB_PASSWORD",
    ]
missing_env_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_env_vars:
    raise RuntimeError(
        f"Missing required environment variable(s): {', '.join(missing_env_vars)}. "
        "Copy .env.example to .env and fill in either DB_HOST/DB_USER/DB_PASSWORD "
        "(direct connection) or the GCP_*/DB_INSTANCE_CONNECTION_NAME set (Cloud SQL Connector)."
    )

# Initialize database service. DBService no longer connects eagerly, so it's
# safe to construct here at import time even though no OIDC token has been
# synced yet, the first real request runs the middleware above first.
db = DBService(
    instance_connection_name=os.getenv("DB_INSTANCE_CONNECTION_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    database=os.getenv("DB_NAME", "main_db"),
    host=_db_host,
    port=int(os.getenv("DB_PORT", "3306")),
)

# ========== Pydantic Models ==========

class WorkflowStartRequest(BaseModel):
    ticket_id: str
    policy_version: Optional[str] = "1.0"

class WorkflowStatusUpdate(BaseModel):
    status: str
    current_agent: Optional[str] = None

class HandoffLog(BaseModel):
    trace_id: str
    from_agent: str
    to_agent: str
    input_json: Dict[str, Any]
    output_json: Dict[str, Any]

class GovernanceEventLog(BaseModel):
    trace_id: str
    agent: str
    owasp_category: str
    interceptor_action: str
    trigger_score: Optional[float] = None
    flags: Optional[Dict[str, Any]] = None
    offending_content: Optional[str] = None

class HumanApprovalRequest(BaseModel):
    trace_id: str
    reason: str
    amount_requested: Optional[float] = None
    triggering_event_id: Optional[str] = None

class HumanApprovalResolve(BaseModel):
    status: str  # approved | rejected
    reviewer: str
    notes: Optional[str] = None

class AuditLogEntry(BaseModel):
    trace_id: str
    event_type: str
    agent: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None

class RefundTransactionCreate(BaseModel):
    trace_id: str
    amount: float
    currency: Optional[str] = "USD"
    approval_id: Optional[str] = None

class RefundTransactionUpdate(BaseModel):
    status: str  # pending | issued | failed | blocked
    external_ref: Optional[str] = None

class CaseDecisionRequest(BaseModel):
    action: Literal["approve", "partial_refund", "deny", "request_info", "confirm_block", "release"]
    reviewer: str = "Admin"
    notes: Optional[str] = None
    amount: Optional[float] = None  # required for partial_refund; optional override for approve

# ========== Health Check ==========

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}

# ========== Workflow Management ==========

@app.post("/api/workflows/start")
async def start_workflow(request: WorkflowStartRequest) -> Dict[str, str]:
    """Start a new workflow run (called by Customer Ticket node)."""
    try:
        trace_id = db.create_workflow_run(request.ticket_id, request.policy_version)
        return {"trace_id": trace_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/workflows/{trace_id}")
async def get_workflow(trace_id: str) -> Dict[str, Any]:
    """Get workflow run details and status."""
    try:
        run = db.get_workflow_run(trace_id)
        if not run:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return dict(run) if hasattr(run, 'keys') else run
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/workflows/{trace_id}/status")
async def update_workflow_status(trace_id: str, request: WorkflowStatusUpdate) -> Dict[str, str]:
    """Update workflow status and current agent."""
    try:
        db.update_workflow_status(trace_id, request.status, request.current_agent)
        return {"status": "updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Agent Handoffs ==========

@app.post("/api/handoffs")
async def log_handoff(request: HandoffLog) -> Dict[str, str]:
    """Log an agent handoff (input/output at agent boundary)."""
    try:
        handoff_id = db.log_agent_handoff(
            request.trace_id, request.from_agent, request.to_agent,
            request.input_json, request.output_json
        )
        return {"handoff_id": handoff_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/workflows/{trace_id}/handoffs")
async def get_handoffs(trace_id: str) -> List[Dict]:
    """Get all handoffs for a workflow run."""
    try:
        handoffs = db.get_handoffs_for_run(trace_id)
        return [dict(h) if hasattr(h, 'keys') else h for h in handoffs]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Governance Events ==========

@app.post("/api/governance-events")
async def log_governance_event(request: GovernanceEventLog) -> Dict[str, str]:
    """Log a governance event (OWASP detection, quarantine, block)."""
    try:
        event_id = db.log_governance_event(
            request.trace_id, request.agent, request.owasp_category,
            request.interceptor_action, request.trigger_score,
            request.flags, request.offending_content
        )
        return {"event_id": event_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/workflows/{trace_id}/governance")
async def get_governance_events(trace_id: str) -> List[Dict]:
    """Get all governance events for a workflow run."""
    try:
        events = db.get_governance_events_for_run(trace_id)
        return [dict(e) if hasattr(e, 'keys') else e for e in events]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/governance-events")
async def list_governance_events(
    owasp_category: Optional[str] = Query(None),
    interceptor_action: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000)
) -> List[Dict]:
    """Query governance events across all workflow runs (dashboard feed)."""
    try:
        events = db.query_governance_events(owasp_category, interceptor_action, agent, limit)
        return [dict(e) if hasattr(e, 'keys') else e for e in events]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Human-in-the-Loop (HITL) Queue ==========

@app.post("/api/approvals")
async def create_approval(request: HumanApprovalRequest) -> Dict[str, str]:
    """Create a human approval request (pause & wait)."""
    try:
        approval_id = db.create_human_approval(
            request.trace_id, request.reason, request.amount_requested,
            request.triggering_event_id
        )
        return {"approval_id": approval_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/approvals/pending")
async def get_pending_approvals(limit: int = Query(10, ge=1, le=100)) -> List[Dict]:
    """Get pending human approvals (HITL queue)."""
    try:
        approvals = db.get_pending_approvals(limit)
        return [dict(a) if hasattr(a, 'keys') else a for a in approvals]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/approvals/{approval_id}")
async def resolve_approval(approval_id: str, request: HumanApprovalResolve) -> Dict[str, str]:
    """Resolve a human approval (approved/rejected)."""
    try:
        db.resolve_human_approval(approval_id, request.status, request.reviewer, request.notes)
        return {"status": "resolved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Audit Log ==========

@app.post("/api/audit-log")
async def log_audit(request: AuditLogEntry) -> Dict[str, str]:
    """Append to audit log."""
    try:
        db.log_audit_event(request.trace_id, request.event_type, request.agent, request.payload)
        return {"status": "logged"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/audit-log/query")
async def query_audit(
    trace_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000)
) -> List[Dict]:
    """Query audit log with optional filters. Each row is enriched with a
    human-readable `summary`/`category`/`actor` for display (raw fields kept too)."""
    try:
        logs = db.query_audit_log(trace_id, event_type, agent, limit)
        rows = [dict(l) if hasattr(l, 'keys') else l for l in logs]
        for row in rows:
            row.update(case_service.summarize_audit_row(row))
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Refund Transactions ==========

@app.post("/api/refund-transactions")
async def create_refund_transaction(request: RefundTransactionCreate) -> Dict[str, str]:
    """Create a refund transaction (pending)."""
    try:
        transaction_id = db.create_refund_transaction(
            request.trace_id, request.amount, request.currency, request.approval_id
        )
        return {"transaction_id": transaction_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/refund-transactions/{transaction_id}")
async def update_refund_transaction(
    transaction_id: str, request: RefundTransactionUpdate
) -> Dict[str, str]:
    """Update refund transaction status."""
    try:
        db.update_refund_transaction(transaction_id, request.status, request.external_ref)
        return {"status": "updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Cases (Refund Ops Console) ==========

@app.get("/api/cases")
async def list_cases(limit: int = Query(200, ge=1, le=500)) -> List[Dict]:
    """List refund cases for the console's case queue, one per workflow run."""
    try:
        return case_service.build_case_list(db, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/cases/{trace_id}")
async def get_case(trace_id: str) -> Dict[str, Any]:
    """Full case detail: pipeline, order facts, policy evaluation, governance panel, timeline."""
    try:
        bundle = db.get_case_bundle(trace_id)
        if not bundle:
            raise HTTPException(status_code=404, detail="Case not found")
        return case_service.CaseAggregator.build_case_detail(bundle)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/cases/{trace_id}/decision")
async def decide_case(trace_id: str, request: CaseDecisionRequest) -> Dict[str, Any]:
    """Apply an admin action (approve/partial_refund/deny/request_info/confirm_block/release) to a case."""
    try:
        bundle = db.get_case_bundle(trace_id)
        if not bundle:
            raise HTTPException(status_code=404, detail="Case not found")
        db.apply_case_decision(trace_id, request.action, request.reviewer, request.notes, request.amount)
        updated = db.get_case_bundle(trace_id)
        return case_service.CaseAggregator.build_case_detail(updated)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/console-metrics")
async def get_console_metrics() -> Dict[str, Any]:
    """Aggregate metrics for the console's Metrics page."""
    try:
        bundles = case_service.build_all_case_bundles(db, limit=500)
        cases = [case_service.CaseAggregator.build_case_summary(b) for b in bundles]
        audit_rows = [row for b in bundles for row in b["audit_log"]]
        approval_rows = [row for b in bundles for row in b["approvals"]]
        governance_rows = [row for b in bundles for row in b["governance_events"]]
        return case_service.CaseAggregator.build_metrics(cases, audit_rows, approval_rows, governance_rows)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Dashboard Stats ==========

@app.get("/api/dashboard/stats")
async def get_dashboard_stats() -> Dict[str, Any]:
    """Get dashboard statistics for overview."""
    try:
        # Query audit log to compute stats
        all_logs = db.query_audit_log(limit=1000)

        stats = {
            "total_workflows": len(set(l['trace_id'] for l in all_logs if l.get('trace_id'))),
            "governance_events": len([l for l in all_logs if 'governance' in l.get('event_type', '').lower()]),
            "pending_approvals": len(db.get_pending_approvals(limit=1000)),
            "total_audit_entries": len(all_logs)
        }
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", 8000)),
        reload=os.getenv("DEBUG", "False") == "True"
    )
