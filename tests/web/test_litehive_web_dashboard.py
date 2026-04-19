import importlib
import sys
from pathlib import Path

from heru.quota import UsageStatus, UsageWindow

from litehive.config.workspace import ensure_workspace

_WEB_ROOT = Path(__file__).resolve().parents[2] / "litehive-web"
if str(_WEB_ROOT) not in sys.path:
    sys.path.insert(0, str(_WEB_ROOT))


def test_litehive_web_import_and_dashboard_use_unified_hours_weeks(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)

    import litehive_web

    snapshot = importlib.import_module("litehive_web.snapshot")
    status = UsageStatus(
        hours=UsageWindow(percent_remaining=80.0, reset_at="2026-04-20T10:00:00Z"),
        weeks=UsageWindow(percent_remaining=35.0, reset_at="2026-04-27T00:00:00Z"),
    )
    monkeypatch.setattr(snapshot, "check_codex_quota", lambda: status)
    monkeypatch.setattr(snapshot, "check_claude_quota", lambda: status)
    monkeypatch.setattr(snapshot, "check_copilot_quota", lambda: status)
    monkeypatch.setattr(snapshot, "check_zai_quota", lambda: status)

    dashboard = litehive_web.read_engine_dashboard(tmp_path)
    codex = dashboard["quota"]["engines"]["codex"]

    assert [window["label"] for window in codex["windows"]] == ["hours", "weeks"]
    assert codex["summary"] == "hours remaining 80%, weeks remaining 35%"
