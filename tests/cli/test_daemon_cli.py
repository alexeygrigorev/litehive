import importlib
from pathlib import Path
import sys

from litehive.config.workspace import create_workspace

cli_app = importlib.import_module("litehive.cli.app")


def _isolate_workspace_resolution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)


def test_daemon_status_uses_workspace_cwd(tmp_path: Path, monkeypatch, capsys) -> None:
    _isolate_workspace_resolution(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    create_workspace(workspace)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(sys, "argv", ["litehive", "daemon", "status"])

    exit_code = cli_app.main()
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"workspace: {workspace.resolve()}" in output
    assert "daemon_status: stopped" in output


def test_daemon_status_accepts_explicit_workspace(tmp_path: Path, monkeypatch, capsys) -> None:
    _isolate_workspace_resolution(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    create_workspace(workspace)
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(sys, "argv", ["litehive", "daemon", "status", "--workspace", str(workspace)])

    exit_code = cli_app.main()
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"workspace: {workspace.resolve()}" in output
    assert "daemon_status: stopped" in output


def test_daemon_status_without_inferred_workspace_errors_clearly(tmp_path: Path, monkeypatch, capsys) -> None:
    _isolate_workspace_resolution(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(sys, "argv", ["litehive", "daemon", "status"])

    exit_code = cli_app.main()
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "daemon status failed: unable to load workspace from cwd" in output
    assert not (outside / ".litehive").exists()
