from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from threading import Event, Lock, Thread
from types import SimpleNamespace
from typing import Any

import pytest

import agents.policy as policy_package
import agents.response as response_package
import app.review as review_module
from app.review import HumanApprovalService, ReviewContinuationError
from db.approval_context import approval_continuation_fence
from db.backend import DatabaseGovernanceEventRepository
from db.database import (
    CloudDatabaseError,
    GCPRepository,
    HumanApprovalConflictError,
    HumanApprovalResolution,
    HumanApprovalStateError,
    _approval_continuation_claim_token,
    _select_approval_governance_trigger,
)
from governance import GovernanceFinding, GovernanceStatement


def _resolution(
    *,
    decision: str = "approve",
    next_agent: str = "refund_agent",
    continuation_complete: bool = False,
) -> HumanApprovalResolution:
    amount = 79.99 if next_agent == "refund_agent" else None
    status = "rejected" if decision == "deny" else "approved"
    return HumanApprovalResolution(
        approval_id="approval-demo01",
        trace_id="demo01",
        ticket_id="ticket-demo01",
        status=status,
        decision=decision,
        resolved_amount=amount,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
        next_agent=next_agent,
        review_trigger_stage="policy",
        state={
            "trace_id": "demo01",
            "ticket_id": "ticket-demo01",
            "user_id": "customer-demo01",
            "message": "Keyboard arrived cracked.",
            "requested_order_id": "order-demo01",
            "order_lookup_result": {
                "order_id": "order-demo01",
                "amount_paid": 79.99,
                "prior_refund_total": 0,
                "currency": "USD",
            },
            "policy_decision": {
                "decision": decision,
                "refund_amount": amount or 0,
                "reason": "Human decision",
            },
            "human_review": {
                "approval_id": "approval-demo01",
                "status": status,
                "approved_next_agent": next_agent if status == "approved" else None,
                "rejected_next_agent": next_agent if status == "rejected" else None,
            },
            "workflow_status": "completed" if continuation_complete else "running",
        },
        idempotent=continuation_complete,
        continuation_complete=continuation_complete,
        continuation_resumable=not continuation_complete,
        continuation_claim_token="approval-claim-demo01",
        continuation_attempt=1,
        continuation_sequence=1,
    )


class ServiceRepository:
    def __init__(self, resolution: HumanApprovalResolution) -> None:
        self.resolution = resolution
        self.refunds: list[dict[str, Any]] = []
        self.marks: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []

    def resolve_human_approval(self, **_kwargs: Any) -> HumanApprovalResolution:
        return self.resolution

    def persist_refund_result(self, **kwargs: Any) -> tuple[str, str]:
        self.refunds.append(kwargs)
        return "refund-demo01", "handoff-refund-demo01"

    def mark_human_approval_continuation(self, **kwargs: Any) -> bool:
        self.marks.append(kwargs)
        return True

    def record_human_approval_continuation_failure(self, **kwargs: Any) -> None:
        self.failures.append(kwargs)

    def heartbeat_human_approval_continuation(self, **_kwargs: Any) -> bool:
        return True

    def ensure_human_approval(self, **kwargs: Any) -> str:
        self.approvals.append(kwargs)
        return "approval-demo01-response-2"


def _successful_refund(_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "refund_result": {
            "status": "success",
            "refund_id": "RF-ticket-demo01",
            "amount": 79.99,
            "currency": "USD",
            "order_id": "order-demo01",
        },
        "workflow_status": "running",
    }


def _completed_response(_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "response_result": {
            "final_outcome": "approved",
            "workflow_status": "completed",
            "response": {"body": "Your refund was issued."},
        },
        "response_handoff": "end",
        "final_outcome": "approved",
        "workflow_status": "completed",
    }


def test_approved_review_runs_refund_persistence_then_response() -> None:
    repository = ServiceRepository(_resolution())
    calls: list[str] = []

    def refund_runner(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("refund")
        return _successful_refund(state)

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        assert state["refund_persistence_result"]["transaction_id"] == "refund-demo01"
        calls.append("response")
        return _completed_response(state)

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=refund_runner,
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )

    assert calls == ["refund", "response"]
    assert len(repository.refunds) == 1
    assert repository.refunds[0]["policy_decision"]["refund_amount"] == 79.99
    assert outcome.continuation_status == "completed"
    assert outcome.workflow_status == "completed"
    assert repository.marks[0]["current_agent"] == "completed"
    assert repository.failures == []


def test_denied_review_routes_to_response_without_refund() -> None:
    resolution = _resolution(decision="deny", next_agent="response_agent")
    repository = ServiceRepository(resolution)
    seen: list[dict[str, Any]] = []

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        seen.append(state)
        assert state["human_review"]["status"] == "rejected"
        return {
            **_completed_response(state),
            "final_outcome": "denied",
        }

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=lambda _state: pytest.fail("refund must not run"),
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="deny",
        resolved_amount=None,
        reviewer="reviewer@example.com",
        notes="The evidence does not support a refund.",
    )

    assert len(seen) == 1
    assert repository.refunds == []
    assert outcome.status == "rejected"
    assert outcome.workflow_status == "completed"


def test_approved_response_release_preserves_underlying_policy_decision() -> None:
    resolution = _resolution(next_agent="response_agent")
    resolution.state["policy_decision"] = {
        "decision": "deny",
        "refund_amount": 0,
        "reason": "The item is outside policy.",
    }
    repository = ServiceRepository(resolution)

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        assert state["human_review"] == {}
        assert state["policy_decision"]["decision"] == "deny"
        return {
            **_completed_response(state),
            "final_outcome": "denied",
        }

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=None,
        reviewer="reviewer@example.com",
        notes="The response may be released after review.",
    )

    assert outcome.workflow_status == "completed"
    assert repository.refunds == []


def test_triage_approval_continues_policy_then_refund_and_response() -> None:
    repository = ServiceRepository(_resolution(next_agent="policy_agent"))
    calls: list[str] = []

    def policy_runner(_state: dict[str, Any]) -> dict[str, Any]:
        calls.append("policy")
        return {
            "policy_decision": {
                "decision": "approve",
                "refund_amount": 79.99,
                "reason": "Damaged item is eligible.",
            },
            "policy_persistence_result": {"next_agent": "refund_agent"},
            "workflow_status": "running",
        }

    def refund_runner(state: dict[str, Any]) -> dict[str, Any]:
        assert state["policy_decision"]["decision"] == "approve"
        calls.append("refund")
        return _successful_refund(state)

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        assert state["refund_persistence_result"]["transaction_id"] == "refund-demo01"
        calls.append("response")
        return _completed_response(state)

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        policy_runner=policy_runner,
        refund_runner=refund_runner,
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=None,
        reviewer="security-reviewer@example.com",
        notes="The triage governance concern was reviewed and cleared.",
    )

    assert calls == ["policy", "refund", "response"]
    assert outcome.workflow_status == "completed"
    assert len(repository.refunds) == 1


def test_triage_approval_continues_policy_then_response_only() -> None:
    repository = ServiceRepository(_resolution(next_agent="policy_agent"))
    calls: list[str] = []

    def policy_runner(_state: dict[str, Any]) -> dict[str, Any]:
        calls.append("policy")
        return {
            "policy_decision": {
                "decision": "deny",
                "refund_amount": 0,
                "reason": "Policy does not allow this refund.",
            },
            "policy_persistence_result": {"next_agent": "response_agent"},
            "workflow_status": "running",
        }

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        assert state["policy_decision"]["decision"] == "deny"
        assert state["human_review"] == {}
        calls.append("response")
        return {
            **_completed_response(state),
            "final_outcome": "denied",
        }

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        policy_runner=policy_runner,
        refund_runner=lambda _state: pytest.fail("refund must not run"),
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=None,
        reviewer="security-reviewer@example.com",
        notes="The triage governance concern was reviewed and cleared.",
    )

    assert calls == ["policy", "response"]
    assert repository.refunds == []
    assert outcome.workflow_status == "completed"


def test_triage_approval_continues_policy_to_new_pending_review_without_recursion() -> None:
    repository = ServiceRepository(_resolution(next_agent="policy_agent"))
    policy_calls = 0

    def policy_runner(_state: dict[str, Any]) -> dict[str, Any]:
        nonlocal policy_calls
        policy_calls += 1
        return {
            "policy_decision": {
                "decision": "manual_review",
                "refund_amount": 0,
                "reason": "The remaining amount needs manager approval.",
            },
            "policy_persistence_result": {"next_agent": "human_approval"},
            "workflow_status": "waiting_human",
        }

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        policy_runner=policy_runner,
        refund_runner=lambda _state: pytest.fail("refund must not run"),
        response_runner=lambda _state: pytest.fail("response must not run"),
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=None,
        reviewer="security-reviewer@example.com",
        notes="The triage governance concern was reviewed and cleared.",
    )

    assert policy_calls == 1
    assert len(repository.approvals) == 1
    assert repository.approvals[0]["stage"] == "policy"
    assert repository.approvals[0]["reason"] == "The remaining amount needs manager approval."
    assert outcome.continuation_status == "pending_human"
    assert outcome.new_approval_id == "approval-demo01-response-2"
    assert repository.marks[0]["workflow_status"] == "pending_human"


def test_response_governance_escalation_creates_pending_review_and_stops() -> None:
    repository = ServiceRepository(_resolution(decision="deny", next_agent="response_agent"))
    response_calls = 0

    def blocked_response(_state: dict[str, Any]) -> dict[str, Any]:
        nonlocal response_calls
        response_calls += 1
        return {
            "response_result": {"workflow_status": "waiting_human"},
            "response_governance_result": {"status": "block"},
            "response_handoff": "human_review",
            "review_trigger_reason": "governance_block",
            "workflow_status": "waiting_human",
        }

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        response_runner=blocked_response,
    ).resolve(
        "demo01",
        decision="deny",
        resolved_amount=None,
        reviewer="reviewer@example.com",
        notes="Deny this request.",
    )

    assert response_calls == 1
    assert len(repository.approvals) == 1
    assert repository.approvals[0]["stage"] == "response"
    assert outcome.continuation_status == "pending_human"
    assert outcome.workflow_status == "pending_human"
    assert outcome.new_approval_id == "approval-demo01-response-2"
    assert repository.marks[0]["workflow_status"] == "pending_human"
    assert repository.approvals[0]["approval_claim_token"] == "approval-claim-demo01"


def test_stale_pending_response_route_repairs_missing_approval_before_terminal_marker() -> None:
    base = _resolution(decision="deny", next_agent="response_agent")
    resolution = replace(
        base,
        idempotent=True,
        continuation_resumable=True,
        state={
            **base.state,
            "workflow_status": "pending_human",
            "current_stage": "human_approval",
            "response_result": {"workflow_status": "waiting_human"},
            "response_handoff": "human_review",
            "review_trigger_reason": "response_governance_block",
        },
    )
    repository = ServiceRepository(resolution)

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=lambda _state: pytest.fail("recovery must not refund"),
        response_runner=lambda _state: pytest.fail("recovery must not rerun response"),
    ).resolve(
        "demo01",
        decision="deny",
        resolved_amount=None,
        reviewer="reviewer@example.com",
        notes="Deny this request.",
    )

    assert outcome.continuation_status == "recovered"
    assert outcome.workflow_status == "pending_human"
    assert outcome.new_approval_id == "approval-demo01-response-2"
    assert repository.approvals == [
        {
            "trace_id": "demo01",
            "reason": "response_governance_block",
            "stage": "response",
            "policy_decision": base.state["policy_decision"],
            "approval_claim_token": "approval-claim-demo01",
        }
    ]
    assert repository.marks[0]["summary"]["new_approval_id"] == outcome.new_approval_id


