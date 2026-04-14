"""Tests for engine freeze/unfreeze CLI and runtime filtering."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from heru import ENGINE_CHOICES
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.engine_models import EngineSelection
from litehive.config.loading import load_config
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.domain.task import TaskRecord
from litehive.state.records import create_task

from tests.support.helpers import _cmd_status


def _run_engine(*args: str) -> tuple[int | None, str]:
    result = CliRunner().invoke(app, list(args), standalone_mode=False)
    return result.return_value, result.output


def test_engine_freeze_cli_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI: freeze an engine, verify config, then unfreeze."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    # Freeze codex until far future
    exit_code, output = _run_engine(
        "engine",
        "freeze",
        "codex",
        "--workspace",
        str(tmp_path),
        "--until",
        "2099-12-31",
        "--reason",
        "quota exhausted",
    )
    assert exit_code == 0
    assert "engine_frozen: codex" in output
    assert "reason=quota exhausted" in output

    config = load_config(tmp_path)
    assert config.engine_freeze["codex"] == "2099-12-31T00:00:00Z"

    # Unfreeze
    exit_code, output = _run_engine(
        "engine",
        "unfreeze",
        "codex",
        "--workspace",
        str(tmp_path),
    )
    assert exit_code == 0
    assert "engine_unfrozen: codex" in output

    config = load_config(tmp_path)
    assert "codex" not in config.engine_freeze


def test_engine_freeze_requires_iso_date(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    exit_code, output = _run_engine(
        "engine",
        "freeze",
        "gemini",
        "--workspace",
        str(tmp_path),
        "--until",
        "2099-06-15 14:30",
    )
    assert exit_code == 1
    assert "YYYY-MM-DD" in output


def test_unfreeze_not_frozen_engine_returns_error(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    exit_code, output = _run_engine(
        "engine",
        "unfreeze",
        "codex",
        "--workspace",
        str(tmp_path),
    )
    assert exit_code == 1
    assert "not frozen" in output


def test_engine_status_prints_compact_summary(tmp_path: Path, capsys) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="codex", engine_freeze={"gemini": "2099-06-15T00:00:00Z"}),
    )

    exit_code, output = _run_engine(
        "engine",
        "status",
        "--workspace",
        str(tmp_path),
    )
    assert exit_code == 0
    output = output.strip()
    assert output.startswith("default_engine: codex | engine_freeze: gemini=2099-06-15T00:00:00Z | engines: ")
    for engine_name in ENGINE_CHOICES:
        assert f"{engine_name}(" in output


def test_frozen_engine_skipped_in_attempt_order(tmp_path: Path) -> None:
    """Frozen engines are removed from the attempt order."""
    from litehive.config.engine_models import resolve_engine_attempt_order

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
    from litehive.config.engine_models import resolve_engine_attempt_order

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

def test_cmd_engine_rejects_stale_single_engine_status_namespace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    exit_code, output = _run_engine(
        "engine",
        "status",
        "codex",
        "--workspace",
        str(tmp_path),
    )

    assert exit_code == 1
    assert "does not take an engine name" in output


def test_frozen_engine_in_fallback_chain_skipped(tmp_path: Path) -> None:
    """When a fallback engine is frozen, it's skipped but others remain."""
    from litehive.config.engine_models import resolve_engine_attempt_order

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
    from litehive.config.engine_models import select_engine

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

    monkeypatch.setattr("litehive.config.engine_models._engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "gemini"
    reloaded = load_config(tmp_path)
    assert reloaded.engine_freeze["codex"] == "2099-01-02T03:04:05Z"


def test_select_engine_skips_active_freeze_without_quota_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litehive.config.engine_models import select_engine

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

    monkeypatch.setattr("litehive.config.engine_models._engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "gemini"
    assert quota_calls == ["gemini"]


def test_select_engine_rechecks_expired_freeze_before_refreshing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litehive.config.engine_models import select_engine

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

    monkeypatch.setattr("litehive.config.engine_models._engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "gemini"
    assert quota_calls[0] == "codex"
    assert load_config(tmp_path).engine_freeze["codex"] == "2099-02-03T04:05:06Z"


def test_select_engine_rechecks_expired_freeze_and_allows_recovered_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litehive.config.engine_models import select_engine

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

    monkeypatch.setattr("litehive.config.engine_models._engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "codex"
    assert quota_calls == ["codex"]


def test_builder_uses_shared_select_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pass  # build_executor deleted


def test_dry_run_uses_shared_select_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litehive.cli.dry_run import plan_single_task_dry_run
    from litehive.domain.pool import TaskPoolStopConditions

    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = create_task(tmp_path, title="Dry run selection")
    config = load_config(tmp_path)
    monkeypatch.setattr(
        "litehive.cli.dry_run.select_engine",
        lambda *args, **kwargs: EngineSelection(
            engine_name="gemini",
            model_name="gemini-2.5-pro",
            engine_attempts=["codex", "gemini"],
            skipped=[],
        ),
    )

    planned, reason = plan_single_task_dry_run(
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
    from litehive.recovery.execution_recovery import resolve_recovery_engine

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
        "litehive.config.engine_models.select_engine",
        lambda *args, **kwargs: EngineSelection(
            engine_name="gemini",
            model_name="gemini-2.5-pro",
            engine_attempts=["codex", "gemini"],
            skipped=[],
        ),
    )

    engine_name, model_name = resolve_recovery_engine(tmp_path, task, config)

    assert engine_name == "gemini"
    assert model_name == "gemini-2.5-pro"
