from __future__ import annotations

import pytest

from policy_agent.cloud_db import (
    MIGRATION_002_PATH,
    CloudDatabaseError,
    _approval_trigger,
    _migration_statements,
)


def test_human_approval_uses_exactly_one_typed_trigger() -> None:
    assert _approval_trigger("TRACE-1", "POL-REV-001", []) == (
        "policy_review",
        "POL-REV-001",
    )
    assert _approval_trigger("TRACE-1", None, ["POL-GOV-001"]) == (
        "governance",
        "POL-GOV-001",
    )


def test_governance_trigger_has_precedence_when_both_events_exist() -> None:
    assert _approval_trigger("TRACE-1", "POL-REV-001", ["POL-GOV-001"]) == (
        "governance",
        "POL-GOV-001",
    )


def test_human_approval_without_an_event_fails_closed() -> None:
    with pytest.raises(CloudDatabaseError, match="requires a governance or policy review event"):
        _approval_trigger("TRACE-1", None, [])


def test_unified_trigger_migration_has_four_ordered_steps() -> None:
    statements = _migration_statements(MIGRATION_002_PATH, expected=4)

    assert "ADD COLUMN triggering_event_type" in statements[0]
    assert "DROP FOREIGN KEY fk_human_approvals_event" in statements[1]
    assert "triggering_event_id = COALESCE" in statements[2]
    assert "DROP COLUMN policy_review_event_id" in statements[3]
