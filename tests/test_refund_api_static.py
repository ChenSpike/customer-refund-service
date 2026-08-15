from __future__ import annotations

from pathlib import Path


STATIC_UI = Path(__file__).resolve().parents[1] / "refund_app" / "static" / "index.html"


def test_static_ui_loads_exact_case_catalog_and_posts_only_case_id() -> None:
    source = STATIC_UI.read_text(encoding="utf-8")

    assert 'fetch("/api/cases")' in source
    assert "Array.from({ length: 20 }" in source
    assert 'padStart(2, "0")' in source
    assert 'fetch("/api/refund"' in source
    assert "JSON.stringify({ case_id: caseId })" in source
    assert "expected-route" in source
    assert "actual-route" in source
    assert "matched_expectations" in source
    assert 'id="run-all"' in source
    assert "20-case HTTP run" in source
    assert "execution_boundary?.entrypoint" in source
    assert "OFFLINE SIMULATION" in source
    assert "?trace=" in source
    assert 'id="send-followup"' in source
    assert "/follow-up`" in source
    assert '"refund_followup_http_api"' in source
    assert "Continue customer → Triage" in source
    assert "healthState?.case_states?.[item.case_id]" in source
    assert "even after a page reload" in source
    assert 'persisted?.followup_status === "retryable"' in source
    assert "Retry/recover follow-up" in source
    assert "Recovery unlocks automatically" in source


def test_static_ui_removes_legacy_cases_and_uses_safe_text_rendering() -> None:
    source = STATIC_UI.read_text(encoding="utf-8")

    for legacy_id in ("ORD-001", "ORD-777", "ORD-LEAK"):
        assert legacy_id not in source
    assert "innerHTML" not in source
    assert ".textContent" in source
