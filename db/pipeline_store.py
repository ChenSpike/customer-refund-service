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


@dataclass(frozen=True)
class TriagePersistenceArtifacts:
    handoff_id: str
    trace_id: str
    next_agent: str

    def state_patch(self) -> dict[str, Any]:
        return {
            "current_stage": "triage_persistence",
            "triage_persistence_result": {
                "handoff_id": self.handoff_id,
                "trace_id": self.trace_id,
                "next_agent": self.next_agent,
            },
        }


@dataclass(frozen=True)
class ResponsePersistenceArtifacts:
    handoff_id: str
    trace_id: str
    next_agent: str

    def state_patch(self) -> dict[str, Any]:
        return {
            "current_stage": "response_persistence",
            "response_persistence_result": {
                "handoff_id": self.handoff_id,
                "trace_id": self.trace_id,
                "next_agent": self.next_agent,
            },
        }


@dataclass(frozen=True)
class RefundPersistenceArtifacts:
    transaction_id: str
    handoff_id: str
    trace_id: str

    def state_patch(self) -> dict[str, Any]:
        return {
            "current_stage": "refund_persistence",
            "refund_persistence_result": {
                "transaction_id": self.transaction_id,
                "handoff_id": self.handoff_id,
                "trace_id": self.trace_id,
                "next_agent": "response_agent",
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
        continuation_kwargs = _continuation_token_kwargs(state)
        handoff_id = self.persist_policy_artifacts(
            policy_input=reconstructed.policy_input,
            policy_output=policy_output,
            policy_result=reconstructed.policy_result,
            precedent_context=reconstructed.precedent_context,
            governance_assessment=governance,
            usage=usage,
            **continuation_kwargs,
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

    def persist_triage_state(self, state: dict[str, Any]) -> TriagePersistenceArtifacts:
        handoff = state.get("triage_handoff")
        if handoff not in {"policy", "response", "human_review"}:
            raise ValueError("triage_handoff must be present before Triage persistence")
        next_agent = {
            "policy": "policy_agent",
            "response": "response_agent",
            "human_review": "human_approval",
        }[handoff]
        trace_id = str(state.get("trace_id") or "")
        if not trace_id:
            raise ValueError("trace_id must be present before Triage persistence")
        ticket_id = str(state.get("ticket_id") or "")
        if not ticket_id:
            raise ValueError("ticket_id must be present before Triage persistence")
        handoff_id = self.repository.persist_agent_handoff(
            trace_id=trace_id,
            ticket_id=ticket_id,
            from_agent="triage_agent",
            to_agent=next_agent,
            input_payload={
                "message": state.get("message"),
                "user_id": state.get("user_id"),
                "requested_order_id": state.get("requested_order_id"),
            },
            output_payload={
                "triage_output": state.get("triage_output"),
                **(
                    {"order_resolution_source": state["order_resolution_source"]}
                    if state.get("order_resolution_source")
                    else {}
                ),
                "triage_governance_result": state.get("triage_governance_result"),
                "triage_handoff": handoff,
            },
            input_tokens=int(state.get("llm_input_tokens") or 0),
            output_tokens=int(state.get("llm_output_tokens") or 0),
            audit_event_type="triage_agent_evaluated",
            workflow_status="waiting_human" if next_agent == "human_approval" else "running",
            current_agent=next_agent,
            **_continuation_token_kwargs(state),
        )
        return TriagePersistenceArtifacts(handoff_id=handoff_id, trace_id=trace_id, next_agent=next_agent)

    def persist_response_state(self, state: dict[str, Any]) -> ResponsePersistenceArtifacts:
        handoff = state.get("response_handoff")
        if handoff not in {"end", "human_review"}:
            raise ValueError("response_handoff must be present before Response persistence")
        next_agent = {
            "end": "end",
            "human_review": "human_approval",
        }[handoff]
        trace_id = str(state.get("trace_id") or "")
        if not trace_id:
            raise ValueError("trace_id must be present before Response persistence")
        ticket_id = str(state.get("ticket_id") or "")
        if not ticket_id:
            raise ValueError("ticket_id must be present before Response persistence")
        response_result = state.get("response_result") or {}
        workflow_status = str(response_result.get("workflow_status") or state.get("workflow_status") or "completed")
        current_agent = next_agent
        if next_agent == "end":
            current_agent = {
                "waiting_human": "human_approval",
                "pending_human": "human_approval",
                "waiting_user": "triage_agent",
            }.get(workflow_status, "completed")
        handoff_id = self.repository.persist_agent_handoff(
            trace_id=trace_id,
            ticket_id=ticket_id,
            from_agent="response_agent",
            to_agent=next_agent,
            input_payload={
                "message": state.get("message"),
                "user_id": state.get("user_id"),
                "human_review": state.get("human_review"),
            },
            output_payload={
                "response_result": response_result,
                "response_governance_result": state.get("response_governance_result"),
                "response_handoff": handoff,
            },
            input_tokens=int(state.get("llm_input_tokens") or 0),
            output_tokens=int(state.get("llm_output_tokens") or 0),
            audit_event_type="response_agent_evaluated",
            workflow_status=workflow_status,
            current_agent=current_agent,
            **_continuation_token_kwargs(state),
        )
        return ResponsePersistenceArtifacts(handoff_id=handoff_id, trace_id=trace_id, next_agent=next_agent)

    def persist_refund_state(self, state: dict[str, Any]) -> RefundPersistenceArtifacts:
        trace_id = str(state.get("trace_id") or "")
        ticket_id = str(state.get("ticket_id") or "")
        refund_result = state.get("refund_result") or {}
        transaction_id, handoff_id = self.repository.persist_refund_result(
            trace_id=trace_id,
            ticket_id=ticket_id,
            policy_decision=state.get("policy_decision") or {},
            order_lookup_result=state.get("order_lookup_result") or {},
            refund_result=refund_result,
            **_continuation_token_kwargs(state),
        )
        return RefundPersistenceArtifacts(
            transaction_id=transaction_id,
            handoff_id=handoff_id,
            trace_id=trace_id,
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
        followup_claim_token: str | None = None,
        approval_claim_token: str | None = None,
    ) -> str:
        if followup_claim_token is not None and approval_claim_token is not None:
            raise ValueError("Policy persistence cannot use overlapping continuation claims")
        kwargs: dict[str, str] = {}
        if followup_claim_token is not None:
            kwargs["followup_claim_token"] = followup_claim_token
        if approval_claim_token is not None:
            kwargs["approval_claim_token"] = approval_claim_token
        return self.repository.persist_result(
            policy_input,
            policy_output,
            policy_result,
            precedent_context,
            governance_assessment.findings,
            usage,
            **kwargs,
        )


class PolicyPersistenceNode:
    def __init__(self, store: PipelineStore) -> None:
        self.store = store

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.store.persist_policy_state(state).state_patch()


class TriagePersistenceNode:
    def __init__(self, store: PipelineStore) -> None:
        self.store = store

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.store.persist_triage_state(state).state_patch()


class ResponsePersistenceNode:
    def __init__(self, store: PipelineStore) -> None:
        self.store = store

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.store.persist_response_state(state).state_patch()


class RefundPersistenceNode:
    def __init__(self, store: PipelineStore) -> None:
        self.store = store

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.store.persist_refund_state(state).state_patch()


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


def _followup_claim_token(state: dict[str, Any]) -> str | None:
    context = state.get("request_context") or {}
    if context.get("continuation_type") != "customer_followup":
        return None
    token = str(context.get("followup_claim_token") or "").strip()
    if not token:
        raise ValueError("customer follow-up persistence requires a claim token")
    return token


def _followup_token_kwargs(state: dict[str, Any]) -> dict[str, str]:
    token = _followup_claim_token(state)
    return {"followup_claim_token": token} if token is not None else {}


def _approval_claim_token(state: dict[str, Any]) -> str | None:
    context = state.get("request_context") or {}
    if context.get("continuation_type") != "human_approval":
        return None
    token = str(context.get("approval_claim_token") or "").strip()
    approval_id = str(context.get("approval_id") or "").strip()
    attempt = context.get("approval_attempt")
    sequence = context.get("approval_sequence")
    if not token or not approval_id:
        raise ValueError("human-approval persistence requires its approval and claim token")
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
    ):
        raise ValueError("human-approval persistence requires attempt and sequence")
    return token


def _continuation_token_kwargs(state: dict[str, Any]) -> dict[str, str]:
    context = state.get("request_context") or {}
    continuation_type = context.get("continuation_type")
    if continuation_type in {None, ""}:
        return {}
    if continuation_type == "customer_followup":
        return _followup_token_kwargs(state)
    if continuation_type == "human_approval":
        token = _approval_claim_token(state)
        return {"approval_claim_token": token} if token is not None else {}
    raise ValueError(f"Unsupported persistence continuation type: {continuation_type!r}")
