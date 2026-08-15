"""Durable, stateless continuation for dashboard human-review decisions.

The HTTP layer should stay thin: validate its request body, call
``HumanApprovalService.resolve()``, and return ``ReviewOutcome.as_dict()``.
All routing and persistence rules live here or in ``GCPRepository`` so the
dashboard cannot partially update an approval with ad-hoc SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from threading import Event, Thread
from typing import Any, Callable

from agents.policy.azure import AzureJsonClient
from agents.refund.node import refund_node
from db.approval_context import approval_continuation_fence
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
            completion_summary = resolution.state.get(
                "human_approval_continuation_summary"
            ) or {}
            return self._outcome(
                resolution,
                resolution.state,
                continuation_status="already_completed",
                new_approval_id=(
                    str(completion_summary.get("new_approval_id"))
                    if completion_summary.get("new_approval_id")
                    else None
                ),
            )
        persisted_status = str(resolution.state.get("workflow_status") or "")
        if (
            resolution.idempotent
            and persisted_status
            in {
                "completed",
                "waiting_user",
                "pending_human",
                "waiting_human",
            }
            and not resolution.continuation_resumable
        ):
            # A stage may have committed its terminal route while the owner is
            # about to ensure a pending review or write the terminal marker. A
            # fresh duplicate must not race that owner using the same claim.
            # Stale recovery gets a new resumable claim after the lease expires.
            return self._outcome(
                resolution,
                resolution.state,
                continuation_status="in_progress",
                new_approval_id=None,
            )
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
            claim_token, _attempt, _sequence = _approval_claim_identity(resolution)
            recovered_state = dict(resolution.state)
            new_approval_id: str | None = None
            if workflow_status == "pending_human":
                recovered_state.update(
                    self._run_claimed_step(
                        resolution,
                        lambda: self._ensure_pending_review_patch(
                            resolution,
                            recovered_state,
                            claim_token,
                            recover_from_durable_checkpoint=True,
                        ),
                    )
                )
                new_approval_id = str(
                    (recovered_state.get("human_review") or {}).get("approval_id")
                    or ""
                ) or None
            self.repository.mark_human_approval_continuation(
                trace_id=resolution.trace_id,
                approval_id=resolution.approval_id,
                workflow_status=workflow_status,
                current_agent=current_agent,
                summary={
                    "next_agent": resolution.next_agent,
                    "recovered_terminal_marker": True,
                    "final_outcome": recovered_state.get("final_outcome"),
                    "new_approval_id": new_approval_id,
                },
                approval_claim_token=claim_token,
            )
            return self._outcome(
                resolution,
                recovered_state,
                continuation_status="recovered",
                new_approval_id=new_approval_id,
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
        claim_token, claim_attempt, claim_sequence = _approval_claim_identity(resolution)
        state["request_context"] = {
            **dict(state.get("request_context") or {}),
            "continuation_type": "human_approval",
            "approval_id": resolution.approval_id,
            "approval_claim_token": claim_token,
            "approval_attempt": claim_attempt,
            "approval_sequence": claim_sequence,
        }
        new_approval_id: str | None = None
        try:
            next_agent = resolution.next_agent
            if next_agent == "policy_agent":
                persisted_policy_route = str(
                    (state.get("policy_persistence_result") or {}).get("next_agent")
                    or ""
                )
                if persisted_policy_route:
                    next_agent = persisted_policy_route
                else:
                    state.update(
                        self._run_claimed_step(
                            resolution,
                            lambda: self.policy_runner(state),
                        )
                    )
                # The resolved Triage review authorized Policy to run; it is
                # not the Policy business decision.  Clear that old review so
                # Response reflects the freshly persisted Policy outcome.
                state["human_review"] = {}
                state["human_review_required"] = False
                if not persisted_policy_route:
                    next_agent = str(
                        (state.get("policy_persistence_result") or {}).get("next_agent")
                        or ""
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
                    state.update(
                        self._run_claimed_step(
                            resolution,
                            lambda: self._ensure_pending_review_patch(
                                resolution,
                                state,
                                claim_token,
                                stage="policy",
                                reason=policy_review_reason,
                            ),
                        )
                    )
                    new_approval_id = str(
                        (state.get("human_review") or {}).get("approval_id") or ""
                    ) or None

            if next_agent == "refund_agent":
                persisted_refund = state.get("refund_persistence_result") or {}
                if not persisted_refund.get("transaction_id"):
                    def run_refund() -> dict[str, Any]:
                        state.update(self.refund_runner(state))
                        return self.store.persist_refund_state(state).state_patch()

                    state.update(self._run_claimed_step(resolution, run_refund))
                # The review has been consumed by Refund. Response should use
                # the actual persisted refund result (including partials and
                # failures), not the earlier pending-review template.
                state["human_review"] = {}
                state.update(self._run_response_step(resolution, state, claim_token))
            elif next_agent == "response_agent":
                if resolution.status == "approved":
                    # This approves continuation/release, not a new refund. The
                    # reconstructed Policy decision remains authoritative.
                    state["human_review"] = {}
                state.update(self._run_response_step(resolution, state, claim_token))
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
                # Response and the fenced ensure call ran in one claimed step.
                # If a process exits between their database commits, an
                # identical request repairs the missing pending row above.
                new_approval_id = str(
                    (state.get("human_review") or {}).get("approval_id") or ""
                ) or None

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
                approval_claim_token=claim_token,
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
            try:
                self.repository.record_human_approval_continuation_failure(
                    trace_id=resolution.trace_id,
                    approval_id=resolution.approval_id,
                    error=error,
                    approval_claim_token=claim_token,
                )
            except Exception as persistence_error:
                # A superseded token is expected to be rejected here too. Keep
                # the original continuation failure as the public cause while
                # retaining the audit-rejection detail for diagnostics.
                if hasattr(error, "add_note"):
                    error.add_note(
                        "Continuation failure persistence was rejected: "
                        f"{persistence_error}"
                    )
            if isinstance(error, ReviewContinuationError):
                raise
            raise ReviewContinuationError(
                f"{resolution.trace_id}: human-approval continuation failed"
            ) from error

    def _run_response_step(
        self,
        resolution: HumanApprovalResolution,
        state: dict[str, Any],
        claim_token: str,
    ) -> dict[str, Any]:
        def run_response_and_ensure_review() -> dict[str, Any]:
            patch = dict(self.response_runner(state))
            state.update(patch)
            if state.get("response_handoff") == "human_review":
                patch.update(
                    self._ensure_pending_review_patch(
                        resolution,
                        state,
                        claim_token,
                        stage="response",
                    )
                )
            return patch

        return self._run_claimed_step(resolution, run_response_and_ensure_review)

    def _ensure_pending_review_patch(
        self,
        resolution: HumanApprovalResolution,
        state: dict[str, Any],
        claim_token: str,
        *,
        stage: str | None = None,
        reason: str | None = None,
        recover_from_durable_checkpoint: bool = False,
    ) -> dict[str, Any]:
        review_stage = stage or _pending_review_stage(resolution, state)
        review_reason = str(
            reason
            or state.get("review_trigger_reason")
            or (state.get("policy_decision") or {}).get("reason")
            or f"{review_stage}_human_review"
        )
        trigger_marker = (
            state.get("response_persistence_marker")
            if recover_from_durable_checkpoint and review_stage == "response"
            else None
        )
        if trigger_marker is not None and not isinstance(trigger_marker, dict):
            raise ReviewContinuationError(
                f"{resolution.trace_id}: recovered Response marker is invalid"
            )
        new_approval_id = self.repository.ensure_human_approval(
            trace_id=resolution.trace_id,
            reason=review_reason,
            stage=review_stage,
            policy_decision=state.get("policy_decision") or {},
            approval_claim_token=claim_token,
            **(
                {"trigger_continuation_marker": trigger_marker}
                if trigger_marker
                else {}
            ),
        )
        return {
            "human_review_required": True,
            "workflow_status": "waiting_human",
            "current_stage": "human_approval",
            "review_trigger_stage": review_stage,
            "review_trigger_reason": review_reason,
            "human_review": {
                "approval_id": new_approval_id,
                "status": "pending",
                "reason": review_reason,
                "stage": review_stage,
            },
        }

    def _run_claimed_step(
        self,
        resolution: HumanApprovalResolution,
        runner: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        claim_token, attempt, sequence = _approval_claim_identity(resolution)
        with approval_continuation_fence(
            trace_id=resolution.trace_id,
            approval_id=resolution.approval_id,
            claim_token=claim_token,
            attempt=attempt,
            sequence=sequence,
        ), _ApprovalClaimLeaseHeartbeat(
            self.repository,
            resolution,
            claim_token,
            lease_seconds=self.continuation_lease_seconds,
        ):
            return runner()

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


def _pending_review_stage(
    resolution: HumanApprovalResolution,
    state: dict[str, Any],
) -> str:
    if (
        state.get("response_handoff") == "human_review"
        or (state.get("response_persistence_result") or {}).get("next_agent")
        == "human_approval"
    ):
        return "response"
    if (
        state.get("policy_handoff") in {"human_review", "human_approval"}
        or (state.get("policy_persistence_result") or {}).get("next_agent")
        == "human_approval"
    ):
        return "policy"
    stage = str(state.get("review_trigger_stage") or resolution.review_trigger_stage)
    if stage not in {"triage", "policy", "response"}:
        raise ReviewContinuationError(
            f"{resolution.trace_id}: pending review stage is not recoverable"
        )
    return stage


def _approval_claim_identity(
    resolution: HumanApprovalResolution,
) -> tuple[str, int, int]:
    token = str(resolution.continuation_claim_token or "").strip()
    attempt = resolution.continuation_attempt
    sequence = resolution.continuation_sequence
    if (
        not token
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
    ):
        raise ReviewContinuationError(
            f"{resolution.trace_id}: human-approval continuation has no active claim"
        )
    return token, attempt, sequence


class _ApprovalClaimLeaseHeartbeat:
    """Keep an approval claim active while an agent waits on Azure."""

    def __init__(
        self,
        repository: Any,
        resolution: HumanApprovalResolution,
        claim_token: str,
        *,
        lease_seconds: int,
    ) -> None:
        self.repository = repository
        self.resolution = resolution
        self.claim_token = claim_token
        self.heartbeat = getattr(
            repository,
            "heartbeat_human_approval_continuation",
            None,
        )
        self.interval = max(0.01, min(10.0, float(lease_seconds) / 3))
        self.stop_event = Event()
        self.error: Exception | None = None
        self.thread: Thread | None = None

    def __enter__(self) -> "_ApprovalClaimLeaseHeartbeat":
        if not callable(self.heartbeat):
            raise ReviewContinuationError(
                f"{self.resolution.trace_id}: approval claim validator is unavailable"
            )
        try:
            active = self.heartbeat(
                trace_id=self.resolution.trace_id,
                approval_id=self.resolution.approval_id,
                approval_claim_token=self.claim_token,
            )
        except Exception as error:
            raise ReviewContinuationError(
                f"{self.resolution.trace_id}: approval claim validation failed"
            ) from error
        if active is not True:
            raise ReviewContinuationError(
                f"{self.resolution.trace_id}: approval claim is not active"
            )
        self.thread = Thread(
            target=self._run,
            name=f"approval-heartbeat-{self.resolution.trace_id}",
            daemon=True,
        )
        self.thread.start()
        return self

    def __exit__(self, error_type: Any, _error: Any, _traceback: Any) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        if error_type is None and self.error is not None:
            raise ReviewContinuationError(
                f"{self.resolution.trace_id}: human-approval lease heartbeat failed"
            ) from self.error

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                if not self.heartbeat(
                    trace_id=self.resolution.trace_id,
                    approval_id=self.resolution.approval_id,
                    approval_claim_token=self.claim_token,
                ):
                    self.error = ReviewContinuationError(
                        f"{self.resolution.trace_id}: approval claim became inactive"
                    )
                    return
            except Exception as error:  # pragma: no cover - surfaced at context exit
                self.error = error
                return


__all__ = [
    "HumanApprovalService",
    "HumanApprovalConflictError",
    "HumanApprovalNotFoundError",
    "HumanApprovalStateError",
    "ReviewContinuationError",
    "ReviewOutcome",
]