def test_fresh_duplicate_at_response_pending_boundary_waits_for_owner_ensure() -> None:
    base = _resolution(decision="deny", next_agent="response_agent")
    resolution = replace(
        base,
        idempotent=True,
        continuation_resumable=False,
        state={
            **base.state,
            "workflow_status": "pending_human",
            "current_stage": "human_approval",
            "response_result": {"workflow_status": "waiting_human"},
            "response_handoff": "human_review",
        },
    )
    repository = ServiceRepository(resolution)

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=lambda _state: pytest.fail("duplicate must not refund"),
        response_runner=lambda _state: pytest.fail("duplicate must not rerun response"),
    ).resolve(
        "demo01",
        decision="deny",
        resolved_amount=None,
        reviewer="reviewer@example.com",
        notes="Deny this request.",
    )

    assert outcome.continuation_status == "in_progress"
    assert repository.approvals == []
    assert repository.marks == []


def test_fresh_duplicate_after_completed_response_waits_for_owner_terminal_marker() -> None:
    base = _resolution(decision="deny", next_agent="response_agent")
    resolution = replace(
        base,
        idempotent=True,
        continuation_resumable=False,
        state={
            **base.state,
            "workflow_status": "completed",
            "current_stage": "completed",
            "response_result": _completed_response(base.state)["response_result"],
            "response_handoff": "end",
        },
    )
    repository = ServiceRepository(resolution)

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        response_runner=lambda _state: pytest.fail("duplicate must not rerun response"),
    ).resolve(
        "demo01",
        decision="deny",
        resolved_amount=None,
        reviewer="reviewer@example.com",
        notes="Deny this request.",
    )

    assert outcome.continuation_status == "in_progress"
    assert repository.marks == []


def test_identical_completed_resolution_does_not_run_agents_again() -> None:
    repository = ServiceRepository(_resolution(continuation_complete=True))
    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=lambda _state: pytest.fail("refund must not rerun"),
        response_runner=lambda _state: pytest.fail("response must not rerun"),
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )

    assert outcome.continuation_status == "already_completed"
    assert outcome.idempotent is True
    assert repository.marks == []


def test_fresh_duplicate_claim_reports_in_progress_without_duplicate_side_effects() -> None:
    resolution = replace(
        _resolution(),
        idempotent=True,
        continuation_resumable=False,
    )
    repository = ServiceRepository(resolution)
    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=lambda _state: pytest.fail("fresh claim must not rerun refund"),
        response_runner=lambda _state: pytest.fail("fresh claim must not rerun response"),
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )

    assert outcome.continuation_status == "in_progress"
    assert repository.refunds == []
    assert repository.marks == []


def test_stale_claim_is_rejected_synchronously_before_refund_side_effect() -> None:
    class StaleRepository(ServiceRepository):
        def heartbeat_human_approval_continuation(self, **_kwargs: Any) -> bool:
            raise CloudDatabaseError("stale human-approval claim token")

    repository = StaleRepository(_resolution())
    refund_called = False

    def refund_runner(_state: dict[str, Any]) -> dict[str, Any]:
        nonlocal refund_called
        refund_called = True
        return _successful_refund(_state)

    with pytest.raises(ReviewContinuationError, match="claim validation failed"):
        HumanApprovalService(
            repository,  # type: ignore[arg-type]
            refund_runner=refund_runner,
            response_runner=lambda _state: pytest.fail("response must not run"),
        ).resolve(
            "demo01",
            decision="approve",
            resolved_amount=79.99,
            reviewer="reviewer@example.com",
            notes="Reviewed against the order evidence.",
        )

    assert refund_called is False
    assert repository.refunds == []


def test_missing_marker_after_terminal_commit_is_repaired_without_rerunning_refund() -> None:
    resolution = replace(
        _resolution(),
        idempotent=True,
        continuation_resumable=True,
        state={**_resolution().state, "workflow_status": "completed", "current_stage": "completed"},
    )
    repository = ServiceRepository(resolution)
    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=lambda _state: pytest.fail("committed refund must not rerun"),
        response_runner=lambda _state: pytest.fail("committed response must not rerun"),
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )

    assert outcome.continuation_status == "recovered"
    assert repository.refunds == []
    assert repository.marks[0]["workflow_status"] == "completed"
    assert repository.marks[0]["summary"]["recovered_terminal_marker"] is True


def test_stale_terminal_recovery_replays_earlier_durable_response_exactly() -> None:
    store = DatabaseStore()
    repository = _database_repository(store, [])
    request = {
        "trace_id": "demo07",
        "decision": "deny",
        "resolved_amount": None,
        "reviewer": "manager@example.com",
        "notes": "The evidence does not support a refund.",
    }
    first = repository.resolve_human_approval(**request)
    repository.persist_agent_handoff(
        trace_id="demo07",
        ticket_id="ticket-demo07",
        from_agent="response_agent",
        to_agent="end",
        input_payload={"human_review": {}},
        output_payload={
            "response_result": {
                "final_outcome": "denied",
                "workflow_status": "completed",
                "response": {"body": "Persisted exact response."},
            },
            "response_handoff": "end",
        },
        audit_event_type="response_agent_evaluated",
        workflow_status="completed",
        current_agent="completed",
        approval_claim_token=first.continuation_claim_token,
    )
    store.claim_age_seconds = 31
    service = HumanApprovalService(
        repository,
        refund_runner=lambda _state: pytest.fail("recovery must not refund"),
        response_runner=lambda _state: pytest.fail("recovery must not rerun response"),
        continuation_lease_seconds=30,
    )

    recovered = service.resolve(**request)
    counts_after_recovery = (
        len(store.handoffs),
        len(store.audit),
        len(store.refunds),
    )
    replay = service.resolve(**request)

    assert recovered.continuation_status == "recovered"
    assert replay.continuation_status == "already_completed"
    assert recovered.response_result == replay.response_result == {
        "final_outcome": "denied",
        "workflow_status": "completed",
        "response": {"body": "Persisted exact response."},
    }
    assert counts_after_recovery == (
        len(store.handoffs),
        len(store.audit),
        len(store.refunds),
    )


def test_stale_claim_resumes_after_process_death_and_finishes_idempotently() -> None:
    resolution = replace(
        _resolution(),
        idempotent=True,
        continuation_resumable=True,
    )
    repository = ServiceRepository(resolution)
    calls: list[str] = []

    def refund_runner(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("refund")
        return _successful_refund(state)

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("response")
        return _completed_response(state)

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=refund_runner,
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )

    assert calls == ["refund", "response"]
    assert outcome.continuation_status == "completed"
    assert len(repository.refunds) == 1
    assert len(repository.marks) == 1


def test_stale_policy_route_with_durable_refund_resumes_only_response() -> None:
    base = _resolution(next_agent="policy_agent")
    resolution = replace(
        base,
        idempotent=True,
        continuation_resumable=True,
        state={
            **base.state,
            "policy_decision": {
                "decision": "approve",
                "refund_amount": 79.99,
                "reason": "Persisted Policy result.",
            },
            "policy_persistence_result": {
                "handoff_id": "policy-continuation",
                "next_agent": "refund_agent",
            },
            "refund_result": _successful_refund(base.state)["refund_result"],
            "refund_persistence_result": {
                "transaction_id": "refund-demo01",
                "handoff_id": "refund-continuation",
                "next_agent": "response_agent",
            },
        },
    )
    repository = ServiceRepository(resolution)
    response_calls = 0

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal response_calls
        response_calls += 1
        assert state["refund_result"]["refund_id"] == "RF-ticket-demo01"
        return _completed_response(state)

    outcome = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        policy_runner=lambda _state: pytest.fail("persisted policy must not rerun"),
        refund_runner=lambda _state: pytest.fail("persisted refund must not rerun"),
        response_runner=response_runner,
    ).resolve(
        "demo01",
        decision="approve",
        resolved_amount=None,
        reviewer="security-reviewer@example.com",
        notes="The triage governance concern was reviewed and cleared.",
    )

    assert outcome.continuation_status == "completed"
    assert response_calls == 1
    assert repository.refunds == []


