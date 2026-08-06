from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseGovernanceNode(ABC):
    """Shared governance contract for agent-specific governance implementations."""

    @abstractmethod
    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        """Evaluate governance for an agent-specific state payload."""
