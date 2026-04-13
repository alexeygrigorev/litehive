from types import SimpleNamespace

from typer.testing import CliRunner

from litehive.cli import app as modern_cli
from tests.workspace_helpers import ensure_workspace


def test_bare_litehive_prints_status_when_idle(tmp_path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(modern_cli, "_run_next_task", lambda root: None)

    result = CliRunner().invoke(modern_cli.app, [])

    assert result.exit_code == 0, result.output
    assert "=== Active Task ===" in result.output
    assert "=== Queue ===" in result.output


def test_bare_litehive_runs_next_task_when_available(monkeypatch) -> None:
    result_payload = SimpleNamespace(
        task=SimpleNamespace(id="T-0007"),
        final_stage="accepting",
    )
    monkeypatch.setattr(modern_cli, "_run_next_task", lambda root: result_payload)

    result = CliRunner().invoke(modern_cli.app, [])

    assert result.exit_code == 0, result.output
    assert result.output == "T-0007: accepting\n"


def test_tasks_command_is_removed_from_typer_apps() -> None:
    result = CliRunner().invoke(modern_cli.app, ["tasks"])

    assert result.exit_code == 2
    assert "No such command 'tasks'" in result.output


def test_web_command_is_removed_from_typer_apps() -> None:
    result = CliRunner().invoke(modern_cli.app, ["web"])

    assert result.exit_code == 2
    assert "No such command 'web'" in result.output