def test_active_heartbeat_keeps_over_30_second_claim_in_progress_without_duplicate_stages() -> None:
    base = _resolution()

    class ConcurrentRepository(ServiceRepository):
        def __init__(self) -> None:
            super().__init__(base)
            self.calls = 0
            self.lock = Lock()
            self.heartbeat_seen = Event()
            self.simulated_claim_age_seconds = 31

        def resolve_human_approval(self, **_kwargs: Any) -> HumanApprovalResolution:
            with self.lock:
                self.calls += 1
                if self.calls == 1:
                    return base
                return replace(
                    base,
                    idempotent=True,
                    continuation_resumable=not self.heartbeat_seen.is_set(),
                )

        def heartbeat_human_approval_continuation(self, **_kwargs: Any) -> bool:
            self.simulated_claim_age_seconds = 0
            self.heartbeat_seen.set()
            return True

    repository = ConcurrentRepository()
    response_started = Event()
    release_response = Event()
    response_calls = 0

    def waiting_response(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal response_calls
        response_calls += 1
        response_started.set()
        assert release_response.wait(timeout=2)
        return _completed_response(state)

    service = HumanApprovalService(
        repository,  # type: ignore[arg-type]
        refund_runner=_successful_refund,
        response_runner=waiting_response,
        continuation_lease_seconds=30,
    )
    worker_result: list[Any] = []
    worker = Thread(
        target=lambda: worker_result.append(
            service.resolve(
                "demo01",
                decision="approve",
                resolved_amount=79.99,
                reviewer="reviewer@example.com",
                notes="Reviewed against the order evidence.",
            )
        )
    )
    worker.start()
    assert response_started.wait(timeout=2)
    assert repository.heartbeat_seen.wait(timeout=2)
    assert repository.simulated_claim_age_seconds == 0

    duplicate = service.resolve(
        "demo01",
        decision="approve",
        resolved_amount=79.99,
        reviewer="reviewer@example.com",
        notes="Reviewed against the order evidence.",
    )
    assert duplicate.continuation_status == "in_progress"
    assert len(repository.refunds) == 1
    assert response_calls == 1

    release_response.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(worker_result) == 1
    assert worker_result[0].continuation_status == "completed"
    assert len(repository.refunds) == 1
    assert response_calls == 1


def test_continuation_failure_is_persisted_and_fails_closed() -> None:
    repository = ServiceRepository(_resolution(decision="deny", next_agent="response_agent"))

    def broken_response(_state: dict[str, Any]) -> dict[str, Any]:
        raise TimeoutError("response timed out")

    service = HumanApprovalService(repository, response_runner=broken_response)  # type: ignore[arg-type]
    with pytest.raises(ReviewContinuationError, match="demo01"):
        service.resolve(
            "demo01",
            decision="deny",
            resolved_amount=None,
            reviewer="reviewer@example.com",
            notes="Deny this request.",
        )
    assert len(repository.failures) == 1
    assert isinstance(repository.failures[0]["error"], TimeoutError)


def test_default_policy_and_response_subgraphs_share_one_lazy_azure_client(monkeypatch) -> None:
    repository = ServiceRepository(_resolution(next_agent="policy_agent"))
    azure = object()
    captured: dict[str, Any] = {}

    class Graph:
        def __init__(self, name: str) -> None:
            self.name = name

        def invoke(self, _state: dict[str, Any]) -> dict[str, Any]:
            return {"runner": self.name}

    def build_policy(client: Any, *, store: Any) -> Graph:
        captured["policy_client"] = client
        captured["policy_store"] = store
        return Graph("policy")

    def build_response(*, client: Any, store: Any, event_writer: Any) -> Graph:
        captured["response_client"] = client
        captured["response_store"] = store
        captured["event_writer"] = event_writer
        return Graph("response")

    monkeypatch.setattr(review_module.AzureJsonClient, "from_env", lambda: azure)
    monkeypatch.setattr(policy_package, "build_policy_agent_graph", build_policy)
    monkeypatch.setattr(response_package, "build_response_agent_graph", build_response)
    service = HumanApprovalService(repository)  # type: ignore[arg-type]

    assert service._run_default_policy({}) == {"runner": "policy"}
    assert service._run_default_response({}) == {"runner": "response"}
    assert captured["policy_client"] is azure
    assert captured["response_client"] is azure


def test_governance_writer_propagates_approval_fence_and_rejects_cross_trace() -> None:
    calls: list[tuple[GovernanceStatement, dict[str, str]]] = []

    class Backend:
        def save_governance_event_record(self, statement, **kwargs):
            calls.append((statement, kwargs))
            return "approval-governance-event"

    repository = DatabaseGovernanceEventRepository(Backend())
    with approval_continuation_fence(
        trace_id="demo07",
        approval_id="approval-demo07",
        claim_token="claim-demo07",
        attempt=1,
        sequence=42,
    ):
        with pytest.raises(RuntimeError, match="crossed its trace fence"):
            repository.save_event(
                GovernanceStatement(
                    trace_id="demo08",
                    agent="response_agent",
                    stage="response_governance",
                    status="allow",
                    summary="Wrong trace.",
                )
            )
        assert repository.save_event(
            GovernanceStatement(
                trace_id="demo07",
                agent="response_agent",
                stage="response_governance",
                status="allow",
                summary="Correct trace.",
            )
        ) == "approval-governance-event"

    assert len(calls) == 1
    assert calls[0][1] == {"approval_claim_token": "claim-demo07"}


class DatabaseStore:
    def __init__(self) -> None:
        self.approval = {
            "approval_id": "approval-demo07",
            "trace_id": "demo07",
            "triggering_event_id": "policy-review-demo07",
            "triggering_event_type": "policy_review",
            "reason": "Manager review required.",
            "amount_requested": Decimal("299.99"),
            "resolved_amount": None,
            "status": "pending",
            "decision": None,
            "approved_next_agent": "refund_agent",
            "rejected_next_agent": "response_agent",
            "reviewer": None,
            "notes": '{"review_source":"policy_review"}',
            "resolved_at": None,
            "ticket_id": "ticket-demo07",
            "workflow_status": "pending_human",
            "current_agent": "human_approval",
            "policy_version": "v1.0",
            "customer_id": "customer-demo07",
            "raw_text": "Monitor arrived broken.",
            "sanitized_text": "Monitor arrived broken.",
            "refund_reason": "damaged",
            "requested_amount": Decimal("299.99"),
            "ticket_currency": "USD",
            "customer_email": "sara@example.com",
            "customer_name": "Sara",
            "governance_agent": None,
            "governance_flags": None,
            "policy_review_evidence": None,
        }
        self.approvals = [self.approval]
        self.workflow = {"status": "pending_human", "current_agent": "human_approval"}
        self.handoffs = [
            {
                "handoff_id": "policy-demo07",
                "from_agent": "policy_agent",
                "to_agent": "human_approval",
                "input_json": "{}",
                "output_json": json.dumps(
                    {
                        "decision": {
                            "type": "manual_review",
                            "refund_amount": 299.99,
                            "reason": "Manager review required.",
                        }
                    }
                ),
            }
        ]
        self.orders = [
            {
                "order_id": "order-demo07",
                "order_customer_id": "customer-demo07",
                "product_type": "monitor",
                "purchase_date": "2026-06-12",
                "item_status": "damaged",
                "amount_paid": Decimal("299.99"),
                "prior_refund_total": Decimal("100.00"),
                "currency": "USD",
                "contact_customer_id": "customer-demo07",
                "contact_email": "sara@example.com",
                "contact_name": "Sara",
            }
        ]
        self.refunds: list[dict[str, Any]] = []
        self.governance: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self.claim_age_seconds = 0
        self.supersede_claim_on_workflow_lock = False
        self.next_log_id = 1
        self.approval_handoffs: list[tuple[Any, ...]] = []
        self.queries: list[str] = []


class DatabaseCursor:
    def __init__(self, store: DatabaseStore, *, dictionary: bool) -> None:
        self.store = store
        self.dictionary = dictionary
        self.rows: list[Any] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        compact = " ".join(sql.split())
        self.store.queries.append(compact)
        self.rowcount = 0
        if "FROM human_approvals approvals" in compact:
            self.rows = []
            for approval in self.store.approvals:
                row = dict(approval)
                row["workflow_status"] = self.store.workflow["status"]
                row["current_agent"] = self.store.workflow["current_agent"]
                self.rows.append(row)
        elif compact.startswith("SELECT status FROM workflow_runs"):
            if self.store.supersede_claim_on_workflow_lock:
                latest_claim = next(
                    row
                    for row in reversed(self.store.audit)
                    if row["event_type"] == "human_approval_continuation_claimed"
                )
                latest_payload = json.loads(latest_claim["payload_json"])
                attempt = int(latest_payload["attempt"]) + 1
                self.store.audit.append(
                    {
                        "log_id": self.store.next_log_id,
                        "event_type": "human_approval_continuation_claimed",
                        "payload_json": json.dumps(
                            {
                                **latest_payload,
                                "attempt": attempt,
                                "claim_token": _approval_continuation_claim_token(
                                    str(self.store.approval["trace_id"]),
                                    str(latest_payload["approval_id"]),
                                    attempt,
                                ),
                            }
                        ),
                    }
                )
                self.store.next_log_id += 1
                self.store.supersede_claim_on_workflow_lock = False
            self.rows = [{"status": self.store.workflow["status"]}]
        elif compact.startswith("SELECT status FROM human_approvals"):
            approval = next(
                (
                    row
                    for row in self.store.approvals
                    if row["approval_id"] == params[1]
                ),
                None,
            )
            self.rows = [{"status": approval["status"]}] if approval else []
        elif (
            compact.startswith("SELECT approval_id FROM human_approvals")
            and "status = 'pending'" in compact
        ):
            self.rows = [
                (
                    {"approval_id": row["approval_id"]}
                    if self.dictionary
                    else (row["approval_id"],)
                )
                for row in self.store.approvals
                if row["status"] == "pending"
            ]
        elif "FROM agent_handoffs" in compact and compact.startswith("SELECT"):
            self.rows = [dict(row) for row in self.store.handoffs]
        elif "FROM orders" in compact and compact.startswith("SELECT"):
            self.rows = [dict(row) for row in self.store.orders]
        elif "FROM refund_transactions" in compact and compact.startswith("SELECT"):
            self.rows = [
                dict(row)
                for row in self.store.refunds
                if row.get("approval_id") == params[1]
            ]
        elif compact.startswith("SELECT event_id, flags_json FROM governance_events"):
            rows = [
                row
                for row in self.store.governance
                if row["trace_id"] == params[0]
                and row["agent"] == params[1]
                and row["interceptor_action"] in {"block", "quarantine"}
            ]
            self.rows = [
                (
                    {"event_id": row["event_id"], "flags_json": row["flags_json"]}
                    if self.dictionary
                    else (row["event_id"], row["flags_json"])
                )
                for row in reversed(rows)
            ]
        elif compact.startswith("SELECT tickets.requested_amount"):
            self.rows = [(self.store.approval["requested_amount"],)]
        elif compact.startswith("SELECT COUNT(*) FROM human_approvals"):
            count = sum(
                row["triggering_event_type"] == params[1]
                and row["triggering_event_id"] == params[2]
                for row in self.store.approvals
            )
            self.rows = [(count,)]
        elif compact.startswith("UPDATE human_approvals"):
            approval = next(
                (
                    row
                    for row in self.store.approvals
                    if row["approval_id"] == params[-1]
                ),
                None,
            )
            if approval is None or approval["status"] != "pending":
                self.rowcount = 0
            else:
                (
                    approval["status"],
                    approval["decision"],
                    approval["resolved_amount"],
                    approval["reviewer"],
                    approval["notes"],
                    _approval_id,
                ) = params
                approval["resolved_at"] = "now"
                self.rowcount = 1
            self.rows = []
        elif compact.startswith("INSERT INTO refund_transactions"):
            row = {
                "transaction_id": params[0],
                "approval_id": params[2],
                "amount": params[3],
                "currency": params[4],
                "status": params[5],
                "external_ref": params[6],
                "created_at": "now",
                "updated_at": "now",
            }
            existing = next(
                (
                    item
                    for item in self.store.refunds
                    if item["transaction_id"] == row["transaction_id"]
                ),
                None,
            )
            if existing is None:
                self.store.refunds.append(row)
            else:
                existing.update(row)
            self.rowcount = 1
            self.rows = []
        elif compact.startswith("INSERT INTO human_approvals"):
            governance = next(
                (
                    row
                    for row in self.store.governance
                    if row["event_id"] == params[2]
                ),
                None,
            )
            self.store.approvals.append(
                {
                    **deepcopy(self.store.approval),
                    "approval_id": params[0],
                    "trace_id": params[1],
                    "triggering_event_id": params[2],
                    "triggering_event_type": params[3],
                    "reason": params[4],
                    "amount_requested": params[5],
                    "status": "pending",
                    "decision": None,
                    "resolved_amount": None,
                    "reviewer": None,
                    "notes": params[7],
                    "resolved_at": None,
                    "approved_next_agent": params[6],
                    "rejected_next_agent": "response_agent",
                    "governance_agent": governance["agent"] if governance else None,
                    "governance_flags": governance["flags_json"] if governance else None,
                    "policy_review_evidence": None,
                }
            )
            self.rowcount = 1
            self.rows = []
        elif compact.startswith("INSERT INTO agent_handoffs"):
            if len(params) == 9:
                row = {
                    "handoff_id": params[0],
                    "from_agent": params[3],
                    "to_agent": params[4],
                    "input_json": params[5],
                    "output_json": params[6],
                    "created_at": "now",
                }
                existing = next(
                    (
                        item for item in self.store.handoffs
                        if item["handoff_id"] == row["handoff_id"]
                    ),
                    None,
                )
                if existing is None:
                    self.store.handoffs.append(row)
                else:
                    existing.update(row)
            else:
                self.store.approval_handoffs.append(params)
            self.rowcount = 1
            self.rows = []
        elif compact.startswith("INSERT INTO governance_events"):
            row = {
                "event_id": params[0],
                "trace_id": params[1],
                "agent": params[2],
                "owasp_category": params[3],
                "trigger_score": params[4],
                "interceptor_action": params[5],
                "flags_json": params[6],
                "offending_content": params[7],
            }
            existing = next(
                (
                    item for item in self.store.governance
                    if item["event_id"] == row["event_id"]
                ),
                None,
            )
            if existing is None:
                self.store.governance.append(row)
            else:
                existing.update(row)
            self.rowcount = 1
            self.rows = []
        elif compact.startswith("INSERT INTO audit_log"):
            if len(params) == 4:
                event_type = str(params[1])
                payload_json = params[3]
            elif len(params) == 3:
                event_type = str(params[1])
                payload_json = params[2]
            else:
                event_type = next(
                    value
                    for value in (
                        "human_approval_continuation_claimed",
                        "human_approval_continued",
                        "human_approval_continuation_failed",
                        "human_approval_resolved",
                    )
                    if value in compact
                )
                payload_json = params[1]
            self.store.audit.append(
                {
                    "log_id": self.store.next_log_id,
                    "event_type": event_type,
                    "payload_json": payload_json,
                }
            )
            self.store.next_log_id += 1
            self.rowcount = 1
            self.rows = []
        elif compact.startswith("UPDATE audit_log"):
            payload, log_id, _trace_id = params
            row = next(item for item in self.store.audit if item["log_id"] == log_id)
            row["payload_json"] = payload
            self.store.claim_age_seconds = 0
            self.rowcount = 1
            self.rows = []
        elif compact.startswith("UPDATE workflow_runs"):
            if len(params) == 4:
                self.store.workflow["status"] = params[0]
                self.store.workflow["current_agent"] = params[1]
            elif "SET status = 'failed', current_agent = 'human_approval'" in compact:
                self.store.workflow["status"] = "failed"
                self.store.workflow["current_agent"] = "human_approval"
            elif "SET status = 'pending_human', current_agent = 'human_approval'" in compact:
                self.store.workflow["status"] = "pending_human"
                self.store.workflow["current_agent"] = "human_approval"
            elif "SET updated_at = CURRENT_TIMESTAMP" in compact:
                self.store.claim_age_seconds = 0
            self.rowcount = 1
            self.rows = []
        elif "event_type = 'human_approval_continued'" in compact:
            self.rows = [
                {"payload_json": row["payload_json"]}
                for row in self.store.audit
                if row["event_type"] == "human_approval_continued"
            ]
        elif "event_type = 'human_approval_continuation_failed'" in compact:
            self.rows = [
                {"payload_json": row["payload_json"]}
                for row in self.store.audit
                if row["event_type"] == "human_approval_continuation_failed"
            ]
        elif "event_type = 'human_approval_continuation_claimed'" in compact:
            claims = [
                row
                for row in self.store.audit
                if row["event_type"] == "human_approval_continuation_claimed"
            ]
            self.rows = (
                [
                    {
                        "log_id": claims[-1]["log_id"],
                        "payload_json": claims[-1]["payload_json"],
                        "created_at": "now",
                        "age_seconds": self.store.claim_age_seconds,
                    }
                ]
                if claims
                else []
            )
        elif compact.startswith("SELECT approval_id, status FROM human_approvals"):
            self.rows = [
                {
                    "approval_id": self.store.approval["approval_id"],
                    "status": self.store.approval["status"],
                }
            ]
        elif compact.startswith("SELECT COUNT(*) FROM workflow_runs"):
            self.rows = [{"COUNT(*)": 1}] if self.dictionary else [(1,)]
        else:
            raise AssertionError(f"Unexpected SQL: {compact}")

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None


class DatabaseConnection:
    def __init__(self, store: DatabaseStore) -> None:
        self.store = store
        self.started = False
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def start_transaction(self) -> None:
        self.started = True

    def cursor(self, dictionary: bool = False) -> DatabaseCursor:
        return DatabaseCursor(self.store, dictionary=dictionary)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _database_repository(store: DatabaseStore, connections: list[DatabaseConnection]) -> GCPRepository:
    repository = GCPRepository({"database": "final"})

    def connect() -> DatabaseConnection:
        connection = DatabaseConnection(store)
        connections.append(connection)
        return connection

    repository._connect = connect  # type: ignore[method-assign]
    return repository


def test_repository_locks_and_atomically_resolves_pending_approval() -> None:
    store = DatabaseStore()
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)

    result = repository.resolve_human_approval(
        trace_id="demo07",
        decision="partial_refund",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the remaining refundable balance.",
    )

    assert connections[0].started and connections[0].committed
    assert not connections[0].rolled_back
    assert any("FOR UPDATE" in query and "human_approvals approvals" in query for query in store.queries)
    assert store.approval["status"] == "approved"
    assert store.approval["decision"] == "partial_refund"
    assert store.workflow == {"status": "running", "current_agent": "refund_agent"}
    assert len(store.approval_handoffs) == 1
    assert store.approval_handoffs[0][0] == "39751ff1-53c2-5fcf-b120-7eee490770a4"
    assert [row["event_type"] for row in store.audit] == [
        "human_approval_resolved",
        "human_approval_continuation_claimed",
    ]
    assert result.state["requested_order_id"] == "order-demo07"
    assert result.state["policy_decision"]["refund_amount"] == 199.99
    assert result.idempotent is False


