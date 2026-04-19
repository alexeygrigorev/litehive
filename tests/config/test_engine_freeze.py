"""Tests for engine freeze/unfreeze CLI and runtime filtering."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from heru import ENGINE_CHOICES
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.engine_models import (
    EngineSelection,
    clear_persisted_engine_freeze,
    parse_engine_freeze_until,
    persist_engine_freeze_iso,
)
from litehive.config.loading import load_config
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.domain.task import TaskRecord
from litehive.git.ops import GitError
from litehive.lifecycle.engines import ConfigBackedEngineSelector
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.state.records import create_task


def _run_engine(*args: str) -> tuple[int | None, str]:
    result = CliRunner().invoke(app, list(args), standalone_mode=False)
    return result.return_value, result.output


class _StubLifecycleEngine:
    def __init__(self, name: str, model_name: str | None = None) -> None:
        self.name = name
        self.model_name = model_name

    def with_model(self, model_name: str | None) -> "_StubLifecycleEngine":
        return _StubLifecycleEngine(self.name, model_name=model_name)


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


def test_engine_freeze_helpers_persist_and_clear_workspace_config(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    assert parse_engine_freeze_until("2099-06-15") == "2099-06-15T00:00:00Z"
    assert parse_engine_freeze_until("2099-06-15 14:30") is None

    persist_engine_freeze_iso(tmp_path, engine_name="codex", freeze_iso="2099-06-15T00:00:00Z")
    assert load_config(tmp_path).engine_freeze["codex"] == "2099-06-15T00:00:00Z"

    assert clear_persisted_engine_freeze(tmp_path, engine_name="gemini") is False
    assert clear_persisted_engine_freeze(tmp_path, engine_name="codex") is True
    assert "codex" not in load_config(tmp_path).engine_freeze


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

    freeze_until = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0)
    freeze_iso = freeze_until.strftime("%Y-%m-%dT%H:%M:%SZ")
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="codex", engine_preference=["codex", "gemini"]),
    )
    task = TaskRecord(id="T-0001", slug="test", title="test", status="queued", pipeline_status="implementing")
    config = load_config(tmp_path)

    def fake_quota_block(root: Path, engine_name: str):
        if engine_name == "codex":
            return f"codex quota exhausted (used 100%, resets at {freeze_iso})", freeze_until
        return None, None

    monkeypatch.setattr("litehive.config.engine_models._engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "gemini"
    reloaded = load_config(tmp_path)
    assert reloaded.engine_freeze["codex"] == freeze_iso


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
    refreshed = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0)
    refreshed_iso = refreshed.strftime("%Y-%m-%dT%H:%M:%SZ")
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
            return f"codex quota exhausted (used 100%, resets at {refreshed_iso})", refreshed
        return None, None

    monkeypatch.setattr("litehive.config.engine_models._engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "gemini"
    assert quota_calls[0] == "codex"
    assert load_config(tmp_path).engine_freeze["codex"] == refreshed_iso


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
    assert "codex" not in load_config(tmp_path).engine_freeze


def test_lifecycle_selector_uses_shared_select_engine_when_task_record_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_preference=["codex", "gemini"],
        ),
    )
    config = load_config(tmp_path)

    def fake_select_engine(root: Path, task: TaskRecord, config: LitehiveConfig, **kwargs) -> EngineSelection:
        captured["root"] = root
        captured["task"] = task
        captured["config"] = config
        captured["kwargs"] = kwargs
        return EngineSelection(
            engine_name="gemini",
            model_name="gemini-2.5-pro",
            engine_attempts=["gemini"],
            skipped=[],
        )

    monkeypatch.setattr("litehive.lifecycle.engines.select_engine", fake_select_engine)

    selector = ConfigBackedEngineSelector(
        config,
        lambda engine_name: _StubLifecycleEngine(engine_name),
        workspace_root=tmp_path,
    )

    engine = selector.select(
        TaskState(task_id="T-4040", stage="implementing", pipeline_mode=PipelineMode.FULL),
        "implementing",
        frozenset({"codex"}),
    )

    assert isinstance(engine, _StubLifecycleEngine)
    assert engine.name == "gemini"
    assert engine.model_name == "gemini-2.5-pro"
    assert captured["root"] == tmp_path
    assert isinstance(captured["task"], TaskRecord)
    assert captured["task"].id == "T-4040"
    assert captured["task"].pipeline_status == "implementing"
    assert captured["kwargs"] == {
        "engine_override": None,
        "model_override": None,
        "excluded_engine_names": frozenset({"codex"}),
    }


@pytest.mark.parametrize(
    ("recovery_engine", "default_engine", "expected_kwargs"),
    [
        ("auto", "codex", {"engine_override": None, "require_available": True}),
        (None, "codex", {"engine_override": None, "require_available": True}),
        ("codex", "gemini", {"engine_override": "codex", "require_available": True}),
    ],
)
def test_recovery_engine_uses_shared_select_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_engine: str | None,
    default_engine: str,
    expected_kwargs: dict[str, object],
) -> None:
    from litehive.tasks.recovery_engine import resolve_recovery_engine

    captured: dict[str, object] = {}
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine=default_engine,
            recovery_engine=recovery_engine,
            engine_preference=["codex", "gemini"],
        ),
    )
    task = create_task(tmp_path, title="Recovery selection")
    config = load_config(tmp_path)

    def fake_select_engine(root: Path, task: TaskRecord, config: LitehiveConfig, **kwargs) -> EngineSelection:
        captured["root"] = root
        captured["task"] = task
        captured["config"] = config
        captured["kwargs"] = kwargs
        return EngineSelection(
            engine_name="gemini",
            model_name="gemini-2.5-pro",
            engine_attempts=["codex", "gemini"],
            skipped=[],
        )

    monkeypatch.setattr("litehive.config.engine_models.select_engine", fake_select_engine)

    engine_name, model_name = resolve_recovery_engine(tmp_path, task, config)

    assert engine_name == "gemini"
    assert model_name == "gemini-2.5-pro"
    assert captured["root"] == tmp_path
    assert captured["task"] == task
    assert captured["config"] == config
    assert captured["kwargs"] == expected_kwargs


def test_recovery_auto_engine_respects_shared_selector_blocked_result(tmp_path: Path) -> None:
    from litehive.config.engine_models import select_engine
    from litehive.tasks.recovery_engine import resolve_recovery_engine

    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            recovery_engine="auto",
            engine_preference=["codex", "gemini"],
            engine_freeze={"codex": future, "gemini": future},
        ),
    )
    task = create_task(tmp_path, title="Recovery freeze repro")
    config = load_config(tmp_path)

    selection = select_engine(tmp_path, task, config, require_available=True)

    assert selection.engine_name is None
    assert selection.blocked_reason == "all candidate engines are frozen"

    with pytest.raises(GitError, match="all candidate engines are frozen"):
        resolve_recovery_engine(tmp_path, task, config)


@pytest.mark.parametrize(
    ("recovery_engine", "default_engine", "selector_kwargs"),
    [
        (None, "codex", {}),
        ("codex", "gemini", {"engine_override": "codex"}),
    ],
)
def test_recovery_non_auto_branches_skip_frozen_engines(
    tmp_path: Path,
    recovery_engine: str | None,
    default_engine: str,
    selector_kwargs: dict[str, object],
) -> None:
    from litehive.config.engine_models import select_engine
    from litehive.tasks.recovery_engine import resolve_recovery_engine

    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine=default_engine,
            recovery_engine=recovery_engine,
            engine_preference=["codex", "gemini"],
            engine_freeze={"codex": future},
        ),
    )
    task = create_task(tmp_path, title=f"Recovery branch {recovery_engine!r}")
    config = load_config(tmp_path)

    selection = select_engine(tmp_path, task, config, require_available=True, **selector_kwargs)
    engine_name, _model_name = resolve_recovery_engine(tmp_path, task, config)

    assert selection.engine_name == "gemini"
    assert engine_name == "gemini"
