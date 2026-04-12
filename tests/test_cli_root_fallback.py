from types import SimpleNamespace
import importlib

import pytest
from typer.testing import CliRunner

import litehive.cli as legacy_cli
from tests.workspace_helpers import ensure_workspace

legacy_typer_app = legacy_cli.app
modern_cli = importlib.import_module("litehive.cli.app")
legacy_cli.app = legacy_typer_app


@pytest.mark.parametrize(
        "app",
        [
            legacy_typer_app,
            modern_cli.app,
        ],
)
def test_bare_litehive_prints_status_when_idle(tmp_path, monkeypatch, app) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(modern_cli, "_run_next_task", lambda root: None)

    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0, result.output
    assert "=== Active Task ===" in result.output
    assert "=== Queue ===" in result.output


@pytest.mark.parametrize(
        "app",
        [
            legacy_typer_app,
            modern_cli.app,
        ],
)
def test_bare_litehive_runs_next_task_when_available(monkeypatch, app) -> None:
    result_payload = SimpleNamespace(
        task=SimpleNamespace(id="T-0007"),
        final_stage="accepting",
    )
    monkeypatch.setattr(modern_cli, "_run_next_task", lambda root: result_payload)

    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0, result.output
    assert result.output == "T-0007: accepting\n"


@pytest.mark.parametrize("app", [legacy_typer_app, modern_cli.app])
def test_tasks_command_is_removed_from_typer_apps(app) -> None:
    result = CliRunner().invoke(app, ["tasks"])

    assert result.exit_code == 2
    assert "No such command 'tasks'" in result.output


@pytest.mark.parametrize("app", [legacy_typer_app, modern_cli.app])
def test_web_command_is_removed_from_typer_apps(app) -> None:
    result = CliRunner().invoke(app, ["web"])

    assert result.exit_code == 2
    assert "No such command 'web'" in result.output
