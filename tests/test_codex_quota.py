"""Tests for proactive codex quota checking."""

import json
import time
from pathlib import Path

import pytest

from litehive.engines._codex_quota import (
    CodexQuotaStatus,
    CodexQuotaWindow,
    _parse_quota_response,
    _read_bearer_token,
    check_codex_quota,
    codex_quota_block_reason,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


# --- Token reading ---


def test_read_token_success(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok-abc"}}))
    assert _read_bearer_token(auth) == "tok-abc"


def test_read_token_missing_file(tmp_path):
    assert _read_bearer_token(tmp_path / "nope.json") is None


def test_read_token_bad_json(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("not json")
    assert _read_bearer_token(auth) is None


def test_read_token_missing_field(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {}}))
    assert _read_bearer_token(auth) is None


# --- Response parsing ---


def _make_api_response(
    limit_reached=False,
    primary_pct=50.0,
    secondary_pct=20.0,
    primary_reset="2026-04-08T05:00:00Z",
    secondary_reset="2026-04-13T00:00:00Z",
):
    return {
        "rate_limit": {
            "limit_reached": limit_reached,
            "windows": [
                {
                    "type": "primary_5h",
                    "used_percent": primary_pct,
                    "reset_at": primary_reset,
                },
                {
                    "type": "secondary_weekly",
                    "used_percent": secondary_pct,
                    "reset_at": secondary_reset,
                },
            ],
        }
    }


def test_parse_limit_not_reached():
    status = _parse_quota_response(_make_api_response(limit_reached=False))
    assert status.limit_reached is False
    assert status.primary_window.used_percent == 50.0
    assert status.secondary_window.used_percent == 20.0
    assert status.primary_window.reset_at == "2026-04-08T05:00:00Z"
    assert status.secondary_window.reset_at == "2026-04-13T00:00:00Z"
    assert status.max_used_percent == 50.0
    assert status.earliest_reset_at == "2026-04-13T00:00:00Z"


def test_parse_limit_reached():
    status = _parse_quota_response(_make_api_response(limit_reached=True, primary_pct=100.0))
    assert status.limit_reached is True
    assert status.max_used_percent == 100.0


def test_parse_empty_response():
    status = _parse_quota_response({})
    assert status.limit_reached is False
    assert status.primary_window.used_percent == 0.0
    assert status.secondary_window.used_percent == 0.0


# --- Quota check with caching ---


def test_check_quota_limit_reached(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    def fake_fetch(token):
        return _parse_quota_response(_make_api_response(limit_reached=True, primary_pct=100.0))

    status = check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    assert status.limit_reached is True


def test_check_quota_not_reached(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    def fake_fetch(token):
        return _parse_quota_response(_make_api_response(limit_reached=False, primary_pct=30.0))

    status = check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    assert status.limit_reached is False


def test_check_quota_cache(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    call_count = 0

    def fake_fetch(token):
        nonlocal call_count
        call_count += 1
        return _parse_quota_response(_make_api_response())

    check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    assert call_count == 1  # second call used cache


def test_check_quota_cache_expires(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    call_count = 0

    def fake_fetch(token):
        nonlocal call_count
        call_count += 1
        return _parse_quota_response(_make_api_response())

    check_codex_quota(auth_path=auth, cache_ttl=0, _fetch=fake_fetch)
    check_codex_quota(auth_path=auth, cache_ttl=0, _fetch=fake_fetch)
    assert call_count == 2  # cache expired immediately


def test_check_quota_no_auth(tmp_path):
    status = check_codex_quota(auth_path=tmp_path / "nope.json")
    assert status.error == "no auth token"
    assert status.limit_reached is False


# --- Block reason ---


def test_block_reason_limit_reached(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    def fake_fetch(token):
        return _parse_quota_response(
            _make_api_response(
                limit_reached=True,
                primary_pct=100.0,
                primary_reset="2026-04-08T05:00:00Z",
            )
        )

    reason = codex_quota_block_reason(auth_path=auth, _fetch=fake_fetch)
    assert reason is not None
    assert "codex quota exhausted" in reason
    assert "100%" in reason
    assert "2026-04" in reason


def test_block_reason_not_reached(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    def fake_fetch(token):
        return _parse_quota_response(_make_api_response(limit_reached=False))

    reason = codex_quota_block_reason(auth_path=auth, _fetch=fake_fetch)
    assert reason is None


def test_block_reason_fail_open(tmp_path):
    """When API call fails, codex should not be blocked (fail-open)."""
    reason = codex_quota_block_reason(auth_path=tmp_path / "nope.json")
    assert reason is None


# --- Monitoring integration ---


def test_monitoring_records_quota(tmp_path):
    from litehive.models import WorkspaceEngineMonitoring
    from litehive.observability._engine_monitoring import (
        load_engine_monitoring,
        record_codex_quota_check,
        save_engine_monitoring,
    )

    litehive_dir = tmp_path / ".litehive"
    litehive_dir.mkdir()
    (litehive_dir / ".gitignore").write_text("")

    save_engine_monitoring(tmp_path, WorkspaceEngineMonitoring())

    status = CodexQuotaStatus(
        limit_reached=True,
        primary_window=CodexQuotaWindow(used_percent=95.0, reset_at="2026-04-08T05:00:00Z"),
        secondary_window=CodexQuotaWindow(used_percent=40.0, reset_at="2026-04-13T00:00:00Z"),
        checked_at=time.monotonic(),
    )
    record_codex_quota_check(tmp_path, status=status)

    monitoring = load_engine_monitoring(tmp_path)
    record = monitoring.engines.get("codex")
    assert record is not None
    assert record.usage is not None
    assert record.usage.used == 95
    assert record.usage.limit == 100
    assert record.usage.remaining == 5
    assert record.usage.unit == "percent"
    assert record.usage.reset_at == "2026-04-13T00:00:00Z"
    assert record.last_limit_kind == "quota"
    assert record.metadata.get("primary_used_percent") == 95
    assert record.metadata.get("secondary_used_percent") == 40


def test_monitoring_skips_error_status(tmp_path):
    from litehive.models import WorkspaceEngineMonitoring
    from litehive.observability._engine_monitoring import (
        load_engine_monitoring,
        record_codex_quota_check,
        save_engine_monitoring,
    )

    litehive_dir = tmp_path / ".litehive"
    litehive_dir.mkdir()
    (litehive_dir / ".gitignore").write_text("")

    save_engine_monitoring(tmp_path, WorkspaceEngineMonitoring())

    status = CodexQuotaStatus(checked_at=time.monotonic(), error="network error")
    record_codex_quota_check(tmp_path, status=status)

    monitoring = load_engine_monitoring(tmp_path)
    assert "codex" not in monitoring.engines
