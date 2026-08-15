"""Request-local fencing context for human-approval continuations."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ApprovalContinuationFence:
    trace_id: str
    approval_id: str
    claim_token: str
    attempt: int
    sequence: int


_ACTIVE_FENCE: ContextVar[ApprovalContinuationFence | None] = ContextVar(
    "human_approval_continuation_fence",
    default=None,
)


@contextmanager
def approval_continuation_fence(
    *,
    trace_id: str,
    approval_id: str,
    claim_token: str,
    attempt: int,
    sequence: int,
) -> Iterator[None]:
    token = _ACTIVE_FENCE.set(
        ApprovalContinuationFence(
            trace_id=trace_id,
            approval_id=approval_id,
            claim_token=claim_token,
            attempt=attempt,
            sequence=sequence,
        )
    )
    try:
        yield
    finally:
        _ACTIVE_FENCE.reset(token)


def active_approval_continuation_fence() -> ApprovalContinuationFence | None:
    return _ACTIVE_FENCE.get()


__all__ = [
    "ApprovalContinuationFence",
    "active_approval_continuation_fence",
    "approval_continuation_fence",
]
