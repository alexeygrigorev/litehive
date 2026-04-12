import pytest; pytest.skip("v1 executor tests — pipeline_old deleted", allow_module_level=True)
"""Tests for engine freeze/unfreeze CLI and runtime filtering."""
from tests.workspace_helpers import (
    EngineBudgetLedger,
    LitehiveConfig,
    Path,
    TaskRecord,
    _cmd_status,
    argparse,
    build_parser,
    create_task,
    ensure_workspace,
    load_config,
    pytest,
)

from datetime import datetime, timedelta, timezone

from litehive.cli.engine import _parse_local_datetime
from litehive.pipeline_old import EngineSelection


def test_parse_local_datetime_date_only():
    dt = _parse_local_datetime("2026-04-08")
    assert dt.tzinfo is not None
    assert dt.tzinfo == timezone.utc
    # Should be start of day in local TZ converted to UTC
    local_midnight = datetime(2026, 4, 8).astimezone()
    expected_utc = local_midnight.astimezone(timezone.utc)
    assert dt == expected_utc


def test_parse_local_datetime_with_time():
    dt = _parse_local_datetime("2026-04-08 09:47")
    assert dt.tzinfo == timezone.utc
    local_dt = datetime(2026, 4, 8, 9, 47).astimezone()
    expected_utc = local_dt.astimezone(timezone.utc)
    assert dt == expected_utc


def test_parse_local_datetime_invalid():
    with pytest.raises(ValueError, match="Cannot parse"):
        _parse_local_datetime("not-a-date")


def test_engine_freeze_cli_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI: freeze an engine, verify config, then unfreeze."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine

    # Freeze codex until far future
    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="freeze",
            engine_name="codex",
            until="2099-12-31",
        )
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "engine_frozen: codex" in output

    config = load_config(tmp_path)
    assert "codex" in config.engine_freeze
    assert "2099" in config.engine_freeze["codex"]

    # Unfreeze
    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="unfreeze",
            engine_name="codex",
        )
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "engine_unfrozen: codex" in output

    config = load_config(tmp_path)
    assert "codex" not in config.engine_freeze


def test_engine_freeze_with_datetime(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="freeze",
            engine_name="gemini",
            until="2099-06-15 14:30",
        )
    )
    assert exit_code == 0
    config = load_config(tmp_path)
    assert "gemini" in config.engine_freeze
    # Stored as UTC ISO
    assert "T" in config.engine_freeze["gemini"]


def test_unfreeze_not_frozen_engine_returns_error(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="unfreeze",
            engine_name="codex",
        )
    )
    assert exit_code == 1
    assert "not frozen" in capsys.readouterr().out


def test_engine_set_backward_compat(tmp_path: Path, capsys) -> None:
    """litehive engine codex still works (backward compat)."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="opencode"))

    from litehive.cli import _cmd_engine

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="codex",
            engine_name=None,
            until=None,
        )
    )
    assert exit_code == 0
    config = load_config(tmp_path)
    assert config.default_engine == "codex"


def test_engine_set_subcommand(tmp_path: Path, capsys) -> None:
    """litehive engine set gemini works."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="set",
            engine_name="gemini",
            until=None,
        )
    )
    assert exit_code == 0
    config = load_config(tmp_path)
    assert config.default_engine == "gemini"


def test_frozen_engine_skipped_in_attempt_order(tmp_path: Path) -> None:
    """Frozen engines are removed from the attempt order."""
    from litehive.pipeline_old import resolve_engine_attempt_order

    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    config = LitehiveConfig(
        default_engine="codex",
        engine_freeze={"codex": future},
    )
    task = TaskRecord(id="T-0001", slug="test", title="test", status="queued", pipeline_status="implementing")

    order = resolve_engine_attempt_order(task, config)
    assert "codex" not in order
    # Fallbacks should still appear (minus frozen)
    assert len(order) > 0


def test_expired_freeze_not_skipped(tmp_path: Path) -> None:
    """Engines with expired freezes are not skipped."""
    from litehive.pipeline_old import resolve_engine_attempt_order

    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    config = LitehiveConfig(
        default_engine="codex",
        engine_freeze={"codex": past},
    )
    task = TaskRecord(id="T-0001", slug="test", title="test", status="queued", pipeline_status="implementing")

    order = resolve_engine_attempt_order(task, config)
    assert "codex" in order


