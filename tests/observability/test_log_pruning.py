import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from litehive.daemon.logs import prune_run_all_log_dirs


def test_prune_log_dirs_logs_on_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    log_base = tmp_path / "logs" / "run-all"
    log_base.mkdir(parents=True)
    for name in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (log_base / name).mkdir()

    with patch("litehive.daemon.logs.shutil.rmtree", side_effect=OSError("permission denied")):
        with caplog.at_level(logging.WARNING, logger="litehive.daemon.logs"):
            prune_run_all_log_dirs(log_base, keep=1)

    assert caplog.text.count("Failed to prune log dir") == 2
    assert "permission denied" in caplog.text


def test_prune_log_dirs_removes_old(tmp_path: Path) -> None:
    log_base = tmp_path / "logs" / "run-all"
    log_base.mkdir(parents=True)
    for name in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (log_base / name).mkdir()

    prune_run_all_log_dirs(log_base, keep=1)

    remaining = sorted(p.name for p in log_base.iterdir())
    assert remaining == ["2026-01-03"]

