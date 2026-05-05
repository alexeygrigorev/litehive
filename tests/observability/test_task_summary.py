from pathlib import Path
from types import SimpleNamespace

from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.pool import DirtyWorktreeFinding, DirtyWorktreeGateReport
from litehive.domain.reports import SEMANTIC_REJECT_CLASSIFICATION, StageReport
from litehive.domain.runtime import RunnerStatusState, RuntimeSubagentState
from litehive.domain.task import WorkspaceState
from litehive.observability.status import (
    collect_task_pipeline_status,
    render_active_task_detail_lines,
    render_full_status_header_lines,
    render_health_active_task_lines,
    render_health_daemon_lines,
    render_health_flagged_task_lines,
    render_health_quota_lines,
    render_health_recent_completion_lines,
    render_health_worktree_finding_lines,
    render_health_worktree_lines,
    render_engine_availability_lines,
    render_recent_activity_section,
    render_runner_status_line,
    render_runtime_policy_lines,
    render_task_summary,
)
from litehive.state.records import create_task
from litehive.tasks.report_storage import record_stage_report
from litehive.workspace import Workspace
from litehive.domain.common import PipelineStatus, TaskStatus


def test_render_task_summary_includes_estimate_velocity_and_eta(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Estimate demo task")

    record_stage_report(Workspace.from_path(tmp_path),
        task,
        StageReport(
            task_id=task.id,
            pipeline_state="grooming",
            verdict="pass",
            summary="ok",
            duration_seconds=120,
        ),
    )

    task.pipeline_status = PipelineStatus.IMPLEMENTING
    lines = render_task_summary(task, active=True, workspace=Workspace.from_path(tmp_path))
    combined = "\n".join(lines)
    assert "stage_estimate=" in combined
    assert "velocity=" in combined
    assert "eta=" in combined


def test_render_task_summary_surfaces_semantic_reject_classification(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Semantic reject status")
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.FLAGGED
    task.flag_reason = SEMANTIC_REJECT_CLASSIFICATION
    record_stage_report(Workspace.from_path(tmp_path),
        task,
        StageReport(
            task_id=task.id,
            pipeline_state="accepting",
            verdict="reject",
            summary="acceptance evidence is incomplete",
            failure_classification=SEMANTIC_REJECT_CLASSIFICATION,
        ),
    )

    lines = render_task_summary(task, active=False, workspace=Workspace.from_path(tmp_path))
    combined = "\n".join(lines)

    assert "flag_reason=semantic_reject" in combined
    assert "last_report_failure_classification=semantic_reject" in combined


def test_render_active_task_detail_lines_prefers_active_subagent_engine(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Active detail task")
    task.status = TaskStatus.IN_PROGRESS
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    task.runtime.pipeline.current_stage.stage = "testing"
    task.runtime.execution.active_subagent = RuntimeSubagentState(
        id="sa-1",
        role="implementer",
        engine="codex",
        status="running",
        path="/tmp/sa-1",
        started_at="2026-04-14T10:00:00Z",
        updated_at="2026-04-14T10:00:00Z",
    )

    lines = render_active_task_detail_lines(task, "claude")

    assert lines == [
        "active_task_title: Active detail task",
        "active_task_status: in_progress/implementing",
        "active_stage: testing",
        "active_engine: codex",
    ]


def test_collect_task_pipeline_status_prefers_runner_active_task_id(tmp_path: Path, monkeypatch) -> None:
    snapshot = SimpleNamespace(
        config=LitehiveConfig(default_engine="codex"),
        state=WorkspaceState(active_task_id=None, queue=["T-0382"]),
        runner=RunnerStatusState(
            status="running",
            pid=123,
            started_at="2026-04-16T03:15:43Z",
            heartbeat_at="2026-04-16T03:21:53Z",
            active_task_id="T-0381",
        ),
        monitoring=WorkspaceEngineMonitoring(),
        issues=[],
    )
    active_task = SimpleNamespace(id="T-0381", title="Move stage and recovery reports off YAML storage")

    # ``collect_task_pipeline_status`` imports its callees at module scope, so
    # patching the callees at their original locations no longer hits the
    # bindings the function uses — patch the local re-imports here.
    monkeypatch.setattr(
        "litehive.observability.status.collect_operational_status_snapshot",
        lambda root: snapshot,
    )
    monkeypatch.setattr(
        "litehive.observability.status.waiting_for_you_lines",
        lambda root, **_: ["operator_needed: unavailable"],
    )
    monkeypatch.setattr(
        "litehive.observability.status.get_task",
        lambda root, task_id: active_task if task_id else None,
    )

    status = collect_task_pipeline_status(tmp_path)

    assert status.active_task_id == "T-0381"
    assert status.active_task is active_task
    assert status.queue_head == "T-0382"
    assert status.waiting_lines == ["operator_needed: unavailable"]
    assert status.fast_runner_status == "running"


def test_render_runner_status_and_full_header_lines(tmp_path: Path) -> None:
    config = LitehiveConfig(
        default_engine="codex",
        litehive_source_path="/src/litehive",
        engine_freeze={"gemini": "2099-06-15T00:00:00Z"},
    )
    state = WorkspaceState(active_task_id="T-0001", queue=["T-0002", "T-0003"])
    runner = RunnerStatusState(
        status="running",
        pid=123,
        started_at="2026-04-14T10:00:00Z",
        heartbeat_at="2026-04-14T10:01:00Z",
        active_task_id="T-0001",
    )

    runner_line = render_runner_status_line(runner)
    lines = render_full_status_header_lines(tmp_path, config, state, runner)

    assert runner_line == (
        "runner_status: running pid=123 "
        "started_at=2026-04-14T10:00:00Z "
        "heartbeat_at=2026-04-14T10:01:00Z "
        "active_task_id=T-0001"
    )
    assert lines[0] == f"workspace: {tmp_path}"
    assert "status_read_mode: full" in lines
    assert "default_engine: codex" in lines
    assert "litehive_source_path: /src/litehive" in lines
    assert "active_task_id: T-0001" in lines
    assert runner_line in lines
    assert "queued_tasks: 2" in lines
    assert "pool_stop_reason: None" in lines
    assert len(lines) == 9


def test_render_full_status_header_prefers_live_runner_active_task_id(tmp_path: Path) -> None:
    config = LitehiveConfig(default_engine="codex", litehive_source_path="/src/litehive")
    state = WorkspaceState(active_task_id=None, queue=["T-0002"])
    runner = RunnerStatusState(
        status="running",
        pid=123,
        started_at="2026-04-14T10:00:00Z",
        heartbeat_at="2026-04-14T10:01:00Z",
        active_task_id="T-0381",
    )

    lines = render_full_status_header_lines(tmp_path, config, state, runner)

    assert "active_task_id: T-0381" in lines


def test_render_runtime_policy_lines_uses_preformatted_retry_label() -> None:
    config = LitehiveConfig(
        default_retry_limit=5,
        pool_stop_on_failure=True,
        pool_max_tasks=7,
        pool_stop_on_dirty_git=True,
        pool_stop_on_attention=True,
        process_profile="python",
    )

    lines = render_runtime_policy_lines(config, "timeout, network")

    assert lines == [
        "default_retry_limit: 5",
        "retry_on: timeout, network",
        "pool_stop_on_failure: True",
        "pool_max_tasks: 7",
        "pool_stop_on_dirty_git: True",
        "pool_stop_on_attention: True",
        "process_profile: python",
    ]


def test_render_health_task_sections(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active health task")
    active.status = TaskStatus.IN_PROGRESS
    active.pipeline_status = PipelineStatus.IMPLEMENTING
    active.runtime.pipeline.current_stage.stage = "testing"

    flagged = create_task(tmp_path, title="Flagged health task")
    flagged.status = TaskStatus.FLAGGED
    flagged.pipeline_status = PipelineStatus.TESTING
    flagged.flag_reason = "needs review"
    record_stage_report(Workspace.from_path(tmp_path),
        flagged,
        StageReport(task_id=flagged.id, pipeline_state="testing", verdict="reject", summary="missing evidence"),
    )

    done = create_task(tmp_path, title="Done health task")
    done.status = TaskStatus.DONE
    done.updated_at = "2026-04-14T10:15:00Z"
    record_stage_report(Workspace.from_path(tmp_path),
        done,
        StageReport(task_id=done.id, pipeline_state="accepting", verdict="pass", summary="all checks passed"),
    )

    workspace = Workspace.from_path(tmp_path)
    active_lines = render_health_active_task_lines(active)
    flagged_lines = render_health_flagged_task_lines([flagged], workspace=workspace)
    completion_lines = render_health_recent_completion_lines([done], workspace=workspace)

    assert active_lines == [
        "=== Active Task ===",
        "active_task: T-0001 [in_progress/implementing] stage=testing title=Active health task",
    ]
    assert flagged_lines == [
        "=== Flagged Tasks ===",
        "flagged_count: 1",
        "flagged: T-0002 stage=testing reason=needs review last_verdict=reject summary=missing evidence",
    ]
    assert completion_lines == [
        "=== Recent Completions ===",
        "completed: T-0003 title=Done health task when=2026-04-14T10:15:00Z summary=all checks passed",
    ]


def test_render_health_worktree_and_quota_sections() -> None:
    worktrees = [
        SimpleNamespace(
            task_id="T-0004",
            status="done",
            change_count=2,
            active=False,
            worktree_rel=".worktrees/T-0004-demo",
        )
    ]
    dirty_report = DirtyWorktreeGateReport(
        findings=[
            DirtyWorktreeFinding(
                location_kind="task-worktree",
                ownership="task-owned-worktree",
                task_id="T-0004",
                worktree_path=".worktrees/T-0004-demo",
                dirty_paths=["src/app.py", "README.md"],
            )
        ]
    )
    quota_health = [SimpleNamespace(engine="codex", status="ok", summary="90% remaining")]

    worktree_lines = render_health_worktree_lines(worktrees)
    finding_lines = render_health_worktree_finding_lines(dirty_report)
    quota_lines = render_health_quota_lines(quota_health)

    assert worktree_lines == [
        "=== Worktrees ===",
        "worktree_count: 1",
        "worktree: T-0004 status=done changes=2 active=no path=.worktrees/T-0004-demo",
    ]
    assert finding_lines == [
        "=== Worktree Findings ===",
        "finding: location=task-worktree ownership=task-owned-worktree task_id=T-0004 path=.worktrees/T-0004-demo dirty_paths=src/app.py,README.md",
    ]
    assert quota_lines == [
        "=== Engine Quotas ===",
        "quota: codex status=ok summary=90% remaining",
    ]


def test_render_engine_availability_lines_are_minimal_routing_signal() -> None:
    config = LitehiveConfig(default_engine="codex", engine_preference=["claude"])
    monitoring = WorkspaceEngineMonitoring(
        engines={
            "codex": {
                "engine": "codex",
                "last_limit_kind": "quota",
                "usage": {"reset_at": "2026-04-30T18:00:00Z"},
            }
        }
    )

    lines = render_engine_availability_lines(config, monitoring)

    assert lines == [
        "engine_available: claude status=available",
        "engine_available: codex status=quota default=yes",
    ]


def test_render_recent_activity_section_uses_canonical_stage_key() -> None:
    lines = render_recent_activity_section(
        [
            {
                "ts": "2026-04-23T10:00:00Z",
                "task_id": "T-0001",
                "kind": "stage_started",
                "data": {"stage": "implementing", "role": "swe"},
            }
        ]
    )

    assert lines == [
        "=== Recent Activity ===",
        "  [2026-04-23T10:00:00Z] T-0001 stage started implementing swe",
    ]


def test_render_health_daemon_lines() -> None:
    assert render_health_daemon_lines("running", "4242") == [
        "=== Daemon ===",
        "daemon_status: running",
        "daemon_pid: 4242",
    ]
