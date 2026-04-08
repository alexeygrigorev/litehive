from pathlib import Path

import pytest

from .helpers import integration_workspace


@pytest.fixture(autouse=True)
def _integration_timeout(request):
    """Integration tests get 180s instead of the global 60s."""
    request.node.timeout = 180


@pytest.fixture
def integration_root(tmp_path: Path) -> Path:
    return integration_workspace(tmp_path)
