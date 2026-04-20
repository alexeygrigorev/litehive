import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

from heru.quota import UsageStatus, UsageWindow

from litehive.config.workspace import ensure_workspace
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import WorkspaceState

_WEB_ROOT = Path(__file__).resolve().parents[2] / "litehive-web"
if str(_WEB_ROOT) not in sys.path:
    sys.path.insert(0, str(_WEB_ROOT))


def test_litehive_web_import_and_dashboard_use_unified_hours_weeks(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)

    import litehive_web

    snapshot = importlib.import_module("litehive_web.snapshot")
    status = UsageStatus(
        short_term=UsageWindow(percent_remaining=80.0, reset_at="2026-04-20T10:00:00Z"),
        long_term=UsageWindow(percent_remaining=35.0, reset_at="2026-04-27T00:00:00Z"),
    )
    monkeypatch.setattr(snapshot, "check_codex_quota", lambda: status)
    monkeypatch.setattr(snapshot, "check_claude_quota", lambda: status)
    monkeypatch.setattr(snapshot, "check_copilot_quota", lambda: status)
    monkeypatch.setattr(snapshot, "check_zai_quota", lambda: status)

    dashboard = litehive_web.read_engine_dashboard(tmp_path)
    codex = dashboard["quota"]["engines"]["codex"]

    assert [window["label"] for window in codex["windows"]] == ["hours", "weeks"]
    assert codex["summary"] == "hours remaining 80%, weeks remaining 35%"


def test_build_workspace_snapshot_uses_canonical_task_pipeline_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ensure_workspace(tmp_path)

    import litehive_web

    snapshot = importlib.import_module("litehive_web.snapshot")
    status = SimpleNamespace(
        state=WorkspaceState(active_task_id=None, queue=["T-0382"]),
        runner=RunnerStatusState(status="late", active_task_id="T-0381"),
        active_task_id="T-0381",
        active_task=SimpleNamespace(id="T-0381"),
    )
    task = SimpleNamespace(id="T-0381")
    captured: dict[str, object] = {}
    serialize_calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(snapshot, "collect_task_pipeline_status", lambda root: status)
    monkeypatch.setattr(
        snapshot,
        "load_state",
        lambda root: (_ for _ in ()).throw(AssertionError("build_workspace_snapshot should use collect_task_pipeline_status")),
    )

    def fake_list_tasks_state_first(root: Path, *, state: WorkspaceState, include_runtime: bool):
        captured["state"] = state
        captured["include_runtime"] = include_runtime
        return [task]

    def fake_serialize_task(root: Path, item: object, active_task_id: str | None) -> dict[str, object]:
        item_id = getattr(item, "id")
        serialize_calls.append((item_id, active_task_id))
        return {"id": item_id, "active_task_id": active_task_id}

    monkeypatch.setattr(snapshot, "list_tasks_state_first", fake_list_tasks_state_first)
    monkeypatch.setattr(snapshot, "serialize_task", fake_serialize_task)
    monkeypatch.setattr(snapshot, "build_daemon_status_payload", lambda root: {"daemon_status": "stopped"})
    monkeypatch.setattr(snapshot, "list_recent_run_all_logs", lambda root: [])
    monkeypatch.setattr(snapshot, "read_iso_now", lambda: "2026-04-21T12:00:00Z")

    payload = litehive_web.build_workspace_snapshot(tmp_path)

    assert captured == {"state": status.state, "include_runtime": True}
    assert payload["runner"]["status"] == "late"
    assert payload["active_task_id"] == "T-0381"
    assert payload["active_task"] == {"id": "T-0381", "active_task_id": "T-0381"}
    assert payload["tasks"] == [{"id": "T-0381", "active_task_id": "T-0381"}]
    assert serialize_calls == [("T-0381", "T-0381"), ("T-0381", "T-0381")]
