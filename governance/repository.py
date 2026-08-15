from __future__ import annotations

from typing import Protocol

from governance.models import GovernanceStatement


class GovernanceEventWriter(Protocol):
    def save_event(self, statement: GovernanceStatement) -> str:
        """Persist a governance event and return its identifier."""


class GovernanceEventReader(Protocol):
    def get_event(self, trace_id: str, agent: str) -> GovernanceStatement | None:
        """Load a governance event by trace and agent."""


class GovernanceEventStore(GovernanceEventWriter, GovernanceEventReader, Protocol):
    pass