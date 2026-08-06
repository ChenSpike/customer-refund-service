from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from governance import GovernanceAssessment

from .cloud_db import GCPRepository, RunMode
from .graph import build_policy_agent_graph
from .models import (
    PolicyAgentOutput,
    PolicyReasoningResult,
    PrecedentContext,
    TokenUsage,
    exact_policy_input,
)


@dataclass(frozen=True)
class ProcessedCase:
    handoff_id: str
    output: PolicyAgentOutput
    usage: TokenUsage
    policy_usage: TokenUsage
    governance_usage: TokenUsage


class PolicyAgentService:
    def __init__(self, repository: GCPRepository, graph: Any) -> None:
        self.repository = repository
        self.graph = graph

    @classmethod
    def from_env(cls) -> "PolicyAgentService":
        return cls(GCPRepository.from_env(), build_policy_agent_graph())

    def run(self, mode: RunMode, trace_id: str | None = None) -> list[ProcessedCase]:
        sources = self.repository.fetch_source_handoffs(mode, trace_id)
        if not sources:
            target = trace_id or mode
            raise RuntimeError(f"No triage_agent -> policy_agent cloud handoff found for {target}.")

        processed: list[ProcessedCase] = []
        for source in sources:
            try:
                policy_input = exact_policy_input(source.payload())
                result = self.graph.invoke({"policy_input": policy_input})
                output = PolicyAgentOutput.model_validate(result["policy_output"])
                policy_result = PolicyReasoningResult.model_validate(result["policy_result"])
                precedent_context = PrecedentContext.model_validate(result["precedent_context"])
                assessment = GovernanceAssessment.model_validate(result["governance_assessment"])
                usage = TokenUsage.model_validate(result["usage"])
                policy_usage = TokenUsage.model_validate(result["policy_usage"])
                governance_usage = TokenUsage.model_validate(result["governance_usage"])
                handoff_id = self.repository.persist_result(
                    policy_input,
                    output,
                    policy_result,
                    precedent_context,
                    assessment.findings,
                    usage,
                )
                processed.append(ProcessedCase(handoff_id, output, usage, policy_usage, governance_usage))
            except Exception as error:
                try:
                    self.repository.record_failure(source.trace_id, error)
                except Exception:
                    pass
                raise RuntimeError(f"{source.trace_id}: Policy Agent processing failed: {error}") from error
        return processed
