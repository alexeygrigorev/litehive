"""Tests for the ``LITEHIVE_PIPELINE_V2`` env var opt-in.

Two tests: one for the mode parser, one for the run.py short-circuit.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from litehive.cli.run import _maybe_run_single_v2, _v2_pipeline_mode


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("", None),
        ("1", "real"),
        ("true", "real"),
        ("stub", "stub"),
        ("dry", "stub"),
        ("bogus", None),
    ],
)
def test_v2_pipeline_mode_parses_env_values(env_value, expected, monkeypatch) -> None:
    monkeypatch.setenv("LITEHIVE_PIPELINE_V2", env_value)
    assert _v2_pipeline_mode() == expected


def test_maybe_run_single_v2_returns_none_when_env_var_unset(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LITEHIVE_PIPELINE_V2", raising=False)
    # Fall-through signal: the run.py path should skip v2 and fall back to v1.
    assert _maybe_run_single_v2(tmp_path) is None
