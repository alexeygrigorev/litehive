import sys

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

    def fake_task_app(*, standalone_mode: bool = False):
        captured["argv"] = list(sys.argv)
        captured["standalone_mode"] = standalone_mode
        return 5

    monkeypatch.setattr("litehive.cli.task_cli.app", fake_task_app)
    monkeypatch.setattr(sys, "argv", ["litehive", "task", "show", "T-0001"])

    exit_code = main_module.main()

    assert exit_code == 5
    assert captured["argv"] == ["litehive", "show", "T-0001"]
    assert captured["standalone_mode"] is False


def test_main_dispatches_pipeline_subcommands_without_full_root_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_pipeline_app(*, standalone_mode: bool = False):
        captured["argv"] = list(sys.argv)
        captured["standalone_mode"] = standalone_mode
        return 6

    monkeypatch.setattr("litehive.cli.pipeline_cli.app", fake_pipeline_app)
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

    def fake_task_app(*, standalone_mode: bool = False):
        captured["argv"] = list(sys.argv)
        captured["standalone_mode"] = standalone_mode
        return 10

    monkeypatch.setattr("litehive.cli.task_cli.app", fake_task_app)
    monkeypatch.setattr(sys, "argv", ["litehive", "add", "New task", "--goal", "scope it"])

    exit_code = main_module.main()

    assert exit_code == 10
    assert captured["argv"] == ["litehive", "add", "New task", "--goal", "scope it"]
    assert captured["standalone_mode"] is False
