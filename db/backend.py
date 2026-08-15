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
		# Governance nodes receive only a statement, not the graph state.  The
		# request-local fences keep continuation writes tied to the newest lease
		# without changing the shared governance contract.
		from db.approval_context import active_approval_continuation_fence
		from db.followup_context import active_followup_fence

		followup_fence = active_followup_fence()
		approval_fence = active_approval_continuation_fence()
		if followup_fence is not None and approval_fence is not None:
			raise RuntimeError("governance write has overlapping continuation fences")
		if followup_fence is not None:
			if followup_fence.trace_id != statement.trace_id:
				raise RuntimeError(
					"customer follow-up governance write crossed its trace fence"
				)
			return self.backend.save_governance_event_record(
				statement,
				followup_claim_token=followup_fence.claim_token,
			)
		if approval_fence is not None:
			if approval_fence.trace_id != statement.trace_id:
				raise RuntimeError(
					"human-approval governance write crossed its trace fence"
				)
			return self.backend.save_governance_event_record(
				statement,
				approval_claim_token=approval_fence.claim_token,
			)
		return self.backend.save_governance_event_record(statement)

	def get_event(self, trace_id: str, agent: str) -> GovernanceStatement | None:
		payload = self.backend.get_governance_event_record(trace_id, agent)
		if payload is None:
			return None
		if isinstance(payload, GovernanceStatement):
			return payload
		return GovernanceStatement.model_validate(payload)
