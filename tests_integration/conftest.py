from pathlib import Path

import pytest

from .helpers import integration_workspace


@pytest.fixture
def integration_root(tmp_path: Path) -> Path:
    return integration_workspace(tmp_path)