@pytest.mark.parametrize("resolved_amount", [Decimal("199.99"), Decimal("299.99")])
def test_repository_requires_partial_refund_when_request_exceeds_remaining_balance(
    resolved_amount: Decimal,
) -> None:
    store = DatabaseStore()
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)

    with pytest.raises(ValueError, match="full approval is unavailable.*use partial_refund"):
        repository.resolve_human_approval(
            trace_id="demo07",
            decision="approve",
            resolved_amount=resolved_amount,
            reviewer="manager@example.com",
            notes="Approve the full request.",
        )

    assert connections[0].started and connections[0].rolled_back
    assert not connections[0].committed
    assert store.approval["status"] == "pending"
    assert store.approval_handoffs == []
    assert store.audit == []


def test_repository_exactly_replays_legacy_full_approval_before_new_amount_rules() -> None:
    store = DatabaseStore()
    store.approval.update(
        {
            "status": "approved",
            "decision": "approve",
            "resolved_amount": Decimal("199.99"),
            "reviewer": "manager@example.com",
            "notes": "Approve the remaining refundable balance.",
            "workflow_status": "completed",
            "current_agent": "completed",
        }
    )
    store.workflow.update({"status": "completed", "current_agent": "completed"})
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)

    replay = repository.resolve_human_approval(
        trace_id="demo07",
        decision="approve",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the remaining refundable balance.",
    )

    assert replay.idempotent is True
    assert replay.resolved_amount == 199.99
    assert connections[0].committed and not connections[0].rolled_back
    assert store.approval_handoffs == []
    assert [row["event_type"] for row in store.audit] == [
        "human_approval_continuation_claimed"
    ]

    with pytest.raises(HumanApprovalConflictError, match="different decision"):
        repository.resolve_human_approval(
            trace_id="demo07",
            decision="partial_refund",
            resolved_amount=Decimal("199.99"),
            reviewer="manager@example.com",
            notes="Approve the remaining refundable balance.",
        )


def test_repository_full_approve_requires_exact_requested_amount() -> None:
    store = DatabaseStore()
    store.approval["amount_requested"] = Decimal("199.99")
    store.approval["requested_amount"] = Decimal("199.99")
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)

    with pytest.raises(ValueError, match="must equal the full requested amount 199.99"):
        repository.resolve_human_approval(
            trace_id="demo07",
            decision="approve",
            resolved_amount=Decimal("150.00"),
            reviewer="manager@example.com",
            notes="Approve less than the full request.",
        )

    result = repository.resolve_human_approval(
        trace_id="demo07",
        decision="approve",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the exact full request.",
    )

    assert result.resolved_amount == 199.99
    assert store.approval["decision"] == "approve"


def test_repository_full_approve_uses_remaining_balance_when_request_is_missing() -> None:
    store = DatabaseStore()
    store.approval["amount_requested"] = None
    store.approval["requested_amount"] = None
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)

    with pytest.raises(ValueError, match="must equal the full remaining refundable amount 199.99"):
        repository.resolve_human_approval(
            trace_id="demo07",
            decision="approve",
            resolved_amount=Decimal("199.98"),
            reviewer="manager@example.com",
            notes="Approve less than the full remaining balance.",
        )

    result = repository.resolve_human_approval(
        trace_id="demo07",
        decision="approve",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the exact remaining balance.",
    )

    assert result.resolved_amount == 199.99
    assert store.approval["decision"] == "approve"


@pytest.mark.parametrize("decision", ["approve", "partial_refund"])
def test_repository_rejects_refund_approval_for_zero_requested_amount(
    decision: str,
) -> None:
    store = DatabaseStore()
    store.approval["amount_requested"] = Decimal("0.00")
    store.approval["requested_amount"] = Decimal("0.00")
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)

    with pytest.raises(ValueError, match="exceeds the requested amount"):
        repository.resolve_human_approval(
            trace_id="demo07",
            decision=decision,
            resolved_amount=Decimal("1.00"),
            reviewer="manager@example.com",
            notes="Attempt a refund against a zero request.",
        )

    assert connections[0].rolled_back and not connections[0].committed
    assert store.approval["status"] == "pending"


def test_repository_allows_triage_approval_to_claim_policy_continuation() -> None:
    store = DatabaseStore()
    store.approval["approved_next_agent"] = "policy_agent"
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)

    result = repository.resolve_human_approval(
        trace_id="demo07",
        decision="approve",
        resolved_amount=None,
        reviewer="security-reviewer@example.com",
        notes="The triage governance concern was reviewed and cleared.",
    )

    assert result.next_agent == "policy_agent"
    assert store.workflow == {"status": "running", "current_agent": "policy_agent"}
    assert store.approval_handoffs[0][3] == "policy_agent"


def test_repository_rejects_partial_refund_decision_for_non_refund_route() -> None:
    store = DatabaseStore()
    store.approval["approved_next_agent"] = "policy_agent"
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)

    with pytest.raises(ValueError, match="only for refund continuations"):
        repository.resolve_human_approval(
            trace_id="demo07",
            decision="partial_refund",
            resolved_amount=None,
            reviewer="security-reviewer@example.com",
            notes="This decision is invalid for a Policy continuation.",
        )

    assert connections[0].rolled_back and not connections[0].committed
    assert store.approval["status"] == "pending"


def test_repository_same_resolution_is_idempotent_but_conflict_is_rejected() -> None:
    store = DatabaseStore()
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)
    request = {
        "trace_id": "demo07",
        "decision": "partial_refund",
        "resolved_amount": Decimal("199.99"),
        "reviewer": "manager@example.com",
        "notes": "Approve the remaining refundable balance.",
    }
    repository.resolve_human_approval(**request)
    repeated = repository.resolve_human_approval(**request)

    assert repeated.idempotent is True
    assert len(store.approval_handoffs) == 1
    assert [row["event_type"] for row in store.audit] == [
        "human_approval_resolved",
        "human_approval_continuation_claimed",
    ]

    with pytest.raises(HumanApprovalConflictError, match="different decision"):
        repository.resolve_human_approval(
            trace_id="demo07",
            decision="deny",
            resolved_amount=None,
            reviewer="manager@example.com",
            notes="Deny instead.",
        )
    assert connections[-1].rolled_back is True


def test_repository_refreshes_only_a_stale_missing_terminal_claim() -> None:
    store = DatabaseStore()
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)
    request = {
        "trace_id": "demo07",
        "decision": "partial_refund",
        "resolved_amount": Decimal("199.99"),
        "reviewer": "manager@example.com",
        "notes": "Approve the remaining refundable balance.",
        "continuation_stale_after_seconds": 30,
    }

    first = repository.resolve_human_approval(**request)
    immediate = repository.resolve_human_approval(**request)
    assert first.continuation_resumable is True
    assert immediate.idempotent is True
    assert immediate.continuation_complete is False
    assert immediate.continuation_resumable is False

    store.claim_age_seconds = 31
    recovered = repository.resolve_human_approval(**request)
    assert recovered.idempotent is True
    assert recovered.continuation_resumable is True
    claims = [
        row for row in store.audit
        if row["event_type"] == "human_approval_continuation_claimed"
    ]
    assert len(claims) == 2
    assert [json.loads(row["payload_json"])["attempt"] for row in claims] == [1, 2]
    assert claims[0]["payload_json"] != claims[1]["payload_json"]


def test_repository_heartbeat_refreshes_workflow_activity_without_mutating_claim_audit() -> None:
    store = DatabaseStore()
    repository = _database_repository(store, [])
    first = repository.resolve_human_approval(
        trace_id="demo07",
        decision="partial_refund",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the remaining refundable balance.",
    )
    claim = next(
        row for row in store.audit
        if row["event_type"] == "human_approval_continuation_claimed"
    )
    original_payload = claim["payload_json"]
    store.claim_age_seconds = 31

    assert repository.heartbeat_human_approval_continuation(
        trace_id="demo07",
        approval_id=first.approval_id,
        approval_claim_token=first.continuation_claim_token or "",
    ) is True

    assert store.claim_age_seconds == 0
    assert claim["payload_json"] == original_payload
    immediate = repository.resolve_human_approval(
        trace_id="demo07",
        decision="partial_refund",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the remaining refundable balance.",
    )
    assert immediate.idempotent is True
    assert immediate.continuation_resumable is False


