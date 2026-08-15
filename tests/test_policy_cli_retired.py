from agents.policy import cli


def test_policy_only_cli_is_retired_and_has_no_live_commands(capsys) -> None:
    parser = cli.build_parser()

    assert "main_db" not in parser.description
    assert cli.main([]) == 2
    output = capsys.readouterr().out
    assert "standalone Policy-only CLI is retired" in output
    assert "refund HTTP" in output
    assert "API for full-stack execution" in output
