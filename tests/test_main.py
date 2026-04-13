import sys

import pytest

import litehive.main as main_module


def test_main_rewrites_agent_report_compat_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_cli_main() -> int:
        captured["argv"] = list(sys.argv)
        return 7

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setattr("litehive.cli.app.main", fake_cli_main)
    monkeypatch.setattr(
        sys,
        "argv",
        ["litehive", "report", "--verdict", "pass", "--message", "ok"],
    )

    exit_code = main_module.main()

    assert exit_code == 7
    assert captured["argv"] == [
        "litehive",
        "agent",
        "report",
        "--verdict",
        "pass",
        "--message",
        "ok",
    ]


def test_main_allows_recovery_diagnostic_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_cli_main() -> int:
        captured["argv"] = list(sys.argv)
        return 9

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "recovery")
    monkeypatch.setattr("litehive.cli.app.main", fake_cli_main)
    monkeypatch.setattr(
        sys,
        "argv",
        ["litehive", "pipeline", "journal", "T-0001"],
    )

    exit_code = main_module.main()

    assert exit_code == 9
    assert captured["argv"] == ["litehive", "pipeline", "journal", "T-0001"]


def test_main_blocks_non_recovery_diagnostic_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setattr(
        sys,
        "argv",
        ["litehive", "pipeline", "journal", "T-0001"],
    )

    exit_code = main_module.main()

    assert exit_code == 1
    assert "You are not authorized to perform this command." in capsys.readouterr().out
