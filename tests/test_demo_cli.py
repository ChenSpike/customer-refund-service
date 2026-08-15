from __future__ import annotations

import importlib
import os

import pytest


def test_importing_main_has_no_cloud_or_database_side_effect(monkeypatch) -> None:
    import main

    module = importlib.reload(main)
    assert callable(module.main)


def test_cli_offline_run_all_matches_twenty(capsys) -> None:
    import main

    assert main.main(["--json", "run-all"]) == 0
    output = capsys.readouterr().out
    assert '"requested": 20' in output
    assert '"matched_expectations": 20' in output


def test_cli_accepts_bounded_parallel_workers(capsys) -> None:
    import main

    assert main.main(["--workers", "2", "--json", "run-all"]) == 0
    output = capsys.readouterr().out
    assert '"workers": 2' in output


def test_cli_can_write_durable_json_report(tmp_path, capsys) -> None:
    import json
    import main

    report = tmp_path / "reports" / "offline.json"
    assert main.main(["--output", str(report), "run", "demo01"]) == 0

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["case_id"] == "demo01"
    assert payload["matched_expectations"] is True
    assert "demo01" in capsys.readouterr().out


def test_cli_live_requires_exact_confirmation_before_runner_creation(monkeypatch) -> None:
    import main

    created: list[object] = []
    monkeypatch.setattr(main, "DemoRunner", lambda *args, **kwargs: created.append((args, kwargs)))

    with pytest.raises(SystemExit) as error:
        main.main(["--mode", "live", "run", "demo01"])

    assert error.value.code == 2
    assert created == []


def test_cli_no_arguments_is_help_only(capsys, monkeypatch) -> None:
    import main

    monkeypatch.delenv("MYSQL_DATABASE", raising=False)
    assert main.main([]) == 0
    assert "run-all" in capsys.readouterr().out
    assert "MYSQL_DATABASE" not in os.environ
