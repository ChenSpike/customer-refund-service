"""Request-local fencing context for customer follow-up graph writes."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class FollowupFence:
    trace_id: str
    claim_token: str


_ACTIVE_FENCE: ContextVar[FollowupFence | None] = ContextVar(
    "customer_followup_fence",
    default=None,
)


@contextmanager
def followup_fence(trace_id: str, claim_token: str) -> Iterator[None]:
    token = _ACTIVE_FENCE.set(FollowupFence(trace_id=trace_id, claim_token=claim_token))
    try:
        yield
    finally:
        _ACTIVE_FENCE.reset(token)


def active_followup_fence() -> FollowupFence | None:
    return _ACTIVE_FENCE.get()


__all__ = ["FollowupFence", "active_followup_fence", "followup_fence"]
