from types import SimpleNamespace
import importlib

from typer.testing import CliRunner

from litehive.attention import record_attention
from litehive.config.workspace import ensure_workspace
from litehive.state.persist import load_state
from litehive.state.records import create_task

modern_cli = importlib.import_module("litehive.cli.app")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def test_cli_main_module_remains_importable() -> None:
    compat_module = importlib.import_module("litehive.cli.main")

    assert compat_module.app is modern_cli.app
    assert compat_module.main is modern_cli.main


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


def test_root_help_hides_legacy_shortcuts_and_internal_aliases() -> None:
    result = CliRunner().invoke(modern_cli.app, ["--help"])

    assert result.exit_code == 0, result.output
    for command in ["add", "recover", "prioritize", "switch", "agent", "daemon"]:
        assert command not in result.output
    for command in ["start", "stop", "restart", "task", "queue", "import", "engine"]:
        assert command in result.output


def test_removed_root_aliases_are_no_longer_available() -> None:
    removed_aliases = [
        "add",
        "list",
        "show",
        "update",
        "close",
        "abandon",
        "debug",
        "logs",
        "move",
        "promote",
        "requeue",
        "resume",
        "issue",
        "intake",
        "import-issue",
        "import-issues",
        "cleanup",
        "recover",
        "prioritize",
        "switch",
    ]

    runner = CliRunner()
    for command in removed_aliases:
        result = runner.invoke(modern_cli.app, [command, "--help"])

        assert result.exit_code != 0, result.output
        diagnostic = result.output or str(result.exception)
        assert f"No such command '{command}'" in diagnostic


def test_queue_recover_prioritize_and_switch_help_describe_supported_subcommands() -> None:
    expected_help = {
        ("queue", "recover"): "Requeue a completed task without reverting workspace code",
        ("queue", "prioritize"): "Move queued tasks to the front in the provided order",
        ("queue", "switch"): "Switch a queued or active task to a different engine",
    }

    runner = CliRunner()
    for argv, help_text in expected_help.items():
        result = runner.invoke(modern_cli.app, [*argv, "--help"])

        assert result.exit_code == 0, result.output
        assert help_text in _normalized(result.output)

    switch_help = runner.invoke(modern_cli.app, ["queue", "switch", "--help"])
    normalized = _normalized(switch_help.output)
    assert "Engine to switch to" in normalized
    assert "Why the engine switch happened" in normalized


def test_queue_resume_and_requeue_help_mentions_parked_semantics() -> None:
    expected_help = {
        ("queue", "resume"): "Resume an interrupted, parked, flagged, or closed task at its current stage",
        ("queue", "requeue"): "Requeue a parked, flagged, or closed task from the implementation entry stage",
    }

    runner = CliRunner()
    for argv, help_text in expected_help.items():
        result = runner.invoke(modern_cli.app, [*argv, "--help"])

        assert result.exit_code == 0, result.output
        assert help_text in _normalized(result.output)


def test_attention_commands_are_registered_and_work(tmp_path) -> None:
    ensure_workspace(tmp_path)
    item = record_attention(
        tmp_path,
        kind="destructive_git_denied",
        title="Destructive git command was blocked",
        reason="`git reset --hard origin/main` was rejected.",
        suggested_action="Use a safe git command and then run `litehive attention resolve <id>`.",
        dedupe_key="destructive_git_denied:cli",
    )

    runner = CliRunner()
    list_result = runner.invoke(modern_cli.app, ["attention", "list", "--workspace", str(tmp_path)])
    resolve_result = runner.invoke(
        modern_cli.app,
        ["attention", "resolve", str(item.id), "--workspace", str(tmp_path)],
    )

    assert list_result.exit_code == 0, list_result.output
    assert "pending_attention: 1" in list_result.output
    assert f"attention: {item.id}" in list_result.output
    assert resolve_result.exit_code == 0, resolve_result.output
    assert f"resolved_attention: {item.id}" in resolve_result.output


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


def test_run_dry_run_previews_next_task_without_mutating_queue(tmp_path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Preview task")

    result = CliRunner().invoke(
        modern_cli.app,
        ["run", "--dry-run", "--workspace", str(tmp_path)],
        catch_exceptions=False,
    )

    state = load_state(tmp_path)
    assert result.exit_code == 0, result.output
    assert f"task: {task.id} Preview task" in result.output
    assert "engine: codex" in result.output
    assert state.active_task_id is None
    assert state.queue == [task.id]


def test_run_dry_run_reports_no_queued_task(tmp_path) -> None:
    ensure_workspace(tmp_path)

    result = CliRunner().invoke(
        modern_cli.app,
        ["run", "--dry-run", "--workspace", str(tmp_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert result.output == "No queued task.\n"


def test_run_dry_run_rejects_drain_mode(tmp_path) -> None:
    ensure_workspace(tmp_path)

    result = CliRunner().invoke(
        modern_cli.app,
        ["run", "--dry-run", "--drain", "--workspace", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "cannot be combined with --drain" in result.output
