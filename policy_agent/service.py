from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .azure_agent import AzurePolicyAgents
from .cloud_db import GCPRepository, RunMode
from .models import PolicyAgentOutput, TokenUsage, exact_policy_input


POLICY_AGENT_DIR = Path(__file__).resolve().parent
POLICY_CONTEXTS = {"v1.0": POLICY_AGENT_DIR / "data" / "policy_context_v1.md"}


@dataclass(frozen=True)
class ProcessedCase:
    handoff_id: str
    output: PolicyAgentOutput
    usage: TokenUsage


class PolicyAgentService:
    def __init__(self, repository: GCPRepository, agents: AzurePolicyAgents) -> None:
        self.repository = repository
        self.agents = agents

    @classmethod
    def from_env(cls) -> "PolicyAgentService":
        return cls(GCPRepository.from_env(), AzurePolicyAgents.from_env())

    def run(self, mode: RunMode, trace_id: str | None = None) -> list[ProcessedCase]:
        sources = self.repository.fetch_source_handoffs(mode, trace_id)
        if not sources:
            target = trace_id or mode
            raise RuntimeError(f"No triage_agent -> policy_agent cloud handoff found for {target}.")

        processed: list[ProcessedCase] = []
        for source in sources:
            try:
                policy_input = exact_policy_input(source.payload())
                policy_context = load_policy_context(policy_input.case.policy_version)
                result = self.agents.evaluate(policy_input, policy_context)
                handoff_id = self.repository.persist_result(policy_input, result.output, result.usage)
                processed.append(ProcessedCase(handoff_id, result.output, result.usage))
            except Exception as error:
                try:
                    self.repository.record_failure(source.trace_id, error)
                except Exception:
                    pass
                raise RuntimeError(f"{source.trace_id}: Policy Agent processing failed: {error}") from error
        return processed


def load_policy_context(policy_version: str) -> str:
    path = POLICY_CONTEXTS.get(policy_version)
    if path is None:
        raise ValueError(f"Unsupported policy knowledge-base version: {policy_version}")
    return path.read_text(encoding="utf-8")
