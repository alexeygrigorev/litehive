from pathlib import Path

from litehive.cli.workspace import _health_daemon_status


def test_health_daemon_status_defaults_to_stopped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("litehive.cli.workspace.daemon_metadata", lambda root: None)

    assert _health_daemon_status(tmp_path) == ("stopped", "-")


def test_health_daemon_status_reports_running_pid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "litehive.cli.workspace.daemon_metadata",
        lambda root: {"status": "running", "pid": 4242},
    )

    assert _health_daemon_status(tmp_path) == ("running", "4242")