def test_retry_claim_heartbeat_refreshes_lease_while_workflow_remains_failed() -> None:
    store = DatabaseStore()
    repository = _database_repository(store, [])
    request = {
        "trace_id": "demo07",
        "decision": "partial_refund",
        "resolved_amount": Decimal("199.99"),
        "reviewer": "manager@example.com",
        "notes": "Approve the remaining refundable balance.",
    }
    first = repository.resolve_human_approval(**request)
    repository.record_human_approval_continuation_failure(
        trace_id="demo07",
        approval_id=first.approval_id,
        error=TimeoutError("response timed out"),
        approval_claim_token=first.continuation_claim_token or "",
    )
    retry = repository.resolve_human_approval(**request)
    assert store.workflow["status"] == "failed"
    store.claim_age_seconds = 31

    assert repository.heartbeat_human_approval_continuation(
        trace_id="demo07",
        approval_id=retry.approval_id,
        approval_claim_token=retry.continuation_claim_token or "",
    ) is True
    assert store.claim_age_seconds == 0


def test_repository_rejects_stale_and_failed_approval_claim_tokens() -> None:
    store = DatabaseStore()
    repository = _database_repository(store, [])
    first = repository.resolve_human_approval(
        trace_id="demo07",
        decision="partial_refund",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the remaining refundable balance.",
    )

    with pytest.raises(CloudDatabaseError, match="stale human-approval claim token"):
        repository.heartbeat_human_approval_continuation(
            trace_id="demo07",
            approval_id=first.approval_id,
            approval_claim_token="stale-token",
        )

    repository.record_human_approval_continuation_failure(
        trace_id="demo07",
        approval_id=first.approval_id,
        error=TimeoutError("response timed out"),
        approval_claim_token=first.continuation_claim_token or "",
    )
    with pytest.raises(CloudDatabaseError, match="failed human-approval claim token"):
        repository.heartbeat_human_approval_continuation(
            trace_id="demo07",
            approval_id=first.approval_id,
            approval_claim_token=first.continuation_claim_token or "",
        )


def test_workflow_lock_precedes_latest_claim_read_and_rejects_interleaved_supersession() -> None:
    store = DatabaseStore()
    repository = _database_repository(store, [])
    first = repository.resolve_human_approval(
        trace_id="demo07",
        decision="partial_refund",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the remaining refundable balance.",
    )
    query_start = len(store.queries)
    store.supersede_claim_on_workflow_lock = True

    with pytest.raises(CloudDatabaseError, match="stale human-approval claim token"):
        repository.heartbeat_human_approval_continuation(
            trace_id="demo07",
            approval_id=first.approval_id,
            approval_claim_token=first.continuation_claim_token or "",
        )

    interleaved_queries = store.queries[query_start:]
    assert interleaved_queries[0].startswith(
        "SELECT status FROM workflow_runs WHERE trace_id = %s FOR UPDATE"
    )
    assert "human_approval_continuation_claimed" in interleaved_queries[1]
    assert not any(query.startswith("UPDATE workflow_runs") for query in interleaved_queries)
    claims = [
        json.loads(row["payload_json"])
        for row in store.audit
        if row["event_type"] == "human_approval_continuation_claimed"
    ]
    assert [claim["attempt"] for claim in claims] == [1, 2]


def test_repository_continuation_marker_is_durable_and_idempotent() -> None:
    store = DatabaseStore()
    connections: list[DatabaseConnection] = []
    repository = _database_repository(store, connections)
    request = {
        "trace_id": "demo07",
        "decision": "partial_refund",
        "resolved_amount": Decimal("199.99"),
        "reviewer": "manager@example.com",
        "notes": "Approve the remaining refundable balance.",
    }
    first = repository.resolve_human_approval(**request)

    assert repository.mark_human_approval_continuation(
        trace_id="demo07",
        approval_id=first.approval_id,
        workflow_status="completed",
        current_agent="completed",
        summary={"final_outcome": "partial_refund"},
        approval_claim_token=first.continuation_claim_token or "",
    ) is True
    assert repository.mark_human_approval_continuation(
        trace_id="demo07",
        approval_id=first.approval_id,
        workflow_status="completed",
        current_agent="completed",
        summary={"final_outcome": "partial_refund"},
        approval_claim_token=first.continuation_claim_token or "",
    ) is False

    repeated = repository.resolve_human_approval(**request)
    assert repeated.idempotent is True
    assert repeated.continuation_complete is True
    assert repeated.state["workflow_status"] == "completed"
    assert len(
        [row for row in store.audit if row["event_type"] == "human_approval_continued"]
    ) == 1


def test_pending_terminal_marker_requires_exact_new_pending_approval() -> None:
    store = DatabaseStore()
    repository = _database_repository(store, [])
    resolution = repository.resolve_human_approval(
        trace_id="demo07",
        decision="deny",
        resolved_amount=None,
        reviewer="manager@example.com",
        notes="The evidence does not support a refund.",
    )

    with pytest.raises(
        HumanApprovalConflictError,
        match="requires its exact pending approval",
    ):
        repository.mark_human_approval_continuation(
            trace_id="demo07",
            approval_id=resolution.approval_id,
            workflow_status="pending_human",
            current_agent="human_approval",
            summary={"new_approval_id": "approval-demo07-response-2"},
            approval_claim_token=resolution.continuation_claim_token or "",
        )

    assert not any(
        row["event_type"] == "human_approval_continued" for row in store.audit
    )


def test_child_approval_cannot_supersede_parent_claim_before_parent_terminal() -> None:
    store = DatabaseStore()
    repository = _database_repository(store, [])
    parent = repository.resolve_human_approval(
        trace_id="demo07",
        decision="deny",
        resolved_amount=None,
        reviewer="parent-reviewer@example.com",
        notes="Generate a reviewed response.",
    )
    parent_marker = {
        "type": "human_approval",
        "approval_id": parent.approval_id,
        "claim_token": parent.continuation_claim_token,
        "attempt": parent.continuation_attempt,
        "sequence": parent.continuation_sequence,
    }
    child_id = "approval-demo07-response-2"
    child = {
        **deepcopy(store.approval),
        "approval_id": child_id,
        "triggering_event_id": "response-governance-child",
        "triggering_event_type": "governance",
        "governance_agent": "response_agent",
        "governance_flags": json.dumps({"_continuation": parent_marker}),
        "policy_review_evidence": None,
        "reason": "Review the blocked response.",
        "status": "pending",
        "decision": None,
        "resolved_amount": None,
        "reviewer": None,
        "notes": '{"review_source":"response"}',
        "resolved_at": None,
        "approved_next_agent": "end",
        "rejected_next_agent": "response_agent",
    }
    store.approvals.append(child)
    store.workflow = {"status": "pending_human", "current_agent": "human_approval"}
    claim_count = len(
        [
            row
            for row in store.audit
            if row["event_type"] == "human_approval_continuation_claimed"
        ]
    )

    with pytest.raises(HumanApprovalStateError, match="before its parent continuation"):
        repository.resolve_human_approval(
            trace_id="demo07",
            approval_id=child_id,
            decision="approve",
            resolved_amount=None,
            reviewer="child-reviewer@example.com",
            notes="Release the corrected response.",
        )

    assert child["status"] == "pending"
    assert claim_count == len(
        [
            row
            for row in store.audit
            if row["event_type"] == "human_approval_continuation_claimed"
        ]
    )
    store.claim_age_seconds = 31
    recovered_parent = repository.resolve_human_approval(
        trace_id="demo07",
        approval_id=parent.approval_id,
        decision="deny",
        resolved_amount=None,
        reviewer="parent-reviewer@example.com",
        notes="Generate a reviewed response.",
        continuation_stale_after_seconds=30,
    )
    assert recovered_parent.continuation_attempt == 2
    assert recovered_parent.continuation_sequence != parent.continuation_sequence
    assert repository.mark_human_approval_continuation(
        trace_id="demo07",
        approval_id=parent.approval_id,
        workflow_status="pending_human",
        current_agent="human_approval",
        summary={"new_approval_id": child_id},
        approval_claim_token=recovered_parent.continuation_claim_token or "",
    ) is True

    resolved_child = repository.resolve_human_approval(
        trace_id="demo07",
        approval_id=child_id,
        decision="approve",
        resolved_amount=None,
        reviewer="child-reviewer@example.com",
        notes="Release the corrected response.",
    )

    assert resolved_child.idempotent is False
    assert resolved_child.continuation_claim_token
    latest_claim = json.loads(
        next(
            row
            for row in reversed(store.audit)
            if row["event_type"] == "human_approval_continuation_claimed"
        )["payload_json"]
    )
    assert latest_claim["approval_id"] == child_id


def test_refund_transaction_links_exact_claimed_approval_not_latest_approved_row() -> None:
    store = DatabaseStore()
    repository = _database_repository(store, [])
    resolution = repository.resolve_human_approval(
        trace_id="demo07",
        decision="partial_refund",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the remaining refundable balance.",
    )
    newer_approval = {
        **deepcopy(store.approval),
        "approval_id": "approval-demo07-newer",
        "status": "approved",
        "decision": "approve",
        "resolved_amount": None,
        "approved_next_agent": "end",
        "resolved_at": "later",
    }
    store.approvals.append(newer_approval)
    query_start = len(store.queries)

    transaction_id, _handoff_id = repository.persist_refund_result(
        trace_id="demo07",
        ticket_id="ticket-demo07",
        policy_decision={"decision": "partial_refund", "refund_amount": 199.99},
        order_lookup_result={"order_id": "order-demo07"},
        refund_result={
            "status": "success",
            "amount": 199.99,
            "currency": "USD",
            "refund_id": "RF-demo07",
        },
        approval_claim_token=resolution.continuation_claim_token,
    )

    refund = next(row for row in store.refunds if row["transaction_id"] == transaction_id)
    assert refund["approval_id"] == resolution.approval_id
    assert not any(
        query.startswith("SELECT approval_id FROM human_approvals")
        and "ORDER BY resolved_at" in query
        for query in store.queries[query_start:]
    )


