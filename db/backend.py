from __future__ import annotations

from typing import Any

from governance import GovernanceEventStore, GovernanceStatement


class InMemoryGovernanceEventRepository(GovernanceEventStore):
	"""Simple repository for tests and local wiring before DB persistence is added."""

	def __init__(self) -> None:
		self._events: dict[tuple[str, str], GovernanceStatement] = {}

	def save_event(self, statement: GovernanceStatement) -> str:
		key = (statement.trace_id, statement.agent)
		self._events[key] = statement
		return f"{statement.trace_id}:{statement.agent}"

	def get_event(self, trace_id: str, agent: str) -> GovernanceStatement | None:
		return self._events.get((trace_id, agent))


class DatabaseGovernanceEventRepository(GovernanceEventStore):
	"""Adapter entrypoint for DB-backed governance event persistence."""

	def __init__(self, backend: Any) -> None:
		self.backend = backend

	def save_event(self, statement: GovernanceStatement) -> str:
		return self.backend.save_governance_event_record(statement)

	def get_event(self, trace_id: str, agent: str) -> GovernanceStatement | None:
		payload = self.backend.get_governance_event_record(trace_id, agent)
		if payload is None:
			return None
		if isinstance(payload, GovernanceStatement):
			return payload
		return GovernanceStatement.model_validate(payload)
