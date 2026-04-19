"""Shared test fixtures."""

import os
from pathlib import Path

import pytest

import heru.quota.codex_quota as _codex_quota_mod


# Skip fsync in tests — saves ~70% of file write time
os.environ["LITEHIVE_SKIP_FSYNC"] = "1"

# The sandbox exposes git plumbing under /lib/git-core. Prepend it so
# subprocess-based git tests are stable under `uv run pytest`.
_GIT_CORE_DIR = "/lib/git-core"
if Path(_GIT_CORE_DIR).is_dir():
    current_path = os.environ.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    if _GIT_CORE_DIR not in path_entries:
        os.environ["PATH"] = os.pathsep.join([_GIT_CORE_DIR, *path_entries]) if current_path else _GIT_CORE_DIR
    os.environ.setdefault("GIT_EXEC_PATH", _GIT_CORE_DIR)

# Several git tests assume `git init` defaults to `main`.
os.environ["GIT_CONFIG_COUNT"] = "1"
os.environ["GIT_CONFIG_KEY_0"] = "init.defaultBranch"
os.environ["GIT_CONFIG_VALUE_0"] = "main"


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