def test_stale_retry_after_refund_commit_resumes_response_without_second_refund() -> None:
    store = DatabaseStore()
    repository = _database_repository(store, [])
    request = {
        "trace_id": "demo07",
        "decision": "partial_refund",
        "resolved_amount": Decimal("199.99"),
        "reviewer": "manager@example.com",
        "notes": "Approve the remaining refundable balance.",
    }
    first = repository.resolve_human_approval(**request)
    repository.persist_refund_result(
        trace_id="demo07",
        ticket_id="ticket-demo07",
        policy_decision={"decision": "partial_refund", "refund_amount": 199.99},
        order_lookup_result={"order_id": "order-demo07"},
        refund_result={
            "status": "success",
            "amount": 199.99,
            "currency": "USD",
            "refund_id": "RF-demo07",
        },
        approval_claim_token=first.continuation_claim_token,
    )
    store.claim_age_seconds = 31
    refund_count = len(store.refunds)
    refund_handoff_count = len(
        [row for row in store.handoffs if row["from_agent"] == "refund_agent"]
    )
    response_calls = 0

    def response_runner(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal response_calls
        response_calls += 1
        assert state["refund_result"]["refund_id"] == "RF-demo07"
        assert state["refund_persistence_result"]["transaction_id"]
        return _completed_response(state)

    outcome = HumanApprovalService(
        repository,
        refund_runner=lambda _state: pytest.fail("persisted refund must not execute again"),
        response_runner=response_runner,
        continuation_lease_seconds=30,
    ).resolve(**request)

    assert outcome.continuation_status == "completed"
    assert outcome.refund_result["refund_id"] == "RF-demo07"
    assert response_calls == 1
    assert len(store.refunds) == refund_count == 1
    assert len(
        [row for row in store.handoffs if row["from_agent"] == "refund_agent"]
    ) == refund_handoff_count == 1


def test_stale_pending_recovery_binds_child_to_prior_response_governance_checkpoint() -> None:
    store = DatabaseStore()
    repository = _database_repository(store, [])
    request = {
        "trace_id": "demo07",
        "decision": "deny",
        "resolved_amount": None,
        "reviewer": "manager@example.com",
        "notes": "The evidence does not support a refund.",
    }
    first = repository.resolve_human_approval(**request)
    governance_event_id = repository.save_governance_event_record(
        GovernanceStatement(
            trace_id="demo07",
            agent="response_agent",
            stage="response_governance",
            status="block",
            summary="The response requires another review.",
            findings=[
                GovernanceFinding(
                    flag="semantic_drift",
                    detail="The generated response requires manager review.",
                    source="deterministic",
                )
            ],
        ),
        approval_claim_token=first.continuation_claim_token,
    )
    repository.persist_agent_handoff(
        trace_id="demo07",
        ticket_id="ticket-demo07",
        from_agent="response_agent",
        to_agent="human_approval",
        input_payload={"human_review": {}},
        output_payload={
            "response_result": {"workflow_status": "waiting_human"},
            "response_handoff": "human_review",
        },
        audit_event_type="response_agent_evaluated",
        workflow_status="waiting_human",
        current_agent="human_approval",
        approval_claim_token=first.continuation_claim_token,
    )
    first_marker = json.loads(
        next(
            row
            for row in store.handoffs
            if row["from_agent"] == "response_agent"
        )["output_json"]
    )["_continuation"]
    store.claim_age_seconds = 31

    outcome = HumanApprovalService(
        repository,
        refund_runner=lambda _state: pytest.fail("recovery must not refund"),
        response_runner=lambda _state: pytest.fail("recovery must not rerun response"),
        continuation_lease_seconds=30,
    ).resolve(**request)

    assert outcome.continuation_status == "recovered"
    assert outcome.workflow_status == "pending_human"
    assert outcome.new_approval_id
    child = next(
        row
        for row in store.approvals
        if row["approval_id"] == outcome.new_approval_id
    )
    assert child["status"] == "pending"
    assert child["triggering_event_id"] == governance_event_id
    assert json.loads(child["governance_flags"])["_continuation"] == first_marker
    terminal = json.loads(
        next(
            row
            for row in store.audit
            if row["event_type"] == "human_approval_continued"
        )["payload_json"]
    )
    assert terminal["_continuation"]["attempt"] == 2
    assert terminal["_continuation"]["sequence"] > first_marker["sequence"]
    assert terminal["summary"]["new_approval_id"] == outcome.new_approval_id
    assert len(
        [row for row in store.governance if row["agent"] == "response_agent"]
    ) == 1
    assert len(
        [row for row in store.handoffs if row["from_agent"] == "response_agent"]
    ) == 1


def test_completed_replay_reconstructs_exact_marked_response_and_approval_refund_without_writes() -> None:
    store = DatabaseStore()
    approval_id = str(store.approval["approval_id"])
    claim_token = _approval_continuation_claim_token("demo07", approval_id, 1)
    marker = {
        "type": "human_approval",
        "approval_id": approval_id,
        "claim_token": claim_token,
        "attempt": 1,
        "sequence": 2,
    }
    store.approval.update(
        {
            "status": "approved",
            "decision": "partial_refund",
            "resolved_amount": Decimal("199.99"),
            "reviewer": "manager@example.com",
            "notes": "Approve the remaining refundable balance.",
            "resolved_at": "now",
        }
    )
    store.workflow.update({"status": "completed", "current_agent": "completed"})
    store.handoffs.extend(
        [
            {
                "handoff_id": "response-z-initial",
                "from_agent": "response_agent",
                "to_agent": "end",
                "input_json": "{}",
                "output_json": json.dumps(
                    {
                        "response_result": {
                            "final_outcome": "manual_review",
                            "workflow_status": "waiting_human",
                            "response": {"body": "A specialist will review this request."},
                        }
                    }
                ),
                "created_at": "2026-08-14 12:00:00",
            },
            {
                "handoff_id": "refund-final",
                "from_agent": "refund_agent",
                "to_agent": "response_agent",
                "input_json": json.dumps({"_continuation": marker}),
                "output_json": json.dumps(
                    {
                        "_continuation": marker,
                        "refund_result": {
                            "status": "success",
                            "refund_id": "RF-ticket-demo07",
                            "amount": 199.99,
                            "currency": "USD",
                        },
                    }
                ),
                "created_at": "2026-08-14 12:00:00",
            },
            {
                "handoff_id": "response-a-final",
                "from_agent": "response_agent",
                "to_agent": "end",
                "input_json": json.dumps({"_continuation": marker}),
                "output_json": json.dumps(
                    {
                        "_continuation": marker,
                        "response_result": {
                            "final_outcome": "partial_refund",
                            "workflow_status": "completed",
                            "response": {"body": "Your partial refund was issued."},
                        },
                    }
                ),
                "created_at": "2026-08-14 12:00:00",
            },
        ]
    )
    store.refunds.append(
        {
            "transaction_id": "refund-demo07",
            "approval_id": approval_id,
            "amount": Decimal("199.99"),
            "currency": "USD",
            "status": "issued",
            "external_ref": "RF-ticket-demo07",
            "created_at": "2026-08-14 12:00:00",
            "updated_at": "2026-08-14 12:00:00",
        }
    )
    store.audit.extend(
        [
            {
                "log_id": 2,
                "event_type": "human_approval_continuation_claimed",
                "payload_json": json.dumps(
                    {
                        "approval_id": approval_id,
                        "next_agent": "refund_agent",
                        "attempt": 1,
                        "claim_token": claim_token,
                    }
                ),
            },
            {
                "log_id": 3,
                "event_type": "human_approval_continued",
                "payload_json": json.dumps(
                    {
                        "approval_id": approval_id,
                        "workflow_status": "completed",
                        "current_agent": "completed",
                        "summary": {"final_outcome": "partial_refund"},
                        "_continuation": marker,
                    }
                ),
            },
        ]
    )
    store.next_log_id = 4
    repository = _database_repository(store, [])
    counts_before = (
        len(store.handoffs),
        len(store.audit),
        len(store.refunds),
        len(store.approval_handoffs),
    )

    outcome = HumanApprovalService(
        repository,
        refund_runner=lambda _state: pytest.fail("completed replay must not refund"),
        response_runner=lambda _state: pytest.fail("completed replay must not respond"),
    ).resolve(
        "demo07",
        decision="partial_refund",
        resolved_amount=Decimal("199.99"),
        reviewer="manager@example.com",
        notes="Approve the remaining refundable balance.",
        approval_id=approval_id,
    )

    assert outcome.continuation_status == "already_completed"
    assert outcome.refund_result == {
        "status": "success",
        "refund_id": "RF-ticket-demo07",
        "amount": 199.99,
        "currency": "USD",
    }
    assert outcome.response_result["response"]["body"] == "Your partial refund was issued."
    assert counts_before == (
        len(store.handoffs),
        len(store.audit),
        len(store.refunds),
        len(store.approval_handoffs),
    )


def test_completed_replay_scopes_policy_response_and_terminal_state_to_selected_approval() -> None:
    store = DatabaseStore()
    approval1_id = str(store.approval["approval_id"])
    approval2_id = "approval-demo07-response-2"
    marker1 = {
        "type": "human_approval",
        "approval_id": approval1_id,
        "claim_token": _approval_continuation_claim_token(
            "demo07", approval1_id, 1
        ),
        "attempt": 1,
        "sequence": 10,
    }
    marker2 = {
        "type": "human_approval",
        "approval_id": approval2_id,
        "claim_token": _approval_continuation_claim_token(
            "demo07", approval2_id, 1
        ),
        "attempt": 1,
        "sequence": 20,
    }
    store.approval.update(
        {
            "status": "approved",
            "decision": "approve",
            "resolved_amount": None,
            "reviewer": "reviewer-one@example.com",
            "notes": "Release the first reviewed response.",
            "resolved_at": "2026-08-14 12:00:00",
            "approved_next_agent": "response_agent",
        }
    )
    approval2 = {
        **deepcopy(store.approval),
        "approval_id": approval2_id,
        "triggering_event_id": "response-governance-marker-1",
        "triggering_event_type": "governance",
        "governance_agent": "response_agent",
        "governance_flags": json.dumps({"_continuation": marker1}),
        "policy_review_evidence": None,
        "reason": "Review the blocked response.",
        "decision": "approve",
        "reviewer": "reviewer-two@example.com",
        "notes": "Release the corrected response.",
        "approved_next_agent": "end",
    }
    store.approvals.append(approval2)
    same_second = "2026-08-14 12:00:00"
    store.handoffs.extend(
        [
            {
                "handoff_id": "z-policy-approval-1",
                "from_agent": "policy_agent",
                "to_agent": "response_agent",
                "input_json": json.dumps({"_continuation": marker1}),
                "output_json": json.dumps(
                    {
                        "_continuation": marker1,
                        "policy_decision": {
                            "decision": "deny",
                            "reason": "approval-one-policy",
                        },
                    }
                ),
                "created_at": same_second,
            },
            {
                "handoff_id": "z-response-approval-1",
                "from_agent": "response_agent",
                "to_agent": "human_approval",
                "input_json": json.dumps({"_continuation": marker1}),
                "output_json": json.dumps(
                    {
                        "_continuation": marker1,
                        "response_handoff": "human_review",
                        "response_result": {
                            "workflow_status": "waiting_human",
                            "response": {"body": "Approval one blocked response."},
                        },
                    }
                ),
                "created_at": same_second,
            },
            {
                "handoff_id": "a-policy-approval-2",
                "from_agent": "policy_agent",
                "to_agent": "response_agent",
                "input_json": json.dumps({"_continuation": marker2}),
                "output_json": json.dumps(
                    {
                        "_continuation": marker2,
                        "policy_decision": {
                            "decision": "approve",
                            "reason": "approval-two-policy",
                        },
                    }
                ),
                "created_at": same_second,
            },
            {
                "handoff_id": "a-refund-approval-2",
                "from_agent": "refund_agent",
                "to_agent": "response_agent",
                "input_json": json.dumps({"_continuation": marker2}),
                "output_json": json.dumps(
                    {
                        "_continuation": marker2,
                        "refund_result": {
                            "status": "success",
                            "refund_id": "RF-approval-2",
                            "amount": 25.0,
                            "currency": "USD",
                        },
                    }
                ),
                "created_at": same_second,
            },
            {
                "handoff_id": "a-response-approval-2",
                "from_agent": "response_agent",
                "to_agent": "end",
                "input_json": json.dumps({"_continuation": marker2}),
                "output_json": json.dumps(
                    {
                        "_continuation": marker2,
                        "response_handoff": "end",
                        "response_result": {
                            "final_outcome": "approved",
                            "workflow_status": "completed",
                            "response": {"body": "Approval two final response."},
                        },
                    }
                ),
                "created_at": same_second,
            },
        ]
    )
    store.audit.extend(
        [
            {
                "log_id": 30,
                "event_type": "human_approval_continued",
                "payload_json": json.dumps(
                    {
                        "approval_id": approval1_id,
                        "workflow_status": "pending_human",
                        "current_agent": "human_approval",
                        "summary": {"new_approval_id": approval2_id},
                        "_continuation": marker1,
                    }
                ),
            },
            {
                "log_id": 31,
                "event_type": "human_approval_continued",
                "payload_json": json.dumps(
                    {
                        "approval_id": approval2_id,
                        "workflow_status": "completed",
                        "current_agent": "completed",
                        "summary": {"final_outcome": "approved"},
                        "_continuation": marker2,
                    }
                ),
            },
        ]
    )
    store.next_log_id = 32
    store.workflow = {"status": "completed", "current_agent": "completed"}
    repository = _database_repository(store, [])
    counts_before = (len(store.handoffs), len(store.audit), len(store.refunds))

    replay1 = repository.resolve_human_approval(
        trace_id="demo07",
        approval_id=approval1_id,
        decision="approve",
        resolved_amount=None,
        reviewer="reviewer-one@example.com",
        notes="Release the first reviewed response.",
    )
    replay2 = repository.resolve_human_approval(
        trace_id="demo07",
        approval_id=approval2_id,
        decision="approve",
        resolved_amount=None,
        reviewer="reviewer-two@example.com",
        notes="Release the corrected response.",
    )

    assert replay1.state["policy_decision"]["reason"] == "approval-one-policy"
    assert replay1.state["response_result"]["response"]["body"] == (
        "Approval one blocked response."
    )
    assert replay1.state["workflow_status"] == "pending_human"
    assert replay1.state["refund_result"] == {}
    assert replay1.state["human_approval_continuation_summary"] == {
        "new_approval_id": approval2_id
    }
    assert replay2.state["policy_decision"]["reason"] == "approval-two-policy"
    assert replay2.state["refund_result"]["refund_id"] == "RF-approval-2"
    assert replay2.state["response_result"]["response"]["body"] == (
        "Approval two final response."
    )
    assert replay2.state["workflow_status"] == "completed"
    outcome1 = HumanApprovalService(
        repository,
        refund_runner=lambda _state: pytest.fail("completed replay must not refund"),
        response_runner=lambda _state: pytest.fail("completed replay must not respond"),
    ).resolve(
        "demo07",
        approval_id=approval1_id,
        decision="approve",
        resolved_amount=None,
        reviewer="reviewer-one@example.com",
        notes="Release the first reviewed response.",
    )
    assert outcome1.continuation_status == "already_completed"
    assert outcome1.workflow_status == "pending_human"
    assert outcome1.new_approval_id == approval2_id
    assert outcome1.response_result["response"]["body"] == (
        "Approval one blocked response."
    )
    assert counts_before == (len(store.handoffs), len(store.audit), len(store.refunds))


def test_public_approval_writers_append_response_audit_and_governance_without_touching_waiting_history() -> None:
    store = DatabaseStore()
    initial_response = {
        "handoff_id": "response-initial",
        "from_agent": "response_agent",
        "to_agent": "end",
        "input_json": json.dumps({"human_review": {"status": "pending"}}),
        "output_json": json.dumps(
            {
                "response_result": {
                    "final_outcome": "manual_review",
                    "workflow_status": "waiting_human",
                }
            }
        ),
        "created_at": "2026-08-14 12:00:00",
    }
    initial_audit = {
        "log_id": 100,
        "event_type": "response_agent_evaluated",
        "payload_json": json.dumps({"cycle": "waiting_human"}),
    }
    initial_governance = {
        "event_id": "governance-response-initial",
        "trace_id": "demo07",
        "agent": "response_agent",
        "owasp_category": "ASI00",
        "trigger_score": None,
        "interceptor_action": "allow",
        "flags_json": json.dumps({"summary": "waiting response allowed"}),
        "offending_content": None,
    }
    store.handoffs.append(initial_response)
    store.audit.append(initial_audit)
    store.governance.append(initial_governance)
    store.next_log_id = 101
    repository = _database_repository(store, [])
    resolution = repository.resolve_human_approval(
        trace_id="demo07",
        decision="deny",
        resolved_amount=None,
        reviewer="manager@example.com",
        notes="The evidence does not support the remaining refund.",
    )
    preserved = (
        deepcopy(initial_response),
        deepcopy(initial_audit),
        deepcopy(initial_governance),
    )

    repository.save_governance_event_record(
        GovernanceStatement(
            trace_id="demo07",
            agent="response_agent",
            stage="response_governance",
            status="allow",
            summary="Final response allowed.",
        ),
        approval_claim_token=resolution.continuation_claim_token,
    )
    repository.persist_agent_handoff(
        trace_id="demo07",
        ticket_id="ticket-demo07",
        from_agent="response_agent",
        to_agent="end",
        input_payload={"human_review": {}},
        output_payload={
            "response_result": {
                "final_outcome": "denied",
                "workflow_status": "completed",
            },
            "response_handoff": "end",
        },
        audit_event_type="response_agent_evaluated",
        workflow_status="completed",
        current_agent="completed",
        approval_claim_token=resolution.continuation_claim_token,
    )

    assert store.handoffs[-2] == preserved[0]
    assert next(row for row in store.audit if row["log_id"] == 100) == preserved[1]
    assert store.governance[0] == preserved[2]
    final_handoff = store.handoffs[-1]
    input_marker = json.loads(final_handoff["input_json"])["_continuation"]
    output_marker = json.loads(final_handoff["output_json"])["_continuation"]
    assert input_marker == output_marker
    assert input_marker == {
        "type": "human_approval",
        "approval_id": resolution.approval_id,
        "claim_token": resolution.continuation_claim_token,
        "attempt": resolution.continuation_attempt,
        "sequence": resolution.continuation_sequence,
    }
    final_audit = store.audit[-1]
    assert final_audit["event_type"] == "response_agent_evaluated"
    assert json.loads(final_audit["payload_json"])["_continuation"] == input_marker
    assert json.loads(store.governance[-1]["flags_json"])["_continuation"] == input_marker
    assert len(store.handoffs) == 3
    assert len([row for row in store.audit if row["event_type"] == "response_agent_evaluated"]) == 2
    assert len(store.governance) == 2

    assert repository.mark_human_approval_continuation(
        trace_id="demo07",
        approval_id=resolution.approval_id,
        workflow_status="completed",
        current_agent="completed",
        summary={"final_outcome": "denied"},
        approval_claim_token=resolution.continuation_claim_token or "",
    ) is True
    counts = (len(store.handoffs), len(store.audit), len(store.governance))
    replay = repository.resolve_human_approval(
        trace_id="demo07",
        approval_id=resolution.approval_id,
        decision="deny",
        resolved_amount=None,
        reviewer="manager@example.com",
        notes="The evidence does not support the remaining refund.",
    )
    assert replay.continuation_complete is True
    assert counts == (len(store.handoffs), len(store.audit), len(store.governance))


@pytest.mark.parametrize("trace_id", ["demo00", "demo21", "TRACE-POL-001", "demo01-extra"])
def test_repository_rejects_every_trace_outside_exact_demo_allowlist(trace_id: str) -> None:
    repository = GCPRepository({"database": "final"})
    with pytest.raises(ValueError, match="demo01 through demo20"):
        repository.resolve_human_approval(
            trace_id=trace_id,
            decision="deny",
            resolved_amount=None,
            reviewer="manager@example.com",
            notes="Out of scope.",
        )


@pytest.mark.parametrize(
    ("decision", "amount", "reviewer", "notes", "message"),
    [
        ("maybe", None, "manager", "Reviewed", "decision must be"),
        ("deny", None, "", "Reviewed", "reviewer is required"),
        ("deny", None, "manager", "", "notes are required"),
        ("approve", 1.001, "manager", "Reviewed", "two decimal places"),
        ("approve", 1e28, "manager", "Reviewed", "supported monetary range"),
        ("approve", 1e100, "manager", "Reviewed", "supported monetary range"),
    ],
)
def test_repository_validates_resolution_fields_before_connecting(
    decision: str,
    amount: float | None,
    reviewer: str,
    notes: str,
    message: str,
) -> None:
    repository = GCPRepository({"database": "final"})
    with pytest.raises(ValueError, match=message):
        repository.resolve_human_approval(
            trace_id="demo01",
            decision=decision,
            resolved_amount=amount,
            reviewer=reviewer,
            notes=notes,
        )


class PolicyApprovalCursor:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount = 1

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self) -> list[tuple[str, str]]:
        return list(self.rows)


