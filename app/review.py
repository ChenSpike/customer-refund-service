"""Durable, stateless continuation for dashboard human-review decisions.

The HTTP layer should stay thin: validate its request body, call
``HumanApprovalService.resolve()``, and return ``ReviewOutcome.as_dict()``.
All routing and persistence rules live here or in ``GCPRepository`` so the
dashboard cannot partially update an approval with ad-hoc SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from agents.policy.azure import AzureJsonClient
from agents.refund.node import refund_node
from db.backend import DatabaseGovernanceEventRepository
from db.database import (
    DEFAULT_CONTINUATION_LEASE_SECONDS,
    GCPRepository,
    HumanApprovalConflictError,
    HumanApprovalNotFoundError,
    HumanApprovalResolution,
    HumanApprovalStateError,
)
from db.pipeline_store import PipelineStore


StateRunner = Callable[[dict[str, Any]], dict[str, Any]]


class ReviewContinuationError(RuntimeError):
    """A persisted review decision could not finish its downstream route."""


@dataclass(frozen=True)
class ReviewOutcome:
    approval_id: str
    trace_id: str
    decision: str
    status: str
    resolved_amount: float | None
    next_agent: str
    continuation_status: str
    workflow_status: str
    current_agent: str
    idempotent: bool
    new_approval_id: str | None
    refund_result: dict[str, Any]
    response_result: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "trace_id": self.trace_id,
            "decision": self.decision,
            "status": self.status,
            "resolved_amount": self.resolved_amount,
            "next_agent": self.next_agent,
            "continuation_status": self.continuation_status,
            "workflow_status": self.workflow_status,
            "current_agent": self.current_agent,
            "idempotent": self.idempotent,
            "new_approval_id": self.new_approval_id,
            "refund_result": self.refund_result,
            "response_result": self.response_result,
        }


class HumanApprovalService:
    """Resolve a persisted approval and run exactly one downstream continuation.

    ``response_runner`` is injectable so offline tests never contact Azure.  In
    production the default runner is the existing Response subgraph wired to
    the same repository as the approval transaction.
    """

    def __init__(
        self,
        repository: GCPRepository,
        *,
        refund_runner: StateRunner = refund_node,
        policy_runner: StateRunner | None = None,
        response_runner: StateRunner | None = None,
        client: AzureJsonClient | None = None,
        continuation_lease_seconds: int = DEFAULT_CONTINUATION_LEASE_SECONDS,
    ) -> None:
        self.repository = repository
        self.refund_runner = refund_runner
        self.store = PipelineStore(repository)
        self._client = client
        self._policy_graph: Any | None = None
        self._response_graph: Any | None = None
        self.policy_runner = policy_runner or self._run_default_policy
        self.response_runner = response_runner or self._run_default_response
        self.continuation_lease_seconds = continuation_lease_seconds

    def resolve(
        self,
        trace_id: str,
        *,
        decision: str,
        resolved_amount: float | Decimal | None,
        reviewer: str,
        notes: str,
        approval_id: str | None = None,
    ) -> ReviewOutcome:
        resolution = self.repository.resolve_human_approval(
            trace_id=trace_id,
            approval_id=approval_id,
            decision=decision,
            resolved_amount=resolved_amount,
            reviewer=reviewer,
            notes=notes,
            continuation_stale_after_seconds=self.continuation_lease_seconds,
        )
        if resolution.continuation_complete:
            return self._outcome(
                resolution,
                resolution.state,
                continuation_status="already_completed",
                new_approval_id=None,
            )
        persisted_status = str(resolution.state.get("workflow_status") or "")
        if resolution.idempotent and persisted_status in {
            "completed",
            "waiting_user",
            "pending_human",
            "waiting_human",
        }:
            # The worker may have died after the downstream stage committed but
            # before writing the terminal marker.  Trust the durable workflow
            # terminal state and repair only the marker; never repeat Refund.
            workflow_status, current_agent = _terminal_workflow_state(resolution.state)
            self.repository.mark_human_approval_continuation(
                trace_id=resolution.trace_id,
                approval_id=resolution.approval_id,
                workflow_status=workflow_status,
                current_agent=current_agent,
                summary={
                    "next_agent": resolution.next_agent,
                    "recovered_terminal_marker": True,
                    "final_outcome": resolution.state.get("final_outcome"),
                },
            )
            return self._outcome(
                resolution,
                resolution.state,
                continuation_status="recovered",
                new_approval_id=None,
            )
        # A matching concurrent request sees the already-resolved row while the
        # first worker owns the downstream continuation.  It must not issue a
        # second refund or generate a second response.  A recorded failed state is
        # intentionally retryable with the identical request.
        if (
            resolution.idempotent
            and not resolution.continuation_resumable
            and resolution.next_agent != "end"
            and resolution.state.get("workflow_status") != "failed"
        ):
            return self._outcome(
                resolution,
                resolution.state,
                continuation_status="in_progress",
                new_approval_id=None,
            )

        state = dict(resolution.state)
        new_approval_id: str | None = None
        try:
            next_agent = resolution.next_agent
            if next_agent == "policy_agent":
                state.update(self.policy_runner(state))
                # The resolved Triage review authorized Policy to run; it is
                # not the Policy business decision.  Clear that old review so
                # Response reflects the freshly persisted Policy outcome.
                state["human_review"] = {}
                state["human_review_required"] = False
                next_agent = str(
                    (state.get("policy_persistence_result") or {}).get("next_agent") or ""
                )
                if next_agent not in {"refund_agent", "response_agent", "human_approval"}:
                    raise ReviewContinuationError(
                        "Policy continuation did not persist a supported next_agent"
                    )
                if next_agent == "human_approval":
                    # Policy persistence already wrote the new pending row. The
                    # idempotent ensure call returns that row and never recurses.
                    policy_review_reason = str(
                        (state.get("policy_decision") or {}).get("reason")
                        or state.get("review_trigger_reason")
                        or "policy_human_review"
                    )
                    new_approval_id = self.repository.ensure_human_approval(
                        trace_id=resolution.trace_id,
                        reason=policy_review_reason,
                        stage="policy",
                        policy_decision=state.get("policy_decision") or {},
                    )
                    state.update(
                        {
                            "human_review_required": True,
                            "workflow_status": "waiting_human",
                            "current_stage": "human_approval",
                            "review_trigger_stage": "policy",
                            "review_trigger_reason": policy_review_reason,
                            "human_review": {
                                "approval_id": new_approval_id,
                                "status": "pending",
                                "reason": policy_review_reason,
                                "stage": "policy",
                            },
                        }
                    )

            if next_agent == "refund_agent":
                state.update(self.refund_runner(state))
                state.update(self.store.persist_refund_state(state).state_patch())
                # The review has been consumed by Refund. Response should use
                # the actual persisted refund result (including partials and
                # failures), not the earlier pending-review template.
                state["human_review"] = {}
                state.update(self.response_runner(state))
            elif next_agent == "response_agent":
                if resolution.status == "approved":
                    # This approves continuation/release, not a new refund. The
                    # reconstructed Policy decision remains authoritative.
                    state["human_review"] = {}
                state.update(self.response_runner(state))
            elif next_agent == "human_approval":
                pass
            elif next_agent == "end":
                state.update(
                    {
                        "current_stage": "completed",
                        "workflow_status": "completed",
                        "final_outcome": state.get("final_outcome") or "approved",
                    }
                )
            else:  # Repository validation should make this unreachable.
                raise ReviewContinuationError(
                    f"Unsupported review continuation route: {next_agent}"
                )

            if state.get("response_handoff") == "human_review":
                # Run Response only once.  A governance block gets a fresh,
                # persisted approval and returns; it never re-enters Response.
                new_approval_id = self.repository.ensure_human_approval(
                    trace_id=resolution.trace_id,
                    reason=str(state.get("review_trigger_reason") or "response_governance_block"),
                    stage="response",
                    policy_decision=state.get("policy_decision") or {},
                )
                state.update(
                    {
                        "human_review_required": True,
                        "workflow_status": "waiting_human",
                        "current_stage": "human_approval",
                        "human_review": {
                            "approval_id": new_approval_id,
                            "status": "pending",
                            "reason": str(
                                state.get("review_trigger_reason")
                                or "response_governance_block"
                            ),
                            "stage": "response",
                        },
                    }
                )

            workflow_status, current_agent = _terminal_workflow_state(state)
            self.repository.mark_human_approval_continuation(
                trace_id=resolution.trace_id,
                approval_id=resolution.approval_id,
                workflow_status=workflow_status,
                current_agent=current_agent,
                summary={
                    "next_agent": resolution.next_agent,
                    "final_outcome": state.get("final_outcome"),
                    "refund_status": (state.get("refund_result") or {}).get("status"),
                    "response_handoff": state.get("response_handoff"),
                    "new_approval_id": new_approval_id,
                },
            )
            state["workflow_status"] = (
                "waiting_human" if workflow_status == "pending_human" else workflow_status
            )
            continuation_status = (
                "pending_human" if workflow_status == "pending_human" else "completed"
            )
            return self._outcome(
                resolution,
                state,
                continuation_status=continuation_status,
                new_approval_id=new_approval_id,
            )
        except Exception as error:
            self.repository.record_human_approval_continuation_failure(
                trace_id=resolution.trace_id,
                approval_id=resolution.approval_id,
                error=error,
            )
            if isinstance(error, ReviewContinuationError):
                raise
            raise ReviewContinuationError(
                f"{resolution.trace_id}: human-approval continuation failed"
            ) from error

    def _azure(self) -> AzureJsonClient:
        if self._client is None:
            self._client = AzureJsonClient.from_env()
        return self._client

    def _run_default_policy(self, state: dict[str, Any]) -> dict[str, Any]:
        if self._policy_graph is None:
            from agents.policy import build_policy_agent_graph

            self._policy_graph = build_policy_agent_graph(
                self._azure(),
                store=PipelineStore(self.repository),
            )
        return self._policy_graph.invoke(state)

    def _run_default_response(self, state: dict[str, Any]) -> dict[str, Any]:
        # Lazy graph construction keeps read-only dashboard startup offline-safe.
        # When both stages are needed they share one AzureJsonClient instance.
        if self._response_graph is None:
            from agents.response import build_response_agent_graph

            self._response_graph = build_response_agent_graph(
                client=self._azure(),
                store=PipelineStore(self.repository),
                event_writer=DatabaseGovernanceEventRepository(self.repository),
            )
        return self._response_graph.invoke(state)

    @staticmethod
    def _outcome(
        resolution: HumanApprovalResolution,
        state: dict[str, Any],
        *,
        continuation_status: str,
        new_approval_id: str | None,
    ) -> ReviewOutcome:
        workflow_status, current_agent = _terminal_workflow_state(state)
        return ReviewOutcome(
            approval_id=resolution.approval_id,
            trace_id=resolution.trace_id,
            decision=resolution.decision,
            status=resolution.status,
            resolved_amount=resolution.resolved_amount,
            next_agent=resolution.next_agent,
            continuation_status=continuation_status,
            workflow_status=workflow_status,
            current_agent=current_agent,
            idempotent=resolution.idempotent,
            new_approval_id=new_approval_id,
            refund_result=dict(state.get("refund_result") or {}),
            response_result=dict(state.get("response_result") or {}),
        )


def _terminal_workflow_state(state: dict[str, Any]) -> tuple[str, str]:
    runtime_status = str(state.get("workflow_status") or "completed")
    if runtime_status in {"waiting_human", "pending_human"}:
        return "pending_human", "human_approval"
    if runtime_status == "waiting_user":
        return "waiting_user", "triage_agent"
    if runtime_status == "failed":
        return "failed", "human_approval"
    if runtime_status == "running":
        return "running", str(state.get("current_stage") or "human_approval")
    return "completed", "completed"


__all__ = [
    "HumanApprovalService",
    "HumanApprovalConflictError",
    "HumanApprovalNotFoundError",
    "HumanApprovalStateError",
    "ReviewContinuationError",
    "ReviewOutcome",
]
