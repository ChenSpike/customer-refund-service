from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dashboard_app.api import app, get_approval_resolution_service
from dashboard_app.approval import (
    ApprovalConflict,
    ApprovalContinuationFailed,
    ApprovalNotFound,
    ApprovalServiceUnavailable,
)


VALID_REQUEST = {
    "approval_id": "approval-demo01",
    "decision": "approve",
    "resolved_amount": 79.99,
    "reviewer": "reviewer@example.com",
    "notes": "Verified the damaged item against the order evidence.",
}


class FakeApprovalService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def resolve(self, trace_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"trace_id": trace_id, **kwargs})
        if self.error is not None:
            raise self.error
        return {
            "approval_id": kwargs["approval_id"],
            "trace_id": trace_id,
            "decision": kwargs["decision"],
            "status": "approved",
            "resolved_amount": float(kwargs["resolved_amount"]),
            "next_agent": "refund_agent",
            "continuation_status": "completed",
            "workflow_status": "completed",
            "current_agent": "completed",
            "idempotent": False,
            "new_approval_id": None,
            "refund_result": {"status": "success"},
            "response_result": {"final_outcome": "approved"},
        }


@pytest.fixture
def approval_client() -> tuple[TestClient, FakeApprovalService]:
    fake = FakeApprovalService()
    app.dependency_overrides[get_approval_resolution_service] = lambda: fake
    try:
        with TestClient(app) as client:
            yield client, fake
    finally:
        app.dependency_overrides.pop(get_approval_resolution_service, None)


def test_resolve_approval_requires_explicit_reviewer_and_delegates_once(
    approval_client: tuple[TestClient, FakeApprovalService],
) -> None:
    client, fake = approval_client

    response = client.post("/api/approvals/demo01/resolve", json=VALID_REQUEST)

    assert response.status_code == 200
    assert response.json()["continuation_status"] == "completed"
    assert fake.calls == [
        {
            "trace_id": "demo01",
            "approval_id": "approval-demo01",
            "decision": "approve",
            "resolved_amount": Decimal("79.99"),
            "reviewer": "reviewer@example.com",
            "notes": "Verified the damaged item against the order evidence.",
        }
    ]


@pytest.mark.parametrize("trace_id", ["demo00", "demo21", "demo1", "demo01-extra"])
def test_resolve_approval_rejects_traces_outside_the_exact_demo_set(
    approval_client: tuple[TestClient, FakeApprovalService],
    trace_id: str,
) -> None:
    client, fake = approval_client

    response = client.post(f"/api/approvals/{trace_id}/resolve", json=VALID_REQUEST)

    assert response.status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize(
    "patch",
    [
        {"decision": "Approve"},
        {"decision": "deny", "resolved_amount": 0},
        {"decision": "partial_refund", "resolved_amount": None},
        {"decision": "partial_refund", "resolved_amount": 1.001},
        {"resolved_amount": "79.99"},
        {"reviewer": "   "},
        {"notes": "   "},
        {"unexpected": "field"},
    ],
)
def test_resolve_approval_strictly_validates_the_request(
    approval_client: tuple[TestClient, FakeApprovalService],
    patch: dict[str, Any],
) -> None:
    client, fake = approval_client
    request = {**VALID_REQUEST, **patch}

    response = client.post("/api/approvals/demo01/resolve", json=request)

    assert response.status_code == 422
    assert fake.calls == []


def test_resolve_approval_requires_reviewer_field(
    approval_client: tuple[TestClient, FakeApprovalService],
) -> None:
    client, fake = approval_client
    request = dict(VALID_REQUEST)
    request.pop("reviewer")

    response = client.post("/api/approvals/demo01/resolve", json=request)

    assert response.status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (ApprovalNotFound("demo01: approval does not exist"), 404, "approval does not exist"),
        (ApprovalConflict("demo01: approval was already resolved"), 409, "already resolved"),
        (ApprovalServiceUnavailable("database offline"), 503, "service is unavailable"),
        (ApprovalContinuationFailed("response failed"), 502, "continuation failed"),
    ],
)
def test_resolve_approval_maps_domain_failures(
    approval_client: tuple[TestClient, FakeApprovalService],
    error: Exception,
    status: int,
    detail: str,
) -> None:
    client, fake = approval_client
    fake.error = error

    response = client.post("/api/approvals/demo01/resolve", json=VALID_REQUEST)

    assert response.status_code == status
    assert detail in response.json()["detail"]


def test_approval_cors_preflight_allows_post(
    approval_client: tuple[TestClient, FakeApprovalService],
) -> None:
    client, _ = approval_client

    response = client.options(
        "/api/approvals/demo01/resolve",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert "POST" in response.headers["access-control-allow-methods"]


def test_dashboard_cors_preflight_accepts_loopback_ip_alias(
    approval_client: tuple[TestClient, FakeApprovalService],
) -> None:
    client, _ = approval_client

    response = client.options(
        "/api/approvals/demo01/resolve",
        headers={
            "Origin": "http://127.0.0.1:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert "POST" in response.headers["access-control-allow-methods"]
