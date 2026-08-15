import pytest

from agents.policy.routing import route_policy
from app.mappers.policy_mapper import map_policy_handoff_to_parent_node


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (
            {
                "policy_handoff": "refund",
                "policy_persistence_result": {"next_agent": "refund_agent"},
            },
            "refund_agent",
        ),
        (
            {
                "policy_handoff": "response",
                "policy_persistence_result": {"next_agent": "response_agent"},
            },
            "response_agent",
        ),
        (
            {
                "policy_handoff": "human_review",
                "policy_persistence_result": {"next_agent": "human_approval"},
            },
            "human_approval",
        ),
    ],
)
def test_route_after_policy_uses_explicit_handoff(state, expected):
    assert map_policy_handoff_to_parent_node(state) == expected


@pytest.mark.parametrize(
    ("decision", "status", "expected"),
    [
        ("approve", "allow", "refund"),
        ("partial_refund", "allow", "refund"),
        ("deny", "allow", "response"),
        ("request_info", "allow", "response"),
        ("manual_review", "allow", "human_review"),
        ("approve", "block", "human_review"),
        ("partial_refund", "block", "human_review"),
        ("deny", "block", "human_review"),
        ("request_info", "block", "human_review"),
        ("manual_review", "block", "human_review"),
    ],
)
def test_policy_routing_returns_handoff(decision, status, expected):
    assert route_policy(decision, status) == expected


def test_policy_mapper_requires_handoff() -> None:
    with pytest.raises(KeyError):
        map_policy_handoff_to_parent_node({})


def test_policy_mapper_requires_persistence_before_routing() -> None:
    with pytest.raises(ValueError, match="policy_persistence_result is required"):
        map_policy_handoff_to_parent_node({"policy_handoff": "refund"})


def test_policy_mapper_rejects_route_that_differs_from_persistence() -> None:
    with pytest.raises(ValueError, match="persisted Policy route disagrees"):
        map_policy_handoff_to_parent_node(
            {
                "policy_handoff": "refund",
                "policy_persistence_result": {"next_agent": "response_agent"},
            }
        )
