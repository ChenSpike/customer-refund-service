import pytest

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


def test_policy_mapper_requires_handoff() -> None:
    with pytest.raises(KeyError):
        map_policy_handoff_to_parent_node({})