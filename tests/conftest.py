"""Shared test fixtures."""

import importlib
import os
from pathlib import Path
import sys
import tempfile

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_heru_import_path() -> None:
    candidates = [
        *sorted((_REPO_ROOT / ".venv" / "lib").glob("python*/site-packages")),
        *sorted((_REPO_ROOT / "packages").glob("heru-*.whl")),
    ]
    for candidate in reversed(candidates):
        entry = str(candidate)
        if entry not in sys.path:
            sys.path.insert(0, entry)


_bootstrap_heru_import_path()
_codex_quota_mod = importlib.import_module("heru.quota.codex_quota")

_PREVIOUS_TEST_ENV = {
    key: os.environ.get(key)
    for key in ("LITEHIVE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")
}
_TEST_XDG_ROOT = Path(tempfile.mkdtemp(prefix="litehive-test-xdg-"))
_TEST_XDG_PATHS = {
    "XDG_CONFIG_HOME": _TEST_XDG_ROOT / "config",
    "XDG_DATA_HOME": _TEST_XDG_ROOT / "data",
    "XDG_STATE_HOME": _TEST_XDG_ROOT / "state",
}
os.environ.pop("LITEHIVE_HOME", None)
for _key, _value in _TEST_XDG_PATHS.items():
    os.environ[_key] = str(_value)
    _value.mkdir(parents=True, exist_ok=True)


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

_PYTHONPATH_ENTRIES = [str(_REPO_ROOT)]
_SITE_PACKAGES = _REPO_ROOT / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
if _SITE_PACKAGES.exists():
    _PYTHONPATH_ENTRIES.append(str(_SITE_PACKAGES.resolve()))
_HERU_IMPORT_ROOT = Path(_codex_quota_mod.__file__).resolve().parents[2]
_PYTHONPATH_ENTRIES.append(str(_HERU_IMPORT_ROOT))
for entry in reversed(_PYTHONPATH_ENTRIES):
    if entry not in sys.path:
        sys.path.insert(0, entry)
current_pythonpath = os.environ.get("PYTHONPATH", "")
pythonpath_entries = current_pythonpath.split(os.pathsep) if current_pythonpath else []
for entry in reversed(_PYTHONPATH_ENTRIES):
    if entry not in pythonpath_entries:
        pythonpath_entries.insert(0, entry)
os.environ["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

_codex_quota_mod = importlib.import_module("heru.quota.codex_quota")


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

        monkeypatch.setattr(models_mod, "engine_quota_block", _noop_engine_quota_block)
    except (ImportError, AttributeError):
        pass
    yield
    _codex_quota_mod.reset_cache()


@pytest.fixture(scope="session", autouse=True)
def _use_session_xdg_dirs():
    yield
    for key, value in _PREVIOUS_TEST_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
