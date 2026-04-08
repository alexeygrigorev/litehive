import os
from pathlib import Path

import pytest

from .helpers import INTEGRATION_ENV, integration_workspace


def pytest_addoption(parser):
    parser.addoption(
        "--engine",
        action="append",
        default=[],
        help="Enable integration tests for this engine (repeatable). E.g. --engine codex --engine claude",
    )
    parser.addoption(
        "--all-engines",
        action="store_true",
        default=False,
        help="Enable integration tests for all engines.",
    )


def pytest_configure(config):
    engines = config.getoption("engine", [])
    all_engines = config.getoption("all_engines", False)
    if all_engines:
        os.environ[INTEGRATION_ENV] = "codex,claude,copilot,gemini,opencode,goz"
    elif engines:
        os.environ[INTEGRATION_ENV] = ",".join(engines)


@pytest.fixture
def integration_root(tmp_path: Path) -> Path:
    return integration_workspace(tmp_path)