def test_is_engine_frozen_and_active_freezes() -> None:
    from litehive.config.engine_models import is_engine_frozen, active_engine_freezes

    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    config = LitehiveConfig(
        engine_freeze={"codex": future, "gemini": past},
    )

    assert is_engine_frozen(config, "codex") is True
    assert is_engine_frozen(config, "gemini") is False
    assert is_engine_frozen(config, "opencode") is False

    freezes = active_engine_freezes(config)
    assert "codex" in freezes
    assert "gemini" not in freezes


def test_status_shows_frozen_engines(tmp_path: Path, capsys) -> None:
    """litehive status displays frozen engines."""
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_freeze={"codex": future},
        ),
    )

    exit_code = _cmd_status(
        argparse.Namespace(workspace=tmp_path, full=True, fast=False)
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "engine_frozen: codex until" in output


def test_status_no_frozen_engines(tmp_path: Path, capsys) -> None:
    """Status without frozen engines shows no freeze lines."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    exit_code = _cmd_status(
        argparse.Namespace(workspace=tmp_path, full=True, fast=False)
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "engine_frozen" not in output


def test_parser_accepts_freeze_subcommand() -> None:
    parser = build_parser()

    args = parser.parse_args(
        ["engine", "freeze", "codex", "--until", "2026-04-08", "--workspace", "/tmp/demo"]
    )
    assert args.command == "engine"
    assert args.engine_action == "freeze"
    assert args.engine_name == "codex"
    assert args.until == "2026-04-08"


def test_parser_accepts_unfreeze_subcommand() -> None:
    parser = build_parser()

    args = parser.parse_args(
        ["engine", "unfreeze", "codex", "--workspace", "/tmp/demo"]
    )
    assert args.command == "engine"
    assert args.engine_action == "unfreeze"
    assert args.engine_name == "codex"


def test_parser_accepts_set_subcommand() -> None:
    parser = build_parser()

    args = parser.parse_args(
        ["engine", "set", "gemini", "--workspace", "/tmp/demo"]
    )
    assert args.command == "engine"
    assert args.engine_action == "set"
    assert args.engine_name == "gemini"


def test_frozen_engine_in_fallback_chain_skipped(tmp_path: Path) -> None:
    """When a fallback engine is frozen, it's skipped but others remain."""
    from litehive.pipeline_old import resolve_engine_attempt_order

    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    config = LitehiveConfig(
        default_engine="codex",
        engine_freeze={"opencode": future},
        engine_preference=["codex", "opencode", "gemini", "copilot"],
    )
    task = TaskRecord(id="T-0001", slug="test", title="test", status="queued", pipeline_status="implementing")

    order = resolve_engine_attempt_order(task, config)
    assert "codex" in order
    assert "opencode" not in order
    assert "gemini" in order


def test_select_engine_records_quota_freeze_and_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litehive.pipeline_old import select_engine

    freeze_until = datetime(2099, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="codex", engine_preference=["codex", "gemini"]),
    )
    task = TaskRecord(id="T-0001", slug="test", title="test", status="queued", pipeline_status="implementing")
    config = load_config(tmp_path)

    def fake_quota_block(root: Path, engine_name: str):
        if engine_name == "codex":
            return "codex quota exhausted (used 100%, resets at 2099-01-02T03:04:05Z)", freeze_until
        return None, None

    monkeypatch.setattr("litehive.pipeline_old._models._engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "gemini"
    reloaded = load_config(tmp_path)
    assert reloaded.engine_freeze["codex"] == "2099-01-02T03:04:05Z"


def test_select_engine_skips_active_freeze_without_quota_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litehive.pipeline_old import select_engine

    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_preference=["codex", "gemini"],
            engine_freeze={"codex": future},
        ),
    )
    task = TaskRecord(id="T-0001", slug="test", title="test", status="queued", pipeline_status="implementing")
    config = load_config(tmp_path)
    quota_calls: list[str] = []

    def fake_quota_block(root: Path, engine_name: str):
        quota_calls.append(engine_name)
        return None, None

    monkeypatch.setattr("litehive.pipeline_old._models._engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "gemini"
    assert quota_calls == ["gemini"]


def test_select_engine_rechecks_expired_freeze_before_refreshing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litehive.pipeline_old import select_engine

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    refreshed = datetime(2099, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_preference=["codex", "gemini"],
            engine_freeze={"codex": past},
        ),
    )
    task = TaskRecord(id="T-0001", slug="test", title="test", status="queued", pipeline_status="implementing")
    config = load_config(tmp_path)
    quota_calls: list[str] = []

    def fake_quota_block(root: Path, engine_name: str):
        quota_calls.append(engine_name)
        if engine_name == "codex":
            return "codex quota exhausted (used 100%, resets at 2099-02-03T04:05:06Z)", refreshed
        return None, None

    monkeypatch.setattr("litehive.pipeline_old._models._engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "gemini"
    assert quota_calls[0] == "codex"
    assert load_config(tmp_path).engine_freeze["codex"] == "2099-02-03T04:05:06Z"


def test_select_engine_rechecks_expired_freeze_and_allows_recovered_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litehive.pipeline_old import select_engine

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_preference=["codex", "gemini"],
            engine_freeze={"codex": past},
        ),
    )
    task = TaskRecord(id="T-0001", slug="test", title="test", status="queued", pipeline_status="implementing")
    config = load_config(tmp_path)
    quota_calls: list[str] = []

    def fake_quota_block(root: Path, engine_name: str):
        quota_calls.append(engine_name)
        return None, None

    monkeypatch.setattr("litehive.pipeline_old._models._engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "codex"
    assert quota_calls == ["codex"]


def test_builder_uses_shared_select_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litehive.pipeline_old._builder import build_executor

    class DummySubagents:
        def run(self, *args, **kwargs):
            raise AssertionError("builder should have stopped before launching a subagent")

    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = create_task(tmp_path, title="Selection test")
    config = load_config(tmp_path)
    monkeypatch.setattr(
        "litehive.pipeline_old._builder.select_engine",
        lambda *args, **kwargs: EngineSelection(
            engine_name=None,
            model_name=None,
            engine_attempts=["codex"],
            skipped=[],
            blocked_reason="shared selector blocked",
        ),
    )

    executor = build_executor(
        tmp_path,
        execution_root=tmp_path,
        initial_engine_names=["codex"],
        workspace_context="",
        subagents=DummySubagents(),
        config=config,
        task=task,
        model_override=None,
        config_auto_commit=False,
        budget_ledger=EngineBudgetLedger(),
    )
    report = executor(task, "implementing")

    assert report.verdict == "blocked"
    assert report.summary == "implementing blocked: shared selector blocked"


def test_dry_run_uses_shared_select_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litehive.cli._dry_run import _plan_single_task_dry_run
    from litehive.config.pool_types import TaskPoolStopConditions

    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = create_task(tmp_path, title="Dry run selection")
    config = load_config(tmp_path)
    monkeypatch.setattr(
        "litehive.cli._dry_run.select_engine",
        lambda *args, **kwargs: EngineSelection(
            engine_name="gemini",
            model_name="gemini-2.5-pro",
            engine_attempts=["codex", "gemini"],
            skipped=[],
        ),
    )

    planned, reason = _plan_single_task_dry_run(
        tmp_path,
        planned_tasks=[task],
        blocked_count=0,
        config=config,
        stop_conditions=TaskPoolStopConditions(),
        engine_override=None,
        model_override=None,
    )

    assert reason == "single_task_complete"
    assert planned[0][1] == "gemini"


def test_recovery_auto_engine_uses_shared_select_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litehive.recovery.execution_recovery import _resolve_recovery_engine

    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            recovery_engine="auto",
            engine_preference=["codex", "gemini"],
        ),
    )
    task = create_task(tmp_path, title="Recovery selection")
    config = load_config(tmp_path)
    monkeypatch.setattr(
        "litehive.pipeline_old._models.select_engine",
        lambda *args, **kwargs: EngineSelection(
            engine_name="gemini",
            model_name="gemini-2.5-pro",
            engine_attempts=["codex", "gemini"],
            skipped=[],
        ),
    )

    engine_name, model_name = _resolve_recovery_engine(tmp_path, task, config)

    assert engine_name == "gemini"
    assert model_name == "gemini-2.5-pro"
