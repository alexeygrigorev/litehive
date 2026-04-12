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
    monkeypatch.setattr("litehive.cli.main", fake_cli_main)
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
