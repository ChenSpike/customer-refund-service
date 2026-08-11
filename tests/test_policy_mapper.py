import pytest

from agents.policy.routing import route_policy
from app.mappers.policy_mapper import map_policy_handoff_to_parent_node


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"policy_handoff": "refund"}, "refund_agent"),
        ({"policy_handoff": "response"}, "response_agent"),
        ({"policy_handoff": "human_review"}, "human_approval"),
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
    ],
)
def test_policy_routing_returns_handoff(decision, status, expected):
    assert route_policy(decision, status) == expected


def test_policy_mapper_requires_handoff() -> None:
    with pytest.raises(KeyError):
        map_policy_handoff_to_parent_node({})