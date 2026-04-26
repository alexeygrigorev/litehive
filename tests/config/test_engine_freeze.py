"""Tests for engine freeze/unfreeze CLI and runtime filtering."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from heru import ENGINE_CHOICES
from heru.quota import UsageStatus, UsageWindow
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
from litehive.config.runtime_settings import load_runtime_setting_audit_entries
from litehive.config.workspace import ensure_workspace
from litehive.domain.engine import EngineUsageRecord, EngineUsageWindow, WorkspaceEngineMonitoring
from litehive.domain.task import TaskRecord
from litehive.git.ops import GitError
from litehive.lifecycle.engines import ConfigBackedEngineSelector
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.state.persist import load_state, persist_task_and_state_without_runner_guard
from litehive.state.records import create_task, get_task_record
from litehive.tasks.audit import load_task_audit_entries


def _run_engine(*args: str) -> tuple[int | None, str]:
    env = dict(os.environ)
    env.pop("LITEHIVE_AGENT_ROLE", None)
    result = CliRunner().invoke(app, list(args), standalone_mode=False, env=env)
    return (
        result.return_value if result.return_value is not None else result.exit_code,
        result.output,
    )


def _prepare_runnable_task(root: Path, title: str) -> TaskRecord:
    task = create_task(root, title=title)
    state = load_state(root)
    task.pipeline_status = "implementing"
    persist_task_and_state_without_runner_guard(root, task=task, state=state)
    return get_task_record(root, task.id) or task


class _StubLifecycleEngine:
    def __init__(self, name: str, model_name: str | None = None) -> None:
        self.name = name
        self.model_name = model_name

    def with_model(self, model_name: str | None) -> "_StubLifecycleEngine":
        return _StubLifecycleEngine(self.name, model_name=model_name)


def test_engine_freeze_cli_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI: freeze an engine, verify audited DB settings, then unfreeze."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config_path = tmp_path / ".litehive" / "config.yaml"
    raw_config_before = config_path.read_text(encoding="utf-8")

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
    assert config_path.read_text(encoding="utf-8") == raw_config_before

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
    assert config_path.read_text(encoding="utf-8") == raw_config_before
    entries = load_runtime_setting_audit_entries(tmp_path, key="engine_freeze", limit=10)
    assert [entry.actor for entry in entries[:2]] == ["operator", "operator"]
    assert entries[0].context["engine"] == "codex"
    assert entries[0].context["old_value"] == "2099-12-31T00:00:00Z"
    assert entries[0].context["new_value"] is None
    assert entries[1].context["engine"] == "codex"
    assert entries[1].context["old_value"] is None
    assert entries[1].context["new_value"] == "2099-12-31T00:00:00Z"


def test_default_engine_cli_persists_to_audited_db_not_config_file(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config_path = tmp_path / ".litehive" / "config.yaml"
    raw_config_before = config_path.read_text(encoding="utf-8")

    exit_code, output = _run_engine(
        "engine",
        "default",
        "gemini",
        "--workspace",
        str(tmp_path),
        "--reason",
        "Prefer larger context",
    )

    assert exit_code == 0
    assert "default_engine: codex -> gemini" in output
    assert load_config(tmp_path).default_engine == "gemini"
    assert config_path.read_text(encoding="utf-8") == raw_config_before

    entries = load_runtime_setting_audit_entries(tmp_path, key="default_engine", limit=5)
    assert len(entries) == 1
    assert entries[0].actor == "operator"
    assert entries[0].source == "cli"
    assert entries[0].old_value == "codex"
    assert entries[0].new_value == "gemini"
    assert entries[0].context == {"reason": "Prefer larger context"}


def test_engine_preference_cli_persists_to_audited_db(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    exit_code, output = _run_engine(
        "engine",
        "preference",
        "gemini,codex",
        "--workspace",
        str(tmp_path),
        "--reason",
        "Quota rotation",
    )

    assert exit_code == 0
    assert "engine_preference: codex,opencode,gemini,copilot,goz -> gemini,codex" in output
    assert load_config(tmp_path).engine_preference == ["gemini", "codex"]

    audit = _run_engine(
        "db",
        "settings-audit",
        "engine_preference",
        "--workspace",
        str(tmp_path),
    )
    assert audit[0] == 0
    assert "setting_audit_entries: 1" in audit[1]
    assert "actor: operator" in audit[1]
    assert 'old_value: ["codex", "opencode", "gemini", "copilot", "goz"]' in audit[1]
    assert 'new_value: ["gemini", "codex"]' in audit[1]


def test_engine_runtime_settings_treat_config_file_drift_as_bootstrap_only(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex", engine_preference=["codex", "gemini"]))
    first = load_config(tmp_path)
    assert first.default_engine == "codex"
    assert first.engine_preference == ["codex", "gemini"]
    assert first.engine_freeze == {}

    config_path = tmp_path / ".litehive" / "config.yaml"
    raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw_config["default_engine"] = "gemini"
    raw_config["engine_preference"] = ["gemini"]
    raw_config["engine_freeze"] = {"codex": "2099-06-15T00:00:00Z"}
    config_path.write_text(yaml.safe_dump(raw_config, sort_keys=False), encoding="utf-8")

    drifted = load_config(tmp_path)

    assert drifted.default_engine == "codex"
    assert drifted.engine_preference == ["codex", "gemini"]
    assert drifted.engine_freeze == {}


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


def test_engine_status_prints_monitoring_and_live_quota(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="codex", engine_freeze={"gemini": "2099-06-15T00:00:00Z"}),
    )

    monkeypatch.setattr(
        "litehive.cli.engine.load_engine_monitoring",
        lambda root: WorkspaceEngineMonitoring(
            engines={
                "codex": EngineUsageRecord(
                    engine="codex",
                    source="provider",
                    provider="openai",
                    observed_at="2026-04-20T00:00:00Z",
                    invocation_count=4,
                    success_count=3,
                    failure_count=1,
                    limit_event_count=1,
                    usage=EngineUsageWindow(
                        used=60,
                        limit=100,
                        remaining=40,
                        unit="percent",
                        reset_at="2026-04-21T00:00:00Z",
                    ),
                )
            }
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.engine.check_claude_quota",
        lambda: SimpleNamespace(
            error=None,
            short_term=UsageWindow(percent_remaining=87.5, reset_at="2026-04-14T12:00:00Z"),
            long_term=UsageWindow(percent_remaining=55.0, reset_at="2026-04-15T00:00:00Z"),
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.engine.check_codex_quota",
        lambda: SimpleNamespace(
            error=None,
            short_term=UsageWindow(percent_remaining=70.0, reset_at="2026-04-14T17:00:00Z"),
            long_term=UsageWindow(percent_remaining=35.0, reset_at="2026-04-21T00:00:00Z"),
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.engine.check_copilot_quota",
        lambda: SimpleNamespace(
            error=None,
            short_term=UsageWindow(percent_remaining=100.0),
            long_term=UsageWindow(percent_remaining=40.0, reset_at="2026-04-30T00:00:00Z"),
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.engine.check_zai_quota",
        lambda: SimpleNamespace(
            error=None,
            short_term=UsageWindow(percent_remaining=55.0),
            long_term=UsageWindow(percent_remaining=100.0),
        ),
    )

    exit_code, output = _run_engine(
        "engine",
        "status",
        "--workspace",
        str(tmp_path),
    )
    assert exit_code == 0
    assert "default_engine: codex" in output
    assert "engine_freeze: gemini=2099-06-15T00:00:00Z" in output
    for engine_name in ENGINE_CHOICES:
        assert f"engine: {engine_name} " in output
    assert (
        "monitoring: source=provider invocations=4 success=3 failure=1 limits=1 "
        "provider=openai usage=used=60,limit=100,remaining=40,unit=percent,reset_at=2026-04-21T00:00:00Z "
        "observed_at=2026-04-20T00:00:00Z"
    ) in output
    assert (
        "quota: hours remaining=87.5% reset=2026-04-14T12:00:00Z | "
        "weeks remaining=55.0% reset=2026-04-15T00:00:00Z"
    ) in output
    assert (
        "quota: hours remaining=70.0% reset=2026-04-14T17:00:00Z | "
        "weeks remaining=35.0% reset=2026-04-21T00:00:00Z"
    ) in output
    assert "quota: hours remaining=100.0% reset=- | weeks remaining=40.0% reset=2026-04-30T00:00:00Z" in output
    assert (
        output.count(
            "quota: hours remaining=55.0% reset=- | weeks remaining=100.0% reset=-"
        )
        == 2
    )
    assert "quota: unsupported" in output


def test_engine_status_handles_quota_errors_gracefully(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    monkeypatch.setattr(
        "litehive.cli.engine.load_engine_monitoring",
        lambda root: WorkspaceEngineMonitoring(),
    )
    monkeypatch.setattr(
        "litehive.cli.engine.check_claude_quota",
        lambda: UsageStatus(error="no-credentials"),
    )
    monkeypatch.setattr(
        "litehive.cli.engine.check_codex_quota",
        lambda: (_ for _ in ()).throw(RuntimeError("backend timeout")),
    )
    monkeypatch.setattr(
        "litehive.cli.engine.check_copilot_quota",
        lambda: UsageStatus(error="gh exit 1"),
    )
    monkeypatch.setattr(
        "litehive.cli.engine.check_zai_quota",
        lambda: UsageStatus(error="goz exit 1"),
    )

    exit_code, output = _run_engine(
        "engine",
        "status",
        "--workspace",
        str(tmp_path),
    )

    assert exit_code == 0
    assert "quota: unavailable (no-credentials)" in output
    assert "quota: unavailable (backend timeout)" in output
    assert "quota: unavailable (gh exit 1)" in output
    assert output.count("quota: unavailable (goz exit 1)") == 2
    assert "quota: unsupported" in output


def test_queue_switch_cli_queues_task_for_new_engine(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = _prepare_runnable_task(tmp_path, "Switch engines")

    exit_code, output = _run_engine(
        "queue",
        "switch",
        task.id,
        "gemini",
        "--workspace",
        str(tmp_path),
        "--reason",
        "Need larger context window",
    )

    assert exit_code == 0
    assert "engine: codex -> gemini" in output
    refreshed = get_task_record(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.last_engine_switch is not None
    assert refreshed.runtime.last_engine_switch.to_engine == "gemini"
    entries = load_task_audit_entries(tmp_path, task_id=task.id, action="engine_switched", limit=5)
    assert len(entries) == 1
    assert entries[0].actor == "operator"
    assert entries[0].source == "cli"
    assert entries[0].context["old_value"] == "codex"
    assert entries[0].context["new_value"] == "gemini"
    assert entries[0].context["reason"] == "Need larger context window"


def test_queue_switch_subcommand_still_works(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = _prepare_runnable_task(tmp_path, "Switch engines via queue subcommand")

    exit_code, output = _run_engine(
        "queue",
        "switch",
        task.id,
        "gemini",
        "--workspace",
        str(tmp_path),
        "--reason",
        "Need larger context window",
    )

    assert exit_code == 0
    assert "engine: codex -> gemini" in output
    refreshed = get_task_record(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.last_engine_switch is not None
    assert refreshed.runtime.last_engine_switch.to_engine == "gemini"


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

    monkeypatch.setattr("litehive.config.engine_models.engine_quota_block", fake_quota_block)

    selection = select_engine(tmp_path, task, config)

    assert selection.engine_name == "gemini"
    reloaded = load_config(tmp_path)
    assert reloaded.engine_freeze["codex"] == freeze_iso


def test_engine_quota_block_consumes_current_heru_normalized_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from heru.quota.codex_quota import CodexQuotaStatus, CodexQuotaWindow
    import litehive.config.engine_models as engine_models

    engine_models = importlib.reload(engine_models)
    status = CodexQuotaStatus(
        secondary_window=CodexQuotaWindow(used_percent=95.0, reset_at="2026-04-27T00:00:00Z"),
        limit_reached=True,
    )
    monkeypatch.setattr(engine_models, "check_codex_quota", lambda: status)
    monkeypatch.setattr(engine_models, "_record_codex_quota_monitoring", lambda root, status: None)

    reason, freeze_until = engine_models.engine_quota_block(tmp_path, "codex")

    assert reason == "codex usage limit reached, resets 2026-04-27T00:00:00Z"
    assert freeze_until == datetime(2026, 4, 27, tzinfo=timezone.utc)


def test_select_engine_skips_current_heru_quota_status_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from heru.quota.codex_quota import CodexQuotaStatus, CodexQuotaWindow
    import litehive.config.engine_models as engine_models

    engine_models = importlib.reload(engine_models)
    status = CodexQuotaStatus(
        secondary_window=CodexQuotaWindow(used_percent=95.0, reset_at="2026-04-27T00:00:00Z"),
        limit_reached=True,
    )
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="codex", recovery_engine="claude"),
    )
    task = create_task(tmp_path, title="quota selection repro")
    config = load_config(tmp_path)
    monkeypatch.setattr(engine_models, "check_codex_quota", lambda: status)
    monkeypatch.setattr(engine_models, "check_claude_quota", lambda: UsageStatus())
    monkeypatch.setattr(engine_models, "_record_codex_quota_monitoring", lambda root, status: None)

    selection = engine_models.select_engine(tmp_path, task, config, engine_names=["codex", "claude"])

    assert selection.engine_name == "claude"
    assert [(item.engine_name, item.reason) for item in selection.skipped] == [
        ("codex", "codex usage limit reached, resets 2026-04-27T00:00:00Z")
    ]
    assert load_config(tmp_path).engine_freeze["codex"] == "2026-04-27T00:00:00Z"


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

    monkeypatch.setattr("litehive.config.engine_models.engine_quota_block", fake_quota_block)

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

    monkeypatch.setattr("litehive.config.engine_models.engine_quota_block", fake_quota_block)

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

    monkeypatch.setattr("litehive.config.engine_models.engine_quota_block", fake_quota_block)

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
