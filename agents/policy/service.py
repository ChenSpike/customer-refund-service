from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from db.pipeline_store import PipelineStore

from .cloud_db import GCPRepository, RunMode
from .graph import build_policy_agent_graph
from .models import PolicyAgentOutput, TokenUsage
from .policy_node import (
    policy_output_from_state,
    policy_stage_usage_from_state,
    policy_usage_from_state,
)


@dataclass(frozen=True)
class ProcessedCase:
    handoff_id: str
    output: PolicyAgentOutput
    usage: TokenUsage
    policy_usage: TokenUsage
    governance_usage: TokenUsage


class PolicyAgentService:
    """Standalone GCP worker backed by the same state-driven Policy subgraph."""

    def __init__(self, repository: GCPRepository, graph: Any) -> None:
        self.repository = repository
        self.graph = graph
        self.pipeline_store = PipelineStore(repository)

    @classmethod
    def from_env(cls) -> "PolicyAgentService":
        repository = GCPRepository.from_env()
        return cls(repository, build_policy_agent_graph())

    def run(self, mode: RunMode, trace_id: str | None = None) -> list[ProcessedCase]:
        sources = self.repository.fetch_source_handoffs(mode, trace_id)
        if not sources:
            target = trace_id or mode
            raise RuntimeError(
                f"No triage_agent -> policy_agent cloud handoff found for {target}."
            )

        processed: list[ProcessedCase] = []
        for source in sources:
            try:
                result = self.graph.invoke(
                    {
                        "trace_id": source.trace_id,
                        "ticket_id": source.ticket_id,
                        "triage_output": source.payload(),
                        "current_stage": "triage_governance",
                        "workflow_status": "running",
                        "risk_flags": [],
                        "llm_usage_events": [],
                    }
                )
                output = policy_output_from_state(result)
                usage = policy_usage_from_state(result)
                policy_usage = policy_stage_usage_from_state(
                    result,
                    "policy_reasoning",
                )
                governance_usage = policy_stage_usage_from_state(
                    result,
                    "policy_governance",
                )
                artifacts = self.pipeline_store.persist_policy_state(result)
                processed.append(
                    ProcessedCase(
                        artifacts.handoff_id,
                        output,
                        usage,
                        policy_usage,
                        governance_usage,
                    )
                )
            except Exception as error:
                try:
                    self.repository.record_failure(source.trace_id, error)
                except Exception:
                    pass
                raise RuntimeError(
                    f"{source.trace_id}: Policy Agent processing failed: {error}"
                ) from error
        return processed
