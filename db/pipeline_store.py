from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents.policy.models import (
    PolicyAgentInput,
    PolicyAgentOutput,
    PolicyReasoningResult,
    PrecedentContext,
    TokenUsage,
)
from agents.policy.routing import parent_agent_for_route
from db.database import GCPRepository
from governance import GovernanceAssessment


@dataclass(frozen=True)
class PolicyPersistenceArtifacts:
    handoff_id: str
    trace_id: str
    next_agent: str
    policy_review_event_count: int
    governance_event_count: int
    human_approval_count: int

    def state_patch(self) -> dict[str, Any]:
        return {
            "current_stage": "policy_persistence",
            "policy_persistence_result": {
                "handoff_id": self.handoff_id,
                "trace_id": self.trace_id,
                "next_agent": self.next_agent,
                "policy_review_event_count": self.policy_review_event_count,
                "governance_event_count": self.governance_event_count,
                "human_approval_count": self.human_approval_count,
            },
        }


class PipelineStore:
    """Own the single transactional write for a completed Policy subgraph."""

    def __init__(self, repository: GCPRepository) -> None:
        self.repository = repository

    @classmethod
    def from_env(cls) -> "PipelineStore":
        return cls(GCPRepository.from_env())

    def persist_policy_state(self, state: dict[str, Any]) -> PolicyPersistenceArtifacts:
        from agents.policy.policy_node import (
            policy_output_from_state,
            policy_usage_from_state,
            reconstruct_policy_state,
        )

        reconstructed = reconstruct_policy_state(state)
        policy_output = policy_output_from_state(state, reconstructed)
        _validate_policy_handoff(state, policy_output)
        governance = GovernanceAssessment.model_validate(
            {
                "governance": policy_output.governance.model_dump(mode="json"),
                "findings": state["policy_governance_result"]["findings"],
            }
        )
        usage = policy_usage_from_state(state)
        handoff_id = self.persist_policy_artifacts(
            policy_input=reconstructed.policy_input,
            policy_output=policy_output,
            policy_result=reconstructed.policy_result,
            precedent_context=reconstructed.precedent_context,
            governance_assessment=governance,
            usage=usage,
        )
        next_agent = policy_output.handoff.next_agent
        return PolicyPersistenceArtifacts(
            handoff_id=handoff_id,
            trace_id=reconstructed.policy_input.case.trace_id,
            next_agent=next_agent,
            policy_review_event_count=int(policy_output.decision.type == "manual_review"),
            governance_event_count=len(governance.findings),
            human_approval_count=int(next_agent == "human_approval"),
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


class PolicyPersistenceNode:
    def __init__(self, store: PipelineStore) -> None:
        self.store = store

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.store.persist_policy_state(state).state_patch()


def persist_policy_state(
    state: dict[str, Any],
    repository: GCPRepository | None = None,
) -> PolicyPersistenceArtifacts:
    return PipelineStore(repository or GCPRepository.from_env()).persist_policy_state(state)


def _validate_policy_handoff(
    state: dict[str, Any],
    output: PolicyAgentOutput,
) -> None:
    handoff = state.get("policy_handoff")
    if handoff not in {"refund", "response", "human_review"}:
        raise ValueError("policy_handoff must be present before Policy persistence")
    if parent_agent_for_route(handoff) != output.handoff.next_agent:
        raise ValueError("policy_handoff disagrees with the validated Policy output")
