"""In-memory repository for REFUND_DB=fake (real graph, no DB writes).

Implements exactly the surface the parent pipeline touches:
  - persist_agent_handoff / persist_result  (PipelineStore)
  - save_governance_event_record / get_governance_event_record
    (via DatabaseGovernanceEventRepository)
  - fetch_source_handoffs / record_failure  (defensive)
Everything is kept in memory and thrown away when the process exits.
"""
from __future__ import annotations

from typing import Any


class FakeRepository:
    def __init__(self) -> None:
        self.handoffs: list[dict[str, Any]] = []
        self.results: list[tuple] = []
        self._events: dict[tuple[str, str], Any] = {}

    # --- PipelineStore surface ------------------------------------------------
    def persist_agent_handoff(self, **kwargs: Any) -> str:
        self.handoffs.append(kwargs)
        return str(len(self.handoffs))

    def persist_result(self, *args: Any, **kwargs: Any) -> str:
        self.results.append((args, kwargs))
        return str(len(self.results) + 100)

    def fetch_source_handoffs(self, *_args: Any, **_kwargs: Any) -> list:
        return []

    def record_failure(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    # --- governance event store surface --------------------------------------
    def save_governance_event_record(self, statement: Any) -> str:
        trace_id = getattr(statement, "trace_id", "unknown")
        agent = getattr(statement, "agent", "unknown")
        self._events[(trace_id, agent)] = statement
        return f"{trace_id}:{agent}"

    def get_governance_event_record(self, trace_id: str, agent: str) -> Any:
        return self._events.get((trace_id, agent))
