from pathlib import Path
import subprocess

from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.paths import workspace_path
from litehive.config.workspace import ensure_workspace
from litehive.recovery.execution_recovery import recover_stale_runner_state
from litehive.recovery.workspace_repair import repair_workspace_state
from litehive.state.records import create_task


def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
    del args, kwargs
    raise AssertionError("clean repair should not scan task records")


def _write_cache_tool(cache_target: Path) -> None:
    cache_target.parent.mkdir(parents=True, exist_ok=True)
    cache_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cache_target.chmod(0o755)


def _create_broken_venv_binary(checkout_root: Path, binary_name: str, cache_root: Path) -> None:
    bin_dir = checkout_root / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cache_target = cache_root / f"{binary_name}-tool"
    _write_cache_tool(cache_target)
    (bin_dir / binary_name).symlink_to(cache_target)
    cache_target.unlink()


def test_recover_stale_runner_state_skips_task_scan_for_clean_queue(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.setattr("litehive.state.records.list_tasks", _boom)
    assert recover_stale_runner_state(tmp_path) is False


def test_repair_clean_workspace_with_100_tasks_skips_task_scan(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    for index in range(100):
        create_task(tmp_path, title=f"Task {index}")
    monkeypatch.setattr("litehive.state.records.list_tasks", _boom)
    result = CliRunner().invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)
    assert result.return_value == 0
    assert "active_task_id: None" in result.output
    assert "queue_length: 100" in result.output


def test_repair_workspace_state_rebuilds_broken_checkout_venv(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    checkout = workspace_path(tmp_path, "worktrees") / "T-0001-demo"
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )
    _create_broken_venv_binary(checkout, "ruff", tmp_path / "fake-home" / ".cache" / "uv")

    def fake_sync(
        args: list[str], *, cwd: str, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert args == ["uv", "sync", "--extra", "dev"]
        assert capture_output is True and text is True and check is False
        bin_dir = Path(cwd) / ".venv" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        binary = bin_dir / "ruff"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "litehive.recovery.workspace_repair.shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    monkeypatch.setattr("litehive.recovery.workspace_repair.subprocess.run", fake_sync)

    summary = repair_workspace_state(tmp_path, repair_broken_venvs_in_checkouts=True)

    assert summary.mutated is True
    assert summary.broken_venv_binaries == []
    assert (checkout / ".venv" / "bin" / "ruff").exists()
