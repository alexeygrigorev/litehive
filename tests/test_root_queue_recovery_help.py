"""Regression tests for public queue/recovery root commands."""

from typer.testing import CliRunner

from litehive.cli import app


def test_root_help_lists_queue_recovery_shortcuts() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "recover" in result.output
    assert "switch" in result.output
    assert "prioritize" in result.output


def test_command_help_describes_operator_scenarios() -> None:
    runner = CliRunner()

    recover_help = runner.invoke(app, ["recover", "--help"])
    assert recover_help.exit_code == 0, recover_help.output
    assert "accepted task needs another pass" in recover_help.output

    switch_help = runner.invoke(app, ["switch", "--help"])
    assert switch_help.exit_code == 0, switch_help.output
    assert "different engine on its next queued run" in switch_help.output

    prioritize_help = runner.invoke(app, ["prioritize", "--help"])
    assert prioritize_help.exit_code == 0, prioritize_help.output
    assert "operator ordering matters more than" in prioritize_help.output
    assert "the current queue" in prioritize_help.output
