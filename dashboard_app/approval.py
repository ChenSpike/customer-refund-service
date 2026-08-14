"""Dashboard boundary for the canonical human-approval lifecycle.

The dashboard owns request/response concerns only.  All approval persistence,
workflow routing, refund execution, and response continuation stay inside
``app.review.HumanApprovalService`` and the canonical ``GCPRepository``.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol

from .repository import require_final_database_name


class ApprovalNotFound(LookupError):
    """The requested workflow or approval does not exist."""


class ApprovalConflict(RuntimeError):
    """The approval cannot be resolved because its durable state changed."""


class ApprovalServiceUnavailable(RuntimeError):
    """The approval repository is unavailable or violates its contract."""


class ApprovalContinuationFailed(RuntimeError):
    """The review decision was persisted but its downstream workflow failed."""


class ApprovalValidationError(ValueError):
    """Lifecycle validation rejected a request that passed HTTP validation."""


class ApprovalLifecycle(Protocol):
    def resolve(
        self,
        trace_id: str,
        *,
        decision: str,
        resolved_amount: Decimal | None,
        reviewer: str,
        notes: str,
        approval_id: str | None = None,
    ) -> Any: ...


class ApprovalResolutionService:
    """Translate lifecycle outcomes and failures into dashboard domain types."""

    def __init__(self, lifecycle: ApprovalLifecycle) -> None:
        self.lifecycle = lifecycle

    def resolve(
        self,
        trace_id: str,
        *,
        approval_id: str,
        decision: str,
        resolved_amount: Decimal | None,
        reviewer: str,
        notes: str,
    ) -> dict[str, Any]:
        try:
            outcome = self.lifecycle.resolve(
                trace_id,
                approval_id=approval_id,
                decision=decision,
                resolved_amount=resolved_amount,
                reviewer=reviewer,
                notes=notes,
            )
        except ValueError as exc:
            raise ApprovalValidationError(str(exc)) from exc
        except Exception as exc:
            self._raise_mapped_service_error(exc)

        if hasattr(outcome, "as_dict"):
            result = outcome.as_dict()
        elif isinstance(outcome, Mapping):
            result = dict(outcome)
        else:
            raise ApprovalServiceUnavailable(
                "Human approval service returned an unsupported result"
            )
        return dict(result)

    @staticmethod
    def _raise_mapped_service_error(exc: Exception) -> None:
        # Imports remain lazy so cloud-free dashboard tests can override the
        # approval dependency without constructing the workflow/Azure stack.
        from app.review import (
            HumanApprovalConflictError,
            HumanApprovalNotFoundError,
            HumanApprovalStateError,
            ReviewContinuationError,
        )
        from db.database import CloudDatabaseError

        if isinstance(exc, ReviewContinuationError):
            raise ApprovalContinuationFailed(str(exc)) from exc
        if isinstance(exc, HumanApprovalNotFoundError):
            raise ApprovalNotFound(str(exc)) from exc
        if isinstance(exc, (HumanApprovalConflictError, HumanApprovalStateError)):
            raise ApprovalConflict(str(exc)) from exc
        if isinstance(exc, CloudDatabaseError):
            raise ApprovalServiceUnavailable(str(exc)) from exc
        raise ApprovalServiceUnavailable(str(exc)) from exc


def build_approval_resolution_service() -> ApprovalResolutionService:
    """Build the production lifecycle with one canonical repository instance."""

    try:
        from app.review import HumanApprovalService
        from db.database import GCPRepository

        repository = GCPRepository.from_env()
        require_final_database_name(repository.database_name)
        return ApprovalResolutionService(HumanApprovalService(repository))
    except Exception as exc:
        raise ApprovalServiceUnavailable(
            "Human approval service configuration is unavailable"
        ) from exc


__all__ = [
    "ApprovalConflict",
    "ApprovalContinuationFailed",
    "ApprovalNotFound",
    "ApprovalResolutionService",
    "ApprovalServiceUnavailable",
    "ApprovalValidationError",
    "build_approval_resolution_service",
]
