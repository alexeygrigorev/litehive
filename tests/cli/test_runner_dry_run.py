from types import SimpleNamespace

from typer.testing import CliRunner

from litehive.cli import app as cli_app
from litehive.cli.runner import _dry_run_stop_conditions
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.state.persist import load_state
from litehive.state.records import create_task


def test_dry_run_stop_conditions_apply_cli_overrides() -> None:
    config = LitehiveConfig(
        pool_stop_on_failure=False,
        pool_max_tasks=10,
        pool_stop_on_dirty_git=False,
        pool_stop_on_attention=True,
    )

    conditions = _dry_run_stop_conditions(
        config,
        stop_on_failure=True,
        max_tasks=3,
        stop_on_dirty_git=True,
    )

    assert conditions.stop_on_failure is True
    assert conditions.max_tasks == 3
    assert conditions.stop_on_dirty_git is True
    assert conditions.stop_on_attention is True


def test_run_dry_run_previews_next_task_without_execution(
    tmp_path,
    monkeypatch,
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="goz",
            engine_preference=["goz"],
            goz_model="goz-preview",
        ),
    )
    task = create_task(tmp_path, title="Dry run selection")

    def _unexpected_run_task(*args, **kwargs):
        raise AssertionError("dry-run should not call run_task")

    monkeypatch.setattr("litehive.cli.runner.run_task", _unexpected_run_task)

    result = CliRunner().invoke(
        cli_app.app,
        ["run", "--dry-run", "--workspace", str(tmp_path)],
    )
    state = load_state(tmp_path)

    assert result.exit_code == 0, result.output
    assert f"would_run: 1. {task.id} {task.title}" in result.output
    assert "engine=goz" in result.output
    assert "model=goz-preview" in result.output
    assert state.active_task_id is None
    assert state.queue == [task.id]


def test_run_dry_run_honors_engine_and_model_overrides(tmp_path) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_preference=["codex", "goz"],
            goz_model="workspace-goz-model",
        ),
    )
    task = create_task(tmp_path, title="Dry run override")

    result = CliRunner().invoke(
        cli_app.app,
        [
            "run",
            "--dry-run",
            "--workspace",
            str(tmp_path),
            "--engine",
            "goz",
            "--model",
            "goz-override-model",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"would_run: 1. {task.id} {task.title}" in result.output
    assert "engine=goz" in result.output
    assert "model=goz-override-model" in result.output


def test_run_live_passes_cli_engine_and_model_overrides_to_execution(
    tmp_path,
    monkeypatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Live run override")
    captured: dict[str, object] = {}

    def _fake_run_task(root, queued_task, **kwargs):
        captured["root"] = root
        captured["task_id"] = queued_task.id
        captured.update(kwargs)
        return SimpleNamespace(
            task=queued_task,
            final_stage="accepting",
            failed_reason=None,
            failed_message=None,
        )

    monkeypatch.setattr("litehive.cli.runner.run_task", _fake_run_task)

    result = CliRunner().invoke(
        cli_app.app,
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--engine",
            "goz",
            "--model",
            "goz-live-model",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["root"] == tmp_path
    assert captured["task_id"] == task.id
    assert captured["engine_override"] == "goz"
    assert captured["model_override"] == "goz-live-model"
