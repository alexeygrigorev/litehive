import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import litehive.main as main_module


def test_main_rewrites_agent_report_compat_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_cli_main() -> int:
        captured["argv"] = list(sys.argv)
        return 7

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setattr("litehive.cli.app.main", fake_cli_main)
    monkeypatch.setattr(
        sys,
        "argv",
        ["litehive", "report", "--verdict", "pass", "--message", "ok"],
    )

    exit_code = main_module.main()

    assert exit_code == 7
    assert captured["argv"] == [
        "litehive",
        "agent",
        "report",
        "--verdict",
        "pass",
        "--message",
        "ok",
    ]


def test_main_allows_recovery_diagnostic_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_cli_main() -> int:
        captured["argv"] = list(sys.argv)
        return 9

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "recovery")
    monkeypatch.setattr("litehive.cli.app.main", fake_cli_main)
    monkeypatch.setattr(
        sys,
        "argv",
        ["litehive", "pipeline", "journal", "T-0001"],
    )

    exit_code = main_module.main()

    assert exit_code == 9
    assert captured["argv"] == ["litehive", "pipeline", "journal", "T-0001"]


def test_main_blocks_non_recovery_diagnostic_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setattr(
        sys,
        "argv",
        ["litehive", "pipeline", "journal", "T-0001"],
    )

    exit_code = main_module.main()

    assert exit_code == 1
    assert "You are not authorized to perform this command." in capsys.readouterr().out


def test_main_dispatches_task_subcommands_without_full_root_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)

    def fake_task_app(*, standalone_mode: bool = False):
        captured["argv"] = list(sys.argv)
        captured["standalone_mode"] = standalone_mode
        return 5

    monkeypatch.setattr("litehive.cli.task_cli.app", fake_task_app)
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.setattr(sys, "argv", ["litehive", "task", "show", "T-0001"])

    exit_code = main_module.main()

    assert exit_code == 5
    assert captured["argv"] == ["litehive", "show", "T-0001"]
    assert captured["standalone_mode"] is False


def test_main_dispatches_pipeline_subcommands_without_full_root_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)

    def fake_pipeline_app(*, standalone_mode: bool = False):
        captured["argv"] = list(sys.argv)
        captured["standalone_mode"] = standalone_mode
        return 6

    monkeypatch.setattr("litehive.cli.pipeline_cli.app", fake_pipeline_app)
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.setattr(sys, "argv", ["litehive", "pipeline", "journal", "T-0001"])

    exit_code = main_module.main()

    assert exit_code == 6
    assert captured["argv"] == ["litehive", "journal", "T-0001"]
    assert captured["standalone_mode"] is False


def test_main_dispatches_agent_subcommands_for_agent_roles_without_full_root_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_agent_app(args=None, *, standalone_mode: bool = False):
        captured["argv"] = list(sys.argv)
        captured["standalone_mode"] = standalone_mode
        captured["args"] = args
        return 8

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "planner")
    monkeypatch.setattr("litehive.cli.agent_cli.agent_app", fake_agent_app)
    monkeypatch.setattr(sys, "argv", ["litehive", "agent", "update", "--task-id", "T-0001", "--goal", "x"])

    exit_code = main_module.main()

    assert exit_code == 8
    assert captured["argv"] == ["litehive", "update", "--task-id", "T-0001", "--goal", "x"]
    assert captured["standalone_mode"] is False
    assert captured["args"] is None


def test_main_dispatches_root_add_without_full_root_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)

    def fake_task_app(*, standalone_mode: bool = False):
        captured["argv"] = list(sys.argv)
        captured["standalone_mode"] = standalone_mode
        return 10

    monkeypatch.setattr("litehive.cli.task_cli.app", fake_task_app)
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.setattr(sys, "argv", ["litehive", "add", "New task", "--goal", "scope it"])

    exit_code = main_module.main()

    assert exit_code == 10
    assert captured["argv"] == ["litehive", "add", "New task", "--goal", "scope it"]
    assert captured["standalone_mode"] is False


def test_fast_status_prefers_runner_active_task_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = SimpleNamespace(
        state=SimpleNamespace(active_task_id=None, queue=["T-0382"], pool_stop_reason=None),
        runner=SimpleNamespace(
            status="running",
            pid=123,
            started_at="2026-04-16T03:15:43Z",
            heartbeat_at="2026-04-16T03:21:53Z",
            active_task_id="T-0381",
        ),
        monitoring=SimpleNamespace(),
        config=SimpleNamespace(default_engine="codex"),
        issues=[],
    )
    task = SimpleNamespace(
        title="Move stage and recovery reports off YAML storage",
        status="in_progress",
        pipeline_status="implementing",
        runtime=SimpleNamespace(
            active_subagent=None,
            last_subagent=None,
            current_stage=SimpleNamespace(stage="implementing"),
        ),
    )

    monkeypatch.setattr("litehive.main.resolve_workspace", lambda _arg: Path("/tmp/ws"))
    monkeypatch.setattr("litehive.main.waiting_for_you_lines", lambda workspace: [])
    monkeypatch.setattr(
        "litehive.observability.status_diagnostics.collect_status_snapshot",
        lambda workspace: snapshot,
    )
    monkeypatch.setattr("litehive.observability.status_diagnostics.status_has_problems", lambda issues: False)
    monkeypatch.setattr(
        "litehive.observability.engine_monitoring.render_engine_monitoring_lines", lambda monitoring: []
    )
    monkeypatch.setattr("litehive.state.records.get_task", lambda workspace, task_id: task)

    exit_code = main_module._fast_status([])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "active_task_id: T-0381" in output
    assert "active_task_status: in_progress/implementing" in output
