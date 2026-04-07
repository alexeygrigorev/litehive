"""Tests for engine freeze/unfreeze CLI and runtime filtering."""
from tests.workspace_helpers import *  # noqa: F401,F403

from datetime import datetime, timedelta, timezone

from litehive.cli.engine import _parse_local_datetime


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
    from litehive.runtime import resolve_engine_attempt_order

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
    from litehive.runtime import resolve_engine_attempt_order

    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    config = LitehiveConfig(
        default_engine="codex",
        engine_freeze={"codex": past},
    )
    task = TaskRecord(id="T-0001", slug="test", title="test", status="queued", pipeline_status="implementing")

    order = resolve_engine_attempt_order(task, config)
    assert "codex" in order


def test_is_engine_frozen_and_active_freezes() -> None:
    from litehive.runtime import is_engine_frozen, active_engine_freezes

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
        argparse.Namespace(workspace=tmp_path, full=False, fast=False)
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "engine_frozen: codex until" in output


def test_status_no_frozen_engines(tmp_path: Path, capsys) -> None:
    """Status without frozen engines shows no freeze lines."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    exit_code = _cmd_status(
        argparse.Namespace(workspace=tmp_path, full=False, fast=False)
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
    from litehive.runtime import resolve_engine_attempt_order

    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    config = LitehiveConfig(
        default_engine="codex",
        engine_freeze={"opencode": future},
        engine_fallbacks={
            "codex": ["opencode", "gemini", "copilot"],
        },
    )
    task = TaskRecord(id="T-0001", slug="test", title="test", status="queued", pipeline_status="implementing")

    order = resolve_engine_attempt_order(task, config)
    assert "codex" in order
    assert "opencode" not in order
    assert "gemini" in order
