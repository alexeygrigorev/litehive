import sys
from pathlib import Path
from types import SimpleNamespace
import importlib

import pytest

import litehive.main as main_module
from litehive.config.paths import litehive_root
from litehive.config.workspace import ensure_workspace

cli_app_module = importlib.import_module("litehive.cli.app")


def test_main_rewrites_agent_report_compat_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_cli_main() -> int:
        captured["argv"] = list(sys.argv)
        return 7

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "planner")
    monkeypatch.setattr(cli_app_module, "main", fake_cli_main)
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
    monkeypatch.setattr(cli_app_module, "main", fake_cli_main)
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


def test_main_blocks_task_update_for_non_pm_agent_roles(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setattr(
        sys,
        "argv",
        ["litehive", "task", "update", "T-0001", "--goal", "Tighten scope"],
    )

    exit_code = main_module.main()

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "not authorized" in output
    assert "litehive agent update" in output


def test_main_allows_task_add_for_agent_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_cli_main() -> int:
        captured["argv"] = list(sys.argv)
        return 11

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "planner")
    monkeypatch.setattr(cli_app_module, "main", fake_cli_main)
    monkeypatch.setattr(
        sys,
        "argv",
        ["litehive", "task", "add", "Follow-up task", "--goal", "Split mixed scope"],
    )

    exit_code = main_module.main()

    assert exit_code == 11
    assert captured["argv"] == [
        "litehive",
        "task",
        "add",
        "Follow-up task",
        "--goal",
        "Split mixed scope",
    ]


def test_main_allows_task_close_for_agent_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_cli_main() -> int:
        captured["argv"] = list(sys.argv)
        return 12

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "reviewer")
    monkeypatch.setattr(cli_app_module, "main", fake_cli_main)
    monkeypatch.setattr(
        sys,
        "argv",
        ["litehive", "task", "close", "T-0001", "--outcome", "duplicate", "--reason", "already done"],
    )

    exit_code = main_module.main()

    assert exit_code == 12
    assert captured["argv"] == [
        "litehive",
        "task",
        "close",
        "T-0001",
        "--outcome",
        "duplicate",
        "--reason",
        "already done",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["litehive", "status"],
        ["litehive", "task", "list"],
        ["litehive", "task", "browse", "--since", "24h"],
        ["litehive", "task", "show", "T-0001"],
    ],
)
def test_main_blocks_broad_operator_inspection_for_agent_roles(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "qa")
    monkeypatch.setattr(sys, "argv", argv)

    exit_code = main_module.main()

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "You are not authorized to perform this command." in output
    assert "litehive agent update" in output
    assert "operator inspection commands" in output


def test_main_routes_root_help_for_agent_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_cli_main() -> int:
        captured["argv"] = list(sys.argv)
        return 3

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setattr(cli_app_module, "main", fake_cli_main)
    monkeypatch.setattr(sys, "argv", ["litehive", "--help"])

    exit_code = main_module.main()

    assert exit_code == 3
    assert captured["argv"] == ["litehive", "--help"]


def test_main_routes_root_command_help_for_agent_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_cli_main() -> int:
        captured["argv"] = list(sys.argv)
        return 4

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setattr(cli_app_module, "main", fake_cli_main)
    monkeypatch.setattr(sys, "argv", ["litehive", "queue", "recover", "--help"])

    exit_code = main_module.main()

    assert exit_code == 4
    assert captured["argv"] == ["litehive", "queue", "recover", "--help"]


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


def test_main_routes_queue_subcommands_through_root_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_cli_main() -> int:
        captured["argv"] = list(sys.argv)
        return 13

    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.setattr(cli_app_module, "main", fake_cli_main)
    monkeypatch.setattr(sys, "argv", ["litehive", "queue", "recover", "T-0001"])

    exit_code = main_module.main()

    assert exit_code == 13
    assert captured["argv"] == ["litehive", "queue", "recover", "T-0001"]


def test_fast_status_prefers_runner_active_task_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = SimpleNamespace(issues=[])

    monkeypatch.setattr("litehive.main.resolve_workspace", lambda _arg, **_kwargs: Path("/tmp/ws"))
    monkeypatch.setattr(
        "litehive.observability.status.collect_task_pipeline_status",
        lambda workspace, **_kwargs: status,
    )
    monkeypatch.setattr("litehive.observability.status_diagnostics.status_has_problems", lambda issues: False)
    monkeypatch.setattr(
        "litehive.observability.status.render_task_pipeline_status_lines",
        lambda task_status, *, workspace, mode: [
            "active_task_id: T-0381",
            "active_task_status: in_progress/implementing",
        ],
    )

    exit_code = main_module.fast_status([])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "active_task_id: T-0381" in output
    assert "active_task_status: in_progress/implementing" in output


def test_main_status_fast_option_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    ensure_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["litehive", "status", "--fast"])

    exit_code = main_module.main()
    output = capsys.readouterr()

    assert exit_code == 2
    assert "No such option: --fast" in output.err


def test_main_status_reports_corrupt_registry_from_workspace_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    ensure_workspace(tmp_path)
    registry_path = litehive_root() / "workspaces.db"
    registry_path.write_text("not a sqlite database", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["litehive", "status"])

    exit_code = main_module.main()
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"registry: BROKEN at {registry_path}" in output
    assert "global workspace registry database" in output
    assert "health:" in output
    assert registry_path.exists()


def test_main_status_reports_corrupt_registry_from_workspace_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    ensure_workspace(tmp_path)
    registry_path = litehive_root() / "workspaces.db"
    registry_path.write_text("not a sqlite database", encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setattr(sys, "argv", ["litehive", "status"])

    exit_code = main_module.main()
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"registry: BROKEN at {registry_path}" in output
    assert "global workspace registry database" in output
    assert "health:" in output
    assert registry_path.exists()
