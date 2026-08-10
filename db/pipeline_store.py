from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents.policy.models import PolicyAgentInput, PolicyAgentOutput, PolicyReasoningResult, PrecedentContext, TokenUsage
from db.database import GCPRepository
from governance import GovernanceAssessment


@dataclass(frozen=True)
class PolicyPersistenceArtifacts:
	handoff_id: str
	trace_id: str
	governance_event_count: int
	next_agent: str


class PipelineStore:
	"""Top-level persistence adapter for writing policy results from parent app state."""

	def __init__(self, repository: GCPRepository) -> None:
		self.repository = repository

	@classmethod
	def from_env(cls) -> "PipelineStore":
		return cls(GCPRepository.from_env())

	def persist_policy_state(self, state: dict[str, Any]) -> PolicyPersistenceArtifacts:
		# Lazy import breaks the agents.policy -> db.pipeline_store -> agents.policy
		# cycle (these names are only defined late in agents/policy/__init__.py).
		from agents.policy import policy_input_from_state, policy_output_from_state, policy_result_from_state

		policy_input = policy_input_from_state(state)
		policy_output = policy_output_from_state(state)
		_policy_input, policy_result = policy_result_from_state(state)
		governance = GovernanceAssessment.model_validate(
			{
				"governance": policy_output.governance.model_dump(mode="json"),
				"findings": state.get("policy_governance_result", {}).get("findings", []),
			}
		)
		usage = self._workflow_usage(state)
		precedent_context = self._precedent_context(state)
		handoff_id = self.persist_policy_artifacts(
			policy_input=policy_input,
			policy_output=policy_output,
			policy_result=policy_result,
			precedent_context=precedent_context,
			governance_assessment=governance,
			usage=usage,
		)
		return PolicyPersistenceArtifacts(
			handoff_id=handoff_id,
			trace_id=policy_input.case.trace_id,
			governance_event_count=len(governance.findings),
			next_agent=policy_output.handoff.next_agent,
		)

	def persist_policy_artifacts(
		self,
		*,
		policy_input: PolicyAgentInput,
		policy_output: PolicyAgentOutput,
		policy_result: PolicyReasoningResult,
		precedent_context: PrecedentContext,
		governance_assessment: GovernanceAssessment,
		usage: TokenUsage,
	) -> str:
		return self.repository.persist_result(
			policy_input,
			policy_output,
			policy_result,
			precedent_context,
			governance_assessment.findings,
			usage,
		)

	def _workflow_usage(self, state: dict[str, Any]) -> TokenUsage:
		from agents.policy import policy_usage_from_state

		return policy_usage_from_state(state)

	def _precedent_context(self, state: dict[str, Any]) -> PrecedentContext:
		value = state.get("precedent_context") or {
			"available": False,
			"status": "not_requested",
			"reason": "parent_state_missing_precedent_context",
			"records": [],
		}
		return PrecedentContext.model_validate(value)


def persist_policy_state(state: dict[str, Any], repository: GCPRepository | None = None) -> PolicyPersistenceArtifacts:
	store = PipelineStore(repository or GCPRepository.from_env())
	return store.persist_policy_state(state)
