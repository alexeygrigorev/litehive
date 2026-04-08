from pathlib import Path

import pytest

from .helpers import integration_workspace


@pytest.fixture(autouse=True)
def _integration_timeout(request):
    """Integration tests get a tight 30s guardrail per test."""
    request.node.timeout = 30


@pytest.fixture
def integration_root(tmp_path: Path) -> Path:
    return integration_workspace(tmp_path)


@pytest.fixture(scope="module")
def module_integration_root(tmp_path_factory: pytest.TempPathFactory, request) -> Path:
    return integration_workspace(tmp_path_factory.mktemp(request.module.__name__))
