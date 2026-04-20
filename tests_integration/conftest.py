import os
from pathlib import Path

import pytest

from tests_integration.support.helpers import integration_workspace, sandboxed_integration_workspace


@pytest.fixture(autouse=True)
def _integration_timeout(request):
    """Integration tests get a tight 30s guardrail per test."""
    request.node.timeout = 30


@pytest.fixture(scope="session", autouse=True)
def _use_session_xdg_dirs(tmp_path_factory: pytest.TempPathFactory):
    xdg_root = tmp_path_factory.mktemp("xdg-home-integration")
    paths = {
        "XDG_CONFIG_HOME": xdg_root / "config",
        "XDG_DATA_HOME": xdg_root / "data",
        "XDG_STATE_HOME": xdg_root / "state",
    }
    previous = {key: os.environ.get(key) for key in paths}
    real_paths = {
        "XDG_CONFIG_HOME": previous["XDG_CONFIG_HOME"] or str(Path.home() / ".config"),
        "XDG_DATA_HOME": previous["XDG_DATA_HOME"] or str(Path.home() / ".local" / "share"),
        "XDG_STATE_HOME": previous["XDG_STATE_HOME"] or str(Path.home() / ".local" / "state"),
    }
    for key, value in paths.items():
        os.environ[key] = str(value)
        value.mkdir(parents=True, exist_ok=True)
    for key, value in real_paths.items():
        os.environ[f"LITEHIVE_REAL_{key}"] = value

    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    for key in real_paths:
        os.environ.pop(f"LITEHIVE_REAL_{key}", None)


@pytest.fixture
def integration_root(tmp_path: Path) -> Path:
    return integration_workspace(tmp_path)


@pytest.fixture(scope="module")
def module_integration_root(tmp_path_factory: pytest.TempPathFactory, request) -> Path:
    return integration_workspace(tmp_path_factory.mktemp(request.module.__name__))


@pytest.fixture
def sandboxed_integration_root(tmp_path: Path) -> Path:
    return sandboxed_integration_workspace(tmp_path)