class PolicyApprovalConnection:
    def __init__(self, cursor: PolicyApprovalCursor) -> None:
        self.value = cursor
        self.committed = False
        self.rolled_back = False

    def start_transaction(self) -> None:
        return None

    def cursor(self, **_kwargs: Any) -> PolicyApprovalCursor:
        return self.value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


def _policy_approval_output(next_agent: str = "human_approval") -> Any:
    return SimpleNamespace(
        case=SimpleNamespace(trace_id="demo07"),
        handoff=SimpleNamespace(next_agent=next_agent, reason="Manager review required."),
        decision=SimpleNamespace(type="manual_review", reason="Evidence conflicts."),
        customer_request=SimpleNamespace(requested_amount=199.99),
        response_guidance=SimpleNamespace(customer_safe_summary="A manager will review this."),
        governance=SimpleNamespace(flags=[]),
    )


def test_policy_persistence_rejects_resolved_history_before_rewriting_dependencies() -> None:
    cursor = PolicyApprovalCursor([("POL-APP-existing", "approved")])
    connection = PolicyApprovalConnection(cursor)
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: connection  # type: ignore[method-assign]

    with pytest.raises(HumanApprovalConflictError, match="resolved Policy approval history"):
        repository.persist_result(
            SimpleNamespace(case=SimpleNamespace(trace_id="demo07")),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            [],
            SimpleNamespace(),
        )

    assert connection.rolled_back and not connection.committed
    assert len(cursor.executed) == 1
    assert cursor.executed[0][0].startswith("SELECT approval_id, status")


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_policy_rerun_never_resets_or_deletes_resolved_policy_approval(status: str) -> None:
    cursor = PolicyApprovalCursor([("POL-APP-existing", status)])
    repository = GCPRepository({"database": "final"})

    with pytest.raises(HumanApprovalConflictError, match="resolved Policy approval history"):
        repository._persist_human_approval(  # type: ignore[attr-defined]
            cursor,
            _policy_approval_output(next_agent="response_agent"),
            None,
            [],
        )

    mutation_sql = [
        sql for sql, _params in cursor.executed
        if sql.startswith("DELETE") or sql.startswith("INSERT") or sql.startswith("UPDATE")
    ]
    assert mutation_sql == []


def test_pending_policy_approval_refresh_clears_every_resolution_field() -> None:
    cursor = PolicyApprovalCursor([("POL-APP-existing", "pending")])
    repository = GCPRepository({"database": "final"})

    repository._persist_human_approval(  # type: ignore[attr-defined]
        cursor,
        _policy_approval_output(),
        "POL-REV-demo07",
        [],
    )

    insert_sql, params = next(
        (sql, params) for sql, params in cursor.executed
        if sql.startswith("INSERT INTO human_approvals")
    )
    assert params[0] == "POL-APP-existing"
    assert "decision = NULL" in insert_sql
    assert "resolved_amount = NULL" in insert_sql
    assert "reviewer = NULL" in insert_sql
    assert "resolved_at = NULL" in insert_sql


def test_non_review_policy_rerun_deletes_only_pending_policy_approval() -> None:
    cursor = PolicyApprovalCursor([("POL-APP-existing", "pending")])
    repository = GCPRepository({"database": "final"})

    repository._persist_human_approval(  # type: ignore[attr-defined]
        cursor,
        _policy_approval_output(next_agent="response_agent"),
        None,
        [],
    )

    delete_sql = next(sql for sql, _params in cursor.executed if sql.startswith("DELETE"))
    assert "status = 'pending'" in delete_sql


