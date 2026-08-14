from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.review import (
    HumanApprovalConflictError,
    HumanApprovalNotFoundError,
    HumanApprovalStateError,
    ReviewContinuationError,
)
from dashboard_app.approval import (
    ApprovalConflict,
    ApprovalContinuationFailed,
    ApprovalNotFound,
    ApprovalResolutionService,
    ApprovalServiceUnavailable,
    ApprovalValidationError,
    build_approval_resolution_service,
)
from db.database import CloudDatabaseError


@dataclass
class Outcome:
    trace_id: str = "demo01"

    def as_dict(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "continuation_status": "completed"}


class Lifecycle:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = Outcome() if result is None else result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def resolve(self, trace_id: str, **kwargs: Any) -> Any:
        self.calls.append({"trace_id": trace_id, **kwargs})
        if self.error is not None:
            raise self.error
        return self.result


REQUEST = {
    "approval_id": "approval-demo01",
    "decision": "deny",
    "resolved_amount": None,
    "reviewer": "reviewer@example.com",
    "notes": "Evidence does not support a refund.",
}


def test_approval_adapter_calls_only_the_lifecycle_service() -> None:
    lifecycle = Lifecycle()
    service = ApprovalResolutionService(lifecycle)

    result = service.resolve("demo01", **REQUEST)

    assert result == {"trace_id": "demo01", "continuation_status": "completed"}
    assert lifecycle.calls == [{"trace_id": "demo01", **REQUEST}]


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (HumanApprovalNotFoundError("missing"), ApprovalNotFound),
        (HumanApprovalConflictError("stale"), ApprovalConflict),
        (HumanApprovalStateError("unsafe state"), ApprovalConflict),
        (ReviewContinuationError("continuation failed"), ApprovalContinuationFailed),
        (CloudDatabaseError("database offline"), ApprovalServiceUnavailable),
        (ValueError("invalid amount"), ApprovalValidationError),
    ],
)
def test_approval_adapter_maps_lifecycle_errors(
    source: Exception,
    target: type[Exception],
) -> None:
    service = ApprovalResolutionService(Lifecycle(error=source))

    with pytest.raises(target):
        service.resolve("demo01", **REQUEST)


def test_approval_adapter_rejects_an_invalid_lifecycle_result() -> None:
    service = ApprovalResolutionService(Lifecycle(result=object()))

    with pytest.raises(ApprovalServiceUnavailable, match="unsupported result"):
        service.resolve("demo01", **REQUEST)


def test_approval_factory_rejects_a_repository_outside_final(monkeypatch) -> None:
    class UnsafeRepository:
        database_name = "main_db"

    monkeypatch.setattr(
        "db.database.GCPRepository.from_env",
        classmethod(lambda _cls: UnsafeRepository()),
    )

    with pytest.raises(
        ApprovalServiceUnavailable,
        match="configuration is unavailable",
    ) as captured:
        build_approval_resolution_service()

    assert "restricted to the final database" in str(captured.value.__cause__)
