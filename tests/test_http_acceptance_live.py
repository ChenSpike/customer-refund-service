"""Explicitly gated live TCP acceptance; never runs in the normal test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from demo.http_acceptance import HttpAcceptanceConfig, run_http_acceptance


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_HTTP_ACCEPTANCE_LIVE") != "final",
        reason=(
            "Start both live services on a clean final baseline and set "
            "RUN_HTTP_ACCEPTANCE_LIVE=final to authorize the 20-case HTTP run."
        ),
    ),
]


def test_live_services_pass_strict_twenty_case_http_acceptance(tmp_path: Path) -> None:
    config = HttpAcceptanceConfig(
        refund_base_url=os.getenv("REFUND_ACCEPTANCE_URL", "http://127.0.0.1:8077"),
        dashboard_base_url=os.getenv("DASHBOARD_ACCEPTANCE_URL", "http://127.0.0.1:8000"),
        output_path=tmp_path / "final-http-e2e.json",
    )

    report = run_http_acceptance(config)

    assert report["transport"] == "tcp"
    assert report["status"] == "passed"
    assert report["summary"]["matched_expectations"] == 20
    assert report["summary"]["dashboard_observed"] == 20