class EnsureApprovalCursor:
    def __init__(self, existing_approval_id: str | None = None) -> None:
        self.rows: list[Any] = []
        self.rowcount = 1
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.insert_params: tuple[Any, ...] | None = None
        self.existing_approval_id = existing_approval_id
        self.events = {
            "triage_agent": "governance-triage",
            "policy_agent": "governance-policy",
            "response_agent": "governance-response",
        }

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        compact = " ".join(sql.split())
        self.executed.append((compact, params))
        self.rowcount = 1
        if compact.startswith("SELECT approval_id FROM human_approvals"):
            self.rows = (
                [(self.existing_approval_id,)] if self.existing_approval_id else []
            )
        elif compact.startswith("SELECT event_id, flags_json FROM governance_events"):
            self.rows = [(self.events[params[1]], "{}")]
        elif compact.startswith("SELECT tickets.requested_amount"):
            self.rows = [(Decimal("49.99"),)]
        elif compact.startswith("SELECT COUNT(*) FROM human_approvals"):
            self.rows = [(0,)]
        elif compact.startswith("INSERT INTO human_approvals"):
            self.insert_params = params
            self.rows = []
        elif compact.startswith("UPDATE workflow_runs"):
            self.rows = []
        else:
            raise AssertionError(f"Unexpected SQL: {compact}")

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None


class EnsureApprovalConnection:
    def __init__(self, cursor: EnsureApprovalCursor) -> None:
        self.value = cursor
        self.committed = False
        self.rolled_back = False

    def start_transaction(self) -> None:
        return None

    def cursor(self, **_kwargs: Any) -> EnsureApprovalCursor:
        return self.value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


def test_governance_trigger_uses_exact_continuation_marker_not_same_second_uuid_order() -> None:
    selected_marker = {
        "type": "human_approval",
        "approval_id": "approval-demo18",
        "claim_token": "claim-selected",
        "attempt": 2,
        "sequence": 200,
    }
    older_marker = {
        **selected_marker,
        "claim_token": "claim-older",
        "attempt": 1,
        "sequence": 100,
    }

    class TriggerCursor:
        def __init__(self) -> None:
            self.rows: list[Any] = []
            self.query = ""

        def execute(self, sql: str, _params: tuple[Any, ...]) -> None:
            self.query = " ".join(sql.split())
            # Model identical created_at values where descending UUID order puts
            # the older event first.
            self.rows = [
                ("z-older-event", json.dumps({"_continuation": older_marker})),
                ("a-selected-event", json.dumps({"_continuation": selected_marker})),
            ]

        def fetchall(self) -> list[Any]:
            return list(self.rows)

    cursor = TriggerCursor()
    event_id = _select_approval_governance_trigger(
        cursor,
        trace_id="demo18",
        agent="response_agent",
        continuation_marker=selected_marker,
    )

    assert event_id == "a-selected-event"
    assert "ORDER BY created_at DESC, event_id DESC" in cursor.query


@pytest.mark.parametrize(
    ("stage", "expected_agent", "expected_event"),
    [
        ("triage", "triage_agent", "governance-triage"),
        ("response", "response_agent", "governance-response"),
    ],
)
def test_ensure_human_approval_binds_governance_trigger_to_requested_stage(
    stage: str,
    expected_agent: str,
    expected_event: str,
) -> None:
    cursor = EnsureApprovalCursor()
    connection = EnsureApprovalConnection(cursor)
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: connection  # type: ignore[method-assign]

    approval_id = repository.ensure_human_approval(
        trace_id="demo18",
        reason="governance_block",
        stage=stage,
        policy_decision={},
    )

    governance_query, governance_params = next(
        (sql, params) for sql, params in cursor.executed
        if sql.startswith("SELECT event_id, flags_json FROM governance_events")
    )
    assert governance_params == ("demo18", expected_agent)
    assert "ORDER BY created_at DESC, event_id DESC" in governance_query
    assert cursor.insert_params is not None
    assert cursor.insert_params[2:4] == (expected_event, "governance")
    assert approval_id
    assert connection.committed and not connection.rolled_back


def test_ensure_policy_approval_returns_existing_pending_row_before_trigger_lookup() -> None:
    cursor = EnsureApprovalCursor(existing_approval_id="POL-APP-existing")
    connection = EnsureApprovalConnection(cursor)
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: connection  # type: ignore[method-assign]

    approval_id = repository.ensure_human_approval(
        trace_id="demo13",
        reason="manual_review",
        stage="policy",
        policy_decision={"decision": "manual_review"},
    )

    assert approval_id == "POL-APP-existing"
    assert len(cursor.executed) == 1
    assert cursor.executed[0][0].startswith("SELECT approval_id FROM human_approvals")
    assert connection.committed and not connection.rolled_back


class GenericHandoffCursor:
    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.rowcount = 1
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        compact = " ".join(sql.split())
        self.executed.append((compact, params))
        self.rowcount = 1
        if compact.startswith("SELECT handoff_id FROM agent_handoffs"):
            self.rows = []
        else:
            self.rows = []

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None


class GenericHandoffConnection:
    def __init__(self, cursor: GenericHandoffCursor) -> None:
        self.value = cursor
        self.committed = False
        self.rolled_back = False

    def start_transaction(self) -> None:
        return None

    def cursor(self, **_kwargs: Any) -> GenericHandoffCursor:
        return self.value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("workflow_status", "current_agent"),
    [("completed", "completed"), ("running", "response_agent")],
)
def test_generic_handoff_sets_completed_at_only_for_completed_workflow(
    workflow_status: str,
    current_agent: str,
) -> None:
    cursor = GenericHandoffCursor()
    connection = GenericHandoffConnection(cursor)
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: connection  # type: ignore[method-assign]

    repository.persist_agent_handoff(
        trace_id="demo01",
        ticket_id="ticket-demo01",
        from_agent="response_agent",
        to_agent="end",
        input_payload={},
        output_payload={},
        audit_event_type="response_agent_evaluated",
        workflow_status=workflow_status,
        current_agent=current_agent,
    )

    update_sql, update_params = next(
        (sql, params) for sql, params in cursor.executed
        if sql.startswith("UPDATE workflow_runs")
    )
    assert "completed_at = CASE WHEN %s = 'completed' THEN CURRENT_TIMESTAMP ELSE NULL END" in update_sql
    assert update_params == (
        workflow_status,
        current_agent,
        workflow_status,
        "demo01",
    )
    assert connection.committed and not connection.rolled_back


def test_approval_continuation_handoff_uses_distinct_deterministic_id_and_exact_markers() -> None:
    marker = {
        "type": "human_approval",
        "approval_id": "approval-demo07",
        "claim_token": "claim-demo07",
        "attempt": 1,
        "sequence": 42,
    }
    input_payload = {"message": "original input"}
    output_payload = {"response_result": {"workflow_status": "completed"}}
    cursor = GenericHandoffCursor()
    repository = GCPRepository({"database": "final"})

    handoff_id = repository._upsert_generic_handoff(  # type: ignore[attr-defined]
        cursor,
        trace_id="demo07",
        ticket_id="ticket-demo07",
        from_agent="response_agent",
        to_agent="end",
        input_payload=input_payload,
        output_payload=output_payload,
        input_tokens=10,
        output_tokens=4,
        continuation_marker=marker,
    )

    assert handoff_id != repository._next_handoff_id("demo07", "response_agent")
    assert handoff_id == repository._next_handoff_id(  # type: ignore[attr-defined]
        "demo07",
        "response_agent",
        continuation_marker=marker,
    )
    assert input_payload == {"message": "original input"}
    assert output_payload == {"response_result": {"workflow_status": "completed"}}
    assert not any(sql.startswith("SELECT handoff_id") for sql, _ in cursor.executed)
    insert_params = next(
        params for sql, params in cursor.executed
        if sql.startswith("INSERT INTO agent_handoffs")
    )
    assert json.loads(insert_params[5])["_continuation"] == marker
    assert json.loads(insert_params[6])["_continuation"] == marker


class WorkflowFailureStore:
    def __init__(self) -> None:
        self.ticket_id = "ticket-demo10"
        self.audit: list[dict[str, Any]] = []
        self.workflow = {"status": "running", "current_agent": "policy_agent"}
        self.queries: list[str] = []


class WorkflowFailureCursor:
    def __init__(self, store: WorkflowFailureStore) -> None:
        self.store = store
        self.rows: list[Any] = []
        self.rowcount = 1
        self.lastrowid = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        compact = " ".join(sql.split())
        self.store.queries.append(compact)
        self.rowcount = 1
        if compact.startswith("SELECT ticket_id FROM workflow_runs"):
            self.rows = [(self.store.ticket_id,)]
        elif compact.startswith("SELECT log_id FROM audit_log"):
            self.rows = [(row["log_id"],) for row in self.store.audit]
        elif compact.startswith("INSERT INTO audit_log"):
            self.lastrowid = 701
            self.store.audit.append({"log_id": 701, "payload_json": params[1]})
            self.rows = []
        elif compact.startswith("UPDATE audit_log"):
            payload, log_id, _trace_id = params
            row = next(item for item in self.store.audit if item["log_id"] == log_id)
            row["payload_json"] = payload
            self.rows = []
        elif compact.startswith("UPDATE workflow_runs"):
            self.store.workflow = {"status": "failed", "current_agent": "failed"}
            self.rows = []
        else:
            raise AssertionError(f"Unexpected SQL: {compact}")

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None


class WorkflowFailureConnection:
    def __init__(self, store: WorkflowFailureStore) -> None:
        self.value = WorkflowFailureCursor(store)
        self.committed = False
        self.rolled_back = False

    def start_transaction(self) -> None:
        return None

    def cursor(self, **_kwargs: Any) -> WorkflowFailureCursor:
        return self.value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


def test_record_workflow_failure_is_atomic_and_upserts_one_trace_audit() -> None:
    store = WorkflowFailureStore()
    connections: list[WorkflowFailureConnection] = []
    repository = GCPRepository({"database": "final"})

    def connect() -> WorkflowFailureConnection:
        connection = WorkflowFailureConnection(store)
        connections.append(connection)
        return connection

    repository._connect = connect  # type: ignore[method-assign]
    first = repository.record_workflow_failure(
        trace_id="demo10",
        ticket_id="ticket-demo10",
        error_type="TimeoutError",
        error_message="model deadline exceeded",
    )
    second = repository.record_workflow_failure(
        trace_id="demo10",
        ticket_id="ticket-demo10",
        error_type="RuntimeError",
        error_message="retry also failed",
    )

    assert first == second == 701
    assert len(store.audit) == 1
    assert json.loads(store.audit[0]["payload_json"]) == {
        "ticket_id": "ticket-demo10",
        "error_type": "RuntimeError",
        "error_message": "retry also failed",
    }
    assert store.workflow == {"status": "failed", "current_agent": "failed"}
    workflow_update = next(
        query for query in store.queries
        if query.startswith("UPDATE workflow_runs")
    )
    assert "completed_at = CURRENT_TIMESTAMP" in workflow_update
    assert all(connection.committed and not connection.rolled_back for connection in connections)


def test_record_workflow_failure_rolls_back_on_ticket_mismatch() -> None:
    store = WorkflowFailureStore()
    connection = WorkflowFailureConnection(store)
    repository = GCPRepository({"database": "final"})
    repository._connect = lambda: connection  # type: ignore[method-assign]

    with pytest.raises(CloudDatabaseError, match="ticket_id does not match"):
        repository.record_workflow_failure(
            trace_id="demo10",
            ticket_id="ticket-demo11",
            error_type="TimeoutError",
            error_message="model deadline exceeded",
        )

    assert connection.rolled_back and not connection.committed
    assert store.audit == []
    assert store.workflow == {"status": "running", "current_agent": "policy_agent"}
