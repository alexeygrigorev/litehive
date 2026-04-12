"""Shared test fixtures."""

import os
from pathlib import Path

import pytest
import heru.quota.codex_quota as _codex_quota_mod


# Skip fsync in tests — saves ~70% of file write time
os.environ["LITEHIVE_SKIP_FSYNC"] = "1"


def _noop_block_reason(**kw):
    return None


def _noop_check_quota(**kw):
    return _codex_quota_mod.UsageStatus(error="test-disabled")


def _noop_engine_quota_block(*args, **kwargs):
    return None, None


@pytest.fixture(autouse=True)
def _neutralize_codex_quota(request, monkeypatch):
    """Prevent real codex quota API calls during tests."""
    _codex_quota_mod.reset_cache()
    # Patch at the source module
    monkeypatch.setattr(_codex_quota_mod, "codex_quota_block_reason", _noop_block_reason)
    # Patch at import sites that did `from ... import codex_quota_block_reason`
    try:
        import litehive.cli.dry_run as dry_run_mod

        monkeypatch.setattr(dry_run_mod, "codex_quota_block_reason", _noop_block_reason)
    except (ImportError, AttributeError):
        pass
    try:
        import litehive.config.engine_models as models_mod

        monkeypatch.setattr(models_mod, "_engine_quota_block", _noop_engine_quota_block)
    except (ImportError, AttributeError):
        pass
    yield
    _codex_quota_mod.reset_cache()


@pytest.fixture(scope="session", autouse=True)
def _use_session_xdg_dirs(tmp_path_factory: pytest.TempPathFactory):
    xdg_root = tmp_path_factory.mktemp("xdg-home")
    paths = {
        "XDG_CONFIG_HOME": xdg_root / "config",
        "XDG_DATA_HOME": xdg_root / "data",
        "XDG_STATE_HOME": xdg_root / "state",
    }
    previous = {key: os.environ.get(key) for key in paths}
    for key, value in paths.items():
        os.environ[key] = str(value)
        Path(value).mkdir(parents=True, exist_ok=True)
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
