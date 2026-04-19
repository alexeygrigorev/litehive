from types import SimpleNamespace
import importlib

from typer.testing import CliRunner

from litehive.config.workspace import ensure_workspace

modern_cli = importlib.import_module("litehive.cli.app")


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


def test_root_help_hides_preserved_recovery_shortcuts() -> None:
    result = CliRunner().invoke(modern_cli.app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "recover" not in result.output
    assert "prioritize" not in result.output
    assert "switch" not in result.output


def test_run_drain_runs_until_queue_is_empty(tmp_path, monkeypatch) -> None:
    ensure_workspace(tmp_path)

    class Task:
        def __init__(self, tid: str, title: str) -> None:
            self.id = tid
            self.title = title

    class Result:
        def __init__(self, task) -> None:
            self.task = task
            self.final_stage = "done"
            self.failed_reason = None
            self.failed_message = None

    queue = [Task("T-1", "one"), Task("T-2", "two")]
    calls: list[tuple[str, object]] = []

    def fake_dequeue_next_task(workspace):  # type: ignore[no-untyped-def]
        calls.append(("dequeue", len(queue)))
        return queue.pop(0) if queue else None

    def fake_run_task(workspace, task, engine_override=None, model_override=None):  # type: ignore[no-untyped-def]
        calls.append(("run_task", task.id))
        return Result(task)

    monkeypatch.setattr("litehive.cli.runner.dequeue_next_task", fake_dequeue_next_task)
    monkeypatch.setattr("litehive.cli.runner.run_task", fake_run_task)

    result = CliRunner().invoke(
        modern_cli.app,
        ["run", "--drain", "--workspace", str(tmp_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "task: T-1 one" in result.output
    assert "task: T-2 two" in result.output
    assert calls == [
        ("dequeue", 2),
        ("run_task", "T-1"),
        ("dequeue", 1),
        ("run_task", "T-2"),
        ("dequeue", 0),
    ]
    assert queue == []
