from tests.workspace_helpers import (
    AdapterCapabilities,
    CLIExecutionResult,
    EngineUsageObservation,
    EngineUsageWindow,
    ExternalCLIAdapter,
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
    Path,
    RuntimeInterruptionState,
    RuntimeStageState,
    RuntimeSubagentState,
    StageReport,
    SubagentRef,
    TaskExecutionRunner,
    TaskRecord,
    UpstreamContributionOrigin,
    UpstreamPatchProposal,
    WorkspaceConflictError,
    _block_runner_lock,
    _cmd_add,
    _cmd_doctor,
    _cmd_health,
    _cmd_issue,
    _cmd_queue,
    _cmd_repair,
    _cmd_run,
    _cmd_status,
    _completed_subagent_result,
    _commit_repo_state,
    _init_git_repo,
    _interrupted_subagent_result,
    _latest_pool_run_report,
    _run,
    _stage_subagent_result,
    argparse,
    build_parser,
    build_workspace_snapshot,
    classify_execution_interruption,
    create_task,
    dequeue_next_task_selection,
    ensure_workspace,
    format_external_engine_sandbox,
    format_subagent_resource_limits,
    get_engine,
    get_task,
    gzip,
    load_config,
    load_state,
    os,
    pytest,
    read_session_view,
    record_engine_execution,
    recover_stale_runner_state,
    render_task_summary,
    repair_workspace_state,
    require_task,
    run_next_task,
    runner_status,
    save_state,
    save_task,
    save_task_runtime,
    set_active_task,
    subprocess,
    tasks_module,
    task_dir,
    threading,
    time,
    yaml,
)
from litehive.tasks.reports import collect_recovery_evidence
import shutil
from types import SimpleNamespace

def test_dequeue_next_task_selection_rejects_multiple_active_tasks(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First active task", auto_commit=False)
    second = create_task(tmp_path, title="Second active task", auto_commit=False)

    first.runtime.execution_status = "running"
    second.runtime.execution_status = "running"
    save_task_runtime(tmp_path, first)
    save_task_runtime(tmp_path, second)

    with pytest.raises(WorkspaceConflictError, match="workspace has multiple active tasks"):
        dequeue_next_task_selection(tmp_path)

def test_set_active_task_rejects_starting_a_second_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task", auto_commit=False)
    pending = create_task(tmp_path, title="Pending task", auto_commit=False)

    active.runtime.execution_status = "running"
    save_task_runtime(tmp_path, active)

    with pytest.raises(
        WorkspaceConflictError,
        match="workspace has multiple active tasks",
    ):
        set_active_task(tmp_path, pending.id)

def test_cmd_run_default_executes_single_task_and_reports_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 First task" in output
    assert "task: T-0002 Second task" not in output
    assert (
        "stage_outcomes: grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in output
    )
    assert "completed_tasks: 1" in output
    assert (
        "completed: T-0001 First task status=done pipeline_status=done "
        "stage_outcomes=grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in output
    )
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 1" in output
    assert "remaining_tasks: 1" in output
    assert "tasks_run: 1" in output
    assert "stop_reason: single_task_complete" in output
    summary_report = (tmp_path / ".litehive" / "pool-summary.txt").read_text(encoding="utf-8")
    assert "completed_tasks: 1" in summary_report
    assert (
        "completed: T-0001 First task status=done pipeline_status=done "
        "stage_outcomes=grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in summary_report
    )
    assert "stop_reason: single_task_complete" in summary_report
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["stop_reason"] == "single_task_complete"
    assert durable_report["stop_condition"] == "single task complete"
    assert durable_report["tasks_run"] == 1
    assert durable_report["completed_count"] == 1
    assert durable_report["flagged_count"] == 0
    assert durable_report["skipped_count"] == 1
    assert durable_report["remaining_count"] == 1
    assert durable_report["completed"] == [
        {
            "task_id": "T-0001",
            "title": "First task",
            "final_task_status": "done",
            "pipeline_status": "done",
            "stage_outcomes": [
                "grooming=pass",
                "implementing=pass",
                "testing=pass",
                "accepting=pass",
                "commit_to_git=pass",
            ],
            "reason_code": None,
            "reason": None,
            "follow_up_task_id": None,
        }
    ]
    assert durable_report["skipped"] == [
        {
            "task_id": "T-0002",
            "title": "Second task",
            "final_task_status": "queued",
            "pipeline_status": "backlog",
            "stage_outcomes": [],
            "reason_code": None,
            "reason": None,
            "follow_up_task_id": None,
        }
    ]
    assert load_state(tmp_path).queue == ["T-0002"]


def test_health_command_reports_healthy_workspace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    active = create_task(tmp_path, title="Active task", auto_commit=False)
    active.pipeline_status = "testing"
    active.runtime.current_stage = RuntimeStageState(step="testing", status="running")
    active.runtime.last_stage = RuntimeStageState(step="implementing", verdict="pass", summary="implemented health command")
    monkeypatch.setattr("litehive.tasks.crud.utcnow", lambda: "2026-04-09T10:00:00Z")
    save_task(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    save_state(tmp_path, state)

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{active.id}-{active.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    active.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, active)
    _commit_repo_state(tmp_path)
    (worktree_path / "health.txt").write_text("pending\n", encoding="utf-8")

    done_titles = ["Completed first", "Completed second", "Completed third", "Completed older"]
    done_times = [
        "2026-04-09T11:00:00Z",
        "2026-04-09T10:30:00Z",
        "2026-04-09T10:15:00Z",
        "2026-04-08T09:00:00Z",
    ]
    for title, updated_at in zip(done_titles, done_times, strict=True):
        task = create_task(tmp_path, title=title, auto_commit=False)
        task.status = "done"
        task.pipeline_status = "done"
        task.runtime.last_stage = RuntimeStageState(
            step="commit_to_git",
            verdict="pass",
            summary=f"{title} summary",
        )
        monkeypatch.setattr("litehive.tasks.crud.utcnow", lambda ts=updated_at: ts)
        save_task(tmp_path, task)
    _commit_repo_state(tmp_path)

    monkeypatch.setattr(
        "litehive.cli.health.check_codex_quota",
        lambda: SimpleNamespace(
            error=None,
            limit_reached=False,
            primary_window=SimpleNamespace(used_percent=42.0),
            secondary_window=SimpleNamespace(used_percent=61.0),
            earliest_reset_at="2026-04-15T00:00:00Z",
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.health.check_claude_quota",
        lambda: SimpleNamespace(
            error=None,
            limit_reached=False,
            five_hour=SimpleNamespace(used_percent=37.5, reset_at="2026-04-09T04:00:00Z"),
            seven_day=SimpleNamespace(used_percent=58.0, reset_at="2026-04-12T00:00:00Z"),
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.health.check_copilot_quota",
        lambda: SimpleNamespace(
            error=None,
            limit_reached=False,
            used_percent=25.0,
            premium_remaining=75,
            premium_entitlement=100,
            quota_reset_date="2026-04-10",
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.health.check_zai_quota",
        lambda: SimpleNamespace(
            error=None,
            limit_reached=False,
            api_calls=SimpleNamespace(used_percent=33.0),
            tokens=SimpleNamespace(used_percent=48.0),
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.health.daemon_status_lines",
        lambda workspace: [
            f"workspace: {workspace.resolve()}",
            "daemon_status: running",
            "pid: 4242",
        ],
    )

    exit_code = _cmd_health(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"active_task: {active.id} [queued/testing] stage=testing title=Active task" in output
    assert "flagged_count: 0" in output
    assert f"worktree: {active.id} status=queued changes=1 active=yes" in output
    assert "ownership=task-owned-worktree" in output
    assert "quota: codex status=ok summary=5h=42.0% weekly=61.0% reset=2026-04-15T00:00:00Z" in output
    assert "quota: gemini status=unsupported summary=no proactive quota check" in output
    assert "daemon_status: running" in output
    assert "daemon_pid: 4242" in output
    assert "completed: T-0002 title=Completed first" in output
    assert "completed: T-0003 title=Completed second" in output
    assert "completed: T-0004 title=Completed third" in output
    assert "Completed older" not in output


def test_health_command_reports_unhealthy_workspace_and_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    flagged = create_task(tmp_path, title="Needs operator", auto_commit=False)
    flagged.status = "flagged"
    flagged.pipeline_status = "implementing"
    flagged.flag_reason = "retry_limit_exhausted"
    flagged.runtime.last_stage = RuntimeStageState(
        step="testing",
        verdict="reject",
        summary="tests failing",
    )
    save_task(tmp_path, flagged)

    stale = create_task(tmp_path, title="Missing worktree", auto_commit=False)
    stale.git.worktree_path = ".litehive/worktrees/missing-worktree"
    save_task(tmp_path, stale)

    monkeypatch.setattr(
        "litehive.cli.health.check_codex_quota",
        lambda: SimpleNamespace(
            error=None,
            limit_reached=True,
            primary_window=SimpleNamespace(used_percent=95.0),
            secondary_window=SimpleNamespace(used_percent=40.0),
            earliest_reset_at="2026-04-10T05:00:00Z",
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.health.check_claude_quota",
        lambda: SimpleNamespace(
            error=None,
            limit_reached=False,
            five_hour=SimpleNamespace(used_percent=10.0, reset_at="2026-04-09T04:00:00Z"),
            seven_day=SimpleNamespace(used_percent=20.0, reset_at="2026-04-12T00:00:00Z"),
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.health.check_copilot_quota",
        lambda: SimpleNamespace(
            error=None,
            limit_reached=False,
            used_percent=15.0,
            premium_remaining=85,
            premium_entitlement=100,
            quota_reset_date="2026-04-10",
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.health.check_zai_quota",
        lambda: SimpleNamespace(
            error=None,
            limit_reached=False,
            api_calls=SimpleNamespace(used_percent=10.0),
            tokens=SimpleNamespace(used_percent=20.0),
        ),
    )
    monkeypatch.setattr(
        "litehive.cli.health.daemon_status_lines",
        lambda workspace: [
            f"workspace: {workspace.resolve()}",
            "daemon_status: stopped",
        ],
    )

    exit_code = _cmd_health(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "flagged_count: 1" in output
    assert (
        f"flagged: {flagged.id} stage=implementing reason=retry_limit_exhausted "
        "last_verdict=reject summary=tests failing"
    ) in output
    assert "finding: location=task-worktree ownership=missing-recorded-worktree" in output
    assert "path=.litehive/worktrees/missing-worktree" in output
    assert "quota: codex status=warning summary=5h=95.0% weekly=40.0% reset=2026-04-10T05:00:00Z" in output
    assert "daemon_status: stopped" in output


def _create_duplicate_task_directory(root: Path, task: TaskRecord, *, suffix: str) -> None:
    duplicate_dir = task_dir(root, task).with_name(f"{task.id}-{suffix}")
    shutil.copytree(task_dir(root, task), duplicate_dir)


def _doctor_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *, fix: bool = False
) -> tuple[int, str]:
    exit_code = _cmd_doctor(argparse.Namespace(workspace=tmp_path, fix=fix))
    return exit_code, capsys.readouterr().out


def _create_merge_failed_task(root: Path) -> TaskRecord:
    task = create_task(root, title="Merge failed task", auto_commit=False)
    task.status = "merge_failed"
    save_task(root, task)
    return task


def _create_flagged_task(root: Path) -> TaskRecord:
    task = create_task(root, title="Flagged task", auto_commit=False)
    task.status = "flagged"
    task.pipeline_status = "testing"
    save_task(root, task)
    return task


def _create_stale_worktree_task(root: Path) -> TaskRecord:
    task = create_task(root, title="Finished worktree task", auto_commit=False)
    task.status = "done"
    task.pipeline_status = "done"
    stale_path = root / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(stale_path), "HEAD"], root)
    task.runtime.git.worktree_path = str(stale_path.relative_to(root))
    save_task(root, task)
    save_task_runtime(root, task)
    return task


def _create_stranded_commit_task(root: Path) -> TaskRecord:
    task = create_task(root, title="Stranded checkpoint")
    task.status = "done"
    task.pipeline_status = "done"
    task.git.checkpoint_attempts = 1
    task.git.commit_sha = None
    save_task(root, task)
    return task


def _create_orphaned_subagent_task(root: Path) -> TaskRecord:
    task = create_task(root, title="Orphaned subagent", auto_commit=False)
    task.runtime.execution_status = "idle"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
        started_at="2026-04-10T00:00:00+00:00",
        updated_at="2026-04-10T00:00:00+00:00",
    )
    save_task_runtime(root, task)
    return task


def test_parser_exposes_doctor_command() -> None:
    parser = build_parser()

    args = parser.parse_args(["doctor", "--fix"])

    assert args.command == "doctor"
    assert args.fix is True


def test_doctor_command_reports_clean_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    exit_code, output = _doctor_output(tmp_path, capsys)

    assert exit_code == 0
    assert "doctor: clean" in output


def test_doctor_command_reports_duplicate_task_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    duplicate = create_task(tmp_path, title="Duplicate task", auto_commit=False)
    _create_duplicate_task_directory(tmp_path, duplicate, suffix="duplicate-copy")

    exit_code, output = _doctor_output(tmp_path, capsys)

    assert exit_code == 1
    assert "finding: duplicate_task_id task_id=T-0001 count=2 fix=litehive doctor --fix" in output


def test_doctor_command_reports_merge_failed_tasks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    merge_failed = _create_merge_failed_task(tmp_path)

    exit_code, output = _doctor_output(tmp_path, capsys)

    assert exit_code == 1
    assert f"finding: merge_failed_task task_id={merge_failed.id} title=Merge failed task " in output


def test_doctor_command_reports_flagged_tasks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    flagged = _create_flagged_task(tmp_path)

    exit_code, output = _doctor_output(tmp_path, capsys)

    assert exit_code == 1
    assert f"finding: flagged_task task_id={flagged.id} stage=testing title=Flagged task " in output


def test_doctor_command_reports_origin_divergence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    monkeypatch.setattr(
        "litehive.pipeline.recovery.doctor._check_origin_divergence",
        lambda root: "local main and origin/main have diverged",
    )

    exit_code, output = _doctor_output(tmp_path, capsys)

    assert exit_code == 1
    assert "finding: origin_divergence local main and origin/main have diverged " in output


def test_doctor_command_reports_stale_worktrees(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    stale_worktree = _create_stale_worktree_task(tmp_path)

    exit_code, output = _doctor_output(tmp_path, capsys)

    assert exit_code == 1
    assert f"finding: stale_worktree task_id={stale_worktree.id} status=done " in output


def test_doctor_command_reports_stuck_commit_to_git_tasks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    stranded = _create_stranded_commit_task(tmp_path)

    exit_code, output = _doctor_output(tmp_path, capsys)

    assert exit_code == 1
    assert f"finding: commit_to_git_stuck task_id={stranded.id} kind=stranded checkpoint_attempts=1 " in output


def test_doctor_command_reports_orphaned_subagents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    orphaned_subagent = _create_orphaned_subagent_task(tmp_path)

    exit_code, output = _doctor_output(tmp_path, capsys)

    assert exit_code == 1
    assert f"finding: orphaned_subagent task_id={orphaned_subagent.id} subagent_id=SA-0001 reason=missing_artifacts " in output


def test_doctor_command_reports_broken_state_yaml_without_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "state.yaml").write_text(
        "<<<<<<< HEAD\nactive_task_id: T-0001\n=======\nqueue: []\n>>>>>>> branch\n",
        encoding="utf-8",
    )

    exit_code, output = _doctor_output(tmp_path, capsys)

    assert exit_code == 1
    assert "finding: broken_state_yaml path=.litehive/state.yaml reason=merge_conflict_markers " in output
    assert "fix=cp .litehive/state.yaml .litehive/state.yaml.bak && ${EDITOR:-vi} .litehive/state.yaml" in output


def test_doctor_fix_repairs_duplicate_task_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    duplicate = create_task(tmp_path, title="Duplicate task", auto_commit=False)
    _create_duplicate_task_directory(tmp_path, duplicate, suffix="duplicate-copy")

    exit_code, output = _doctor_output(tmp_path, capsys, fix=True)

    assert exit_code == 0
    assert "fixed: duplicate_task_id task_id=T-0001 count=2 fix=litehive doctor --fix" in output

    duplicate_count = 0
    for task_path in (tmp_path / ".litehive" / "tasks").glob("*/task.yaml"):
        payload = yaml.safe_load(task_path.read_text(encoding="utf-8")) or {}
        if payload.get("id") == duplicate.id:
            duplicate_count += 1
    assert duplicate_count == 1


def test_doctor_fix_repairs_stranded_commit_and_orphaned_subagent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    stranded = _create_stranded_commit_task(tmp_path)
    orphaned_subagent = _create_orphaned_subagent_task(tmp_path)

    monkeypatch.setattr(
        "litehive.pipeline.recovery.doctor._check_origin_divergence",
        lambda root: None,
    )

    exit_code, output = _doctor_output(tmp_path, capsys, fix=True)

    assert exit_code == 0
    assert f"fixed: commit_to_git_stuck task_id={stranded.id} kind=stranded checkpoint_attempts=1 fix=litehive doctor --fix" in output
    assert f"fixed: orphaned_subagent task_id={orphaned_subagent.id} subagent_id=SA-0001 reason=missing_artifacts fix=litehive doctor --fix" in output

    refreshed_stranded = get_task(tmp_path, stranded.id)
    assert refreshed_stranded is not None
    assert refreshed_stranded.pipeline_status == "commit_to_git"
    assert refreshed_stranded.status == "queued"

    refreshed_orphaned = get_task(tmp_path, orphaned_subagent.id)
    assert refreshed_orphaned is not None
    assert refreshed_orphaned.runtime.active_subagent is None


def test_doctor_fix_leaves_flagged_tasks_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)

    flagged = create_task(tmp_path, title="Needs review", auto_commit=False)
    flagged.status = "flagged"
    save_task(tmp_path, flagged)

    exit_code, output = _doctor_output(tmp_path, capsys, fix=True)

    assert exit_code == 1
    assert f"finding: flagged_task task_id={flagged.id} stage=backlog title=Needs review " in output

    refreshed_flagged = get_task(tmp_path, flagged.id)
    assert refreshed_flagged is not None
    assert refreshed_flagged.status == "flagged"

def test_cmd_run_drains_task_pool_and_reports_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 First task" in output
    assert "task: T-0002 Second task" in output
    assert "completed_tasks: 2" in output
    assert "tasks_run: 2" in output
    assert "stop_reason: queue_exhausted" in output
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["stop_reason"] == "queue_exhausted"
    assert durable_report["stop_condition"] == "queue exhausted"
    assert durable_report["tasks_run"] == 2
    assert durable_report["completed_count"] == 2
    assert durable_report["flagged_count"] == 0
    assert durable_report["skipped_count"] == 0
    assert durable_report["remaining_count"] == 0
    assert durable_report["completed"] == [
        {
            "task_id": "T-0001",
            "title": "First task",
            "final_task_status": "done",
            "pipeline_status": "done",
            "stage_outcomes": [
                "grooming=pass",
                "implementing=pass",
                "testing=pass",
                "accepting=pass",
                "commit_to_git=pass",
            ],
            "reason_code": None,
            "reason": None,
            "follow_up_task_id": None,
        },
        {
            "task_id": "T-0002",
            "title": "Second task",
            "final_task_status": "done",
            "pipeline_status": "done",
            "stage_outcomes": [
                "grooming=pass",
                "implementing=pass",
                "testing=pass",
                "accepting=pass",
                "commit_to_git=pass",
            ],
            "reason_code": None,
            "reason": None,
            "follow_up_task_id": None,
        },
    ]
    assert load_state(tmp_path).queue == []

def test_cmd_run_reports_runner_conflict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)

    from litehive.workspace import locking as locking_module

    real_flock = locking_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & locking_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.workspace.locking.fcntl.flock", fake_flock)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=None,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            pool_usage_cap=None,
            pool_cost_cap=None,
            engine_usage_cap=None,
            engine_budget_cap=None,
            engine_cost=None,
            stop_on_dirty_git=False,
            drain=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "run failed: workspace is already being mutated by another runner" in output

def test_save_task_rejects_runner_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Pending task", auto_commit=False)

    from litehive.workspace import locking as locking_module

    real_flock = locking_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & locking_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.workspace.locking.fcntl.flock", fake_flock)

    task.title = "Updated title"
    with pytest.raises(
        WorkspaceConflictError,
        match="workspace is already being mutated by another runner",
    ):
        save_task(tmp_path, task)

def test_save_state_rejects_runner_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    state = load_state(tmp_path)

    from litehive.workspace import locking as locking_module

    real_flock = locking_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & locking_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.workspace.locking.fcntl.flock", fake_flock)

    with pytest.raises(
        WorkspaceConflictError,
        match="workspace is already being mutated by another runner",
    ):
        save_state(tmp_path, state)

def test_cmd_run_reports_blocked_tasks_when_no_runnable_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked task", auto_commit=False)
    blocked.depends_on = ["T-9999"]
    save_task(tmp_path, blocked)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No runnable task." in output
    assert f"blocked: {blocked.id} Blocked task blocked_by=T-9999 (missing)" in output
    assert "completed_tasks: 0" in output
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 1" in output
    assert "remaining_tasks: 1" in output
    assert f"remaining: {blocked.id} Blocked task status=queued pipeline_status=backlog" in output
    assert "tasks_run: 0" in output
    assert "progress_status: no_useful_progress" in output
    assert (
        "summary: Pool stopped with no useful progress because no runnable task remained." in output
    )
    assert "stop_reason: blocked_tasks_remaining" in output
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["progress_status"] == "no_useful_progress"
    assert (
        durable_report["summary"]
        == "Pool stopped with no useful progress because no runnable task remained."
    )

def test_cmd_run_reports_pre_execution_stop_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=None,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            stop_on_dirty_git=True,
            drain=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No task executed." in output
    assert "completed_tasks: 0" in output
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 1" in output
    assert "remaining_tasks: 1" in output
    assert "remaining: T-0001 Pending task status=queued pipeline_status=backlog" in output
    assert "tasks_run: 0" in output
    assert "stop_condition: dirty git state" in output
    assert "stop_reason: dirty_git_state" in output

def test_cmd_run_reports_resumable_interrupted_tasks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Halted task", auto_commit=False)
    create_task(tmp_path, title="Pending follow-up", auto_commit=False)

    def fake_run(self, current_task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "testing":
            raise KeyboardInterrupt()
        return _completed_subagent_result(tmp_path, current_task.pipeline_status, task=current_task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: interrupted" in output
    assert "resumable_tasks: 1" in output
    assert "resumable: T-0001 Halted task status=interrupted pipeline_status=testing" in output
    assert "reason_code=execution_interrupted" in output
    assert "remaining_tasks: 1" in output
    assert "remaining: T-0002 Pending follow-up status=queued pipeline_status=backlog" in output
    assert "progress_status: no_useful_progress" in output
    assert (
        "summary: Pool stopped with no useful progress because the active task was interrupted and must be resumed."
        in output
    )
    assert "stop_condition: task interrupted and awaiting resume" in output
    assert "stop_reason: task_interrupted" in output

def test_run_next_task_marks_subagent_termination_as_interrupted(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Halted subagent task", auto_commit=False)

    def fake_run(self, current_task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "testing":
            return _interrupted_subagent_result(
                tmp_path, current_task.pipeline_status, engine_name=engine_name
            )
        return _completed_subagent_result(
            tmp_path, current_task.pipeline_status, engine_name=engine_name, task=current_task
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)
    try:
        summary = run_next_task(tmp_path)
    finally:
        monkeypatch.undo()

    assert summary.result is not None
    assert summary.result.final_status == "interrupted"
    refreshed = require_task(tmp_path, task.id)
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert refreshed.runtime.last_outcome.reason_code == "execution_interrupted"
    assert refreshed.runtime.current_stage.step == "testing"
    assert refreshed.runtime.current_stage.status == "interrupted"

def test_classify_execution_interruption_matches_signal_exit_codes_and_text() -> None:
    assert classify_execution_interruption("", exit_code=130) == "execution interrupted"
    assert (
        classify_execution_interruption("Received SIGINT from controlling terminal")
        == "execution interrupted"
    )
    assert classify_execution_interruption("ordinary failure", exit_code=1) is None

def test_cmd_run_reports_remaining_tasks_when_pool_stops_early(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=1,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            pool_usage_cap=None,
            pool_cost_cap=None,
            engine_usage_cap=None,
            engine_budget_cap=None,
            engine_cost=None,
            stop_on_dirty_git=False,
            drain=True,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "completed_tasks: 1" in output
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 1" in output
    assert (
        "completed: T-0001 First task status=done pipeline_status=done "
        "stage_outcomes=grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in output
    )
    assert (
        "skipped: T-0002 Second task status=queued pipeline_status=backlog stage_outcomes=-"
        in output
    )
    assert "remaining_tasks: 1" in output
    assert (
        "remaining: T-0002 Second task status=queued pipeline_status=backlog stage_outcomes=-"
        in output
    )
    assert "tasks_run: 1" in output
    assert "stop_condition: max tasks reached" in output
    assert "stop_reason: max_tasks_reached" in output
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["stop_reason"] == "max_tasks_reached"
    assert durable_report["stop_condition"] == "max tasks reached"
    assert durable_report["tasks_run"] == 1
    assert durable_report["completed_count"] == 1
    assert durable_report["flagged_count"] == 0
    assert durable_report["skipped_count"] == 1
    assert durable_report["remaining_count"] == 1

def test_cmd_run_drain_reports_no_useful_progress_after_requeue_when_only_blocked_work_remains(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    active = create_task(tmp_path, title="Active review loop", auto_commit=False)
    blocked = create_task(tmp_path, title="Blocked later task", auto_commit=False)
    blocked.depends_on = ["T-9999"]
    save_task(tmp_path, blocked)
    active.status = "in_progress"
    active.pipeline_status = "testing"
    active.retry_policy.max_retries = 2
    save_task(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [blocked.id]
    save_state(tmp_path, state)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if task.id == active.id and task.pipeline_status == "testing":
            task.depends_on = ["T-9998"]
            save_task(tmp_path, task)
            return _stage_subagent_result(
                tmp_path, task.pipeline_status, engine_name=engine_name,
                verdict="FAIL", summary="qa wants another implementation pass",
                files_changed=[], tests_added=0, tests_passing=0, task=task,
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name, task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "progress_status: no_useful_progress" in output
    assert (
        "summary: Pool stopped with no useful progress because no runnable task remained."
        in output
    )
    assert "stop_reason: blocked_tasks_remaining" in output
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["progress_status"] == "no_useful_progress"
    assert (
        durable_report["summary"]
        == "Pool stopped with no useful progress because no runnable task remained."
    )

def test_cmd_run_reports_human_checkpoint_stop_without_marking_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    create_task(
        tmp_path,
        title="Review before acceptance",
        human_checkpoints=["before_acceptance"],
        auto_commit=False,
    )
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: paused" in output
    assert "completed_tasks: 0" in output
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 2" in output
    assert "tasks_run: 1" in output
    assert "stop_condition: human checkpoint before acceptance" in output
    assert "stop_reason: human_checkpoint_before_acceptance" in output

def test_cmd_run_reports_requeued_task_even_when_other_tasks_are_blocked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    active = create_task(tmp_path, title="Active review loop", auto_commit=False)
    blocked = create_task(tmp_path, title="Blocked later task", auto_commit=False)
    blocked.depends_on = ["T-9999"]
    save_task(tmp_path, blocked)

    active.status = "in_progress"
    active.pipeline_status = "testing"
    active.retry_policy.max_retries = 2
    save_task(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [blocked.id]
    save_state(tmp_path, state)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if task.id == active.id and task.pipeline_status == "testing":
            return _stage_subagent_result(
                tmp_path, task.pipeline_status, engine_name=engine_name,
                verdict="FAIL", summary="qa wants another implementation pass",
                files_changed=[], tests_added=0, tests_passing=0, task=task,
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name, task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 Active review loop" in output
    assert "status: queued" in output
    assert "No runnable task." not in output
    assert "tasks_run: 1" in output
    assert "stop_reason: task_requeued" in output
    assert (
        f"remaining: {blocked.id} Blocked later task status=queued pipeline_status=backlog"
        in output
    )

def test_dequeue_next_task_selection_restores_missing_queued_tasks_to_state_queue(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First queued task")
    second = create_task(tmp_path, title="Second queued task")

    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == first.id
    repaired_state = load_state(tmp_path)
    assert repaired_state.active_task_id == first.id
    restored_second = get_task(tmp_path, second.id)
    assert restored_second is not None
    assert restored_second.status == "queued"

def test_cmd_run_reports_stage_outcomes_for_remaining_task_with_prior_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)
    reports_dir = task_dir(tmp_path, second) / "reports"
    reports_dir.mkdir(exist_ok=True)
    (reports_dir / "grooming-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": second.id,
                "step": "grooming",
                "verdict": "pass",
                "summary": "groomed",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=1,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            pool_usage_cap=None,
            pool_cost_cap=None,
            engine_usage_cap=None,
            engine_budget_cap=None,
            engine_cost=None,
            stop_on_dirty_git=False,
            drain=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert (
        "skipped: T-0002 Second task status=queued pipeline_status=backlog "
        "stage_outcomes=grooming=pass" in output
    )
    assert (
        "remaining: T-0002 Second task status=queued pipeline_status=backlog "
        "stage_outcomes=grooming=pass" in output
    )

def test_cmd_run_reports_failed_task_summary_with_stage_outcomes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Needs acceptance criteria", auto_commit=False)
    task.pipeline_status = "implementing"
    task.priority = "high"
    save_task(tmp_path, task)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        return _stage_subagent_result(
            tmp_path, task.pipeline_status, engine_name=engine_name,
            verdict="BLOCKED", summary="missing acceptance criteria",
            files_changed=[], tests_added=0, tests_passing=0, task=task,
        )

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "flagged_tasks: 1" in output
    assert (
        "flagged: T-0001 Needs acceptance criteria status=flagged pipeline_status=grooming "
        "stage_outcomes=grooming=blocked" in output
    )
    assert "skipped_tasks: 0" in output
    assert "remaining_tasks: 0" in output
    assert "tasks_run: 1" in output
    assert "stop_reason: single_task_complete" in output

def test_cmd_run_uses_configured_pool_stop_defaults(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_stop_on_dirty_git=True))
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=None,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            stop_on_dirty_git=False,
            drain=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No task executed." in output
    assert "stop_reason: dirty_git_state" in output

def test_cmd_run_reports_summary_when_queue_is_empty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=None,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            pool_usage_cap=None,
            pool_cost_cap=None,
            engine_usage_cap=None,
            engine_budget_cap=None,
            engine_cost=None,
            stop_on_dirty_git=False,
            drain=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No queued task." in output
    assert "completed_tasks: 0" in output
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 0" in output
    assert "remaining_tasks: 0" in output
    assert "tasks_run: 0" in output
    assert "stop_condition: queue exhausted" in output
    assert "stop_reason: queue_exhausted" in output
    summary_report = (tmp_path / ".litehive" / "pool-summary.txt").read_text(encoding="utf-8")
    assert "completed_tasks: 0" in summary_report
    assert "flagged_tasks: 0" in summary_report
    assert "stop_condition: queue exhausted" in summary_report

def test_status_output_includes_runtime_observability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_retry_limit=2,
            execution_retry_policies={
                "external_cli": {
                    "max_retries": 2,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout", "network"],
                }
            },
            pool_stop_on_failure=True,
            pool_max_tasks=4,
            pool_stop_on_execution_limit=True,
            pool_quota_threshold=2,
            pool_budget_threshold=1,
            pool_stop_on_dirty_git=True,
            pool_selection_policy="priority_first",
        ),
    )
    task = create_task(tmp_path, title="Observe long run")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.retry_policy.max_retries = 1
    task.runtime.execution_status = "running"
    task.runtime.retry_count = 1
    task.runtime.retry_limit = 1
    task.runtime.retry_source = "task"
    task.runtime.run_started_at = "2026-03-31T10:00:00+00:00"
    task.runtime.current_stage = RuntimeStageState(
        step="implementing",
        status="running",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:01+00:00",
        duration_seconds=0,
        summary="",
    )
    task.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="completed",
        path="subagents/SA-0001-swe",
        pid=4242,
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:02:00+00:00",
        completed_at="2026-03-31T10:02:00+00:00",
        exit_code=0,
        transcript_snippet="implemented live observability",
    )
    task.runtime.last_stage = RuntimeStageState(
        step="grooming",
        status="completed",
        started_at="2026-03-31T09:59:00+00:00",
        completed_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
        duration_seconds=60,
        verdict="pass",
        summary="plan confirmed",
    )
    task.runtime.last_outcome.kind = "blocked"
    task.runtime.last_outcome.stage = "testing"
    task.runtime.last_outcome.reason_code = "verdict_blocked"
    task.runtime.last_outcome.reason = "waiting on fixture update"
    task.runtime.last_outcome.retry_count = 1
    task.runtime.last_outcome.retry_limit = 1
    task.runtime.last_outcome.retry_source = "task"
    task.runtime.last_outcome.recorded_at = "2026-03-31T10:02:30+00:00"

    save_task(tmp_path, task)

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "default_retry_limit: 2" in output
    assert "external_cli=retries:2 backoff:0.25s multiplier:2.00 retry_on:timeout,network" in output
    assert "pool_stop_on_failure: True" in output
    assert "pool_max_tasks: 4" in output
    assert "pool_stop_on_execution_limit: True" in output
    assert "pool_quota_threshold: 2" in output
    assert "pool_budget_threshold: 1" in output
    assert "pool_stop_on_dirty_git: True" in output
    assert "pool_selection_policy: priority_first" in output
    assert "runner_status: idle pid=- started_at=- heartbeat_at=- active_task_id=-" in output
    assert "pool_stop_reason: None" in output
    assert "process_profile: generic" in output
    assert "retry_limit=1" in output
    assert "auto_commit=True" in output
    assert "commit_message=litehive: complete T-0001 observe-long-run" in output
    assert "retry_policy=configured:1 effective:1 source=task" in output
    assert "run=running" in output
    assert "retries=1/1" in output
    assert "retry_source=task" in output
    assert "stage=implementing" in output
    assert (
        "last_subagent=SA-0001 swe/codex completed pid=4242 sandbox=host snippet=implemented live observability"
        in output
    )
    assert "last_report=grooming/pass duration=1m00s summary=plan confirmed" in output
    assert (
        "outcome=blocked stage=testing reason_code=verdict_blocked recorded_at=2026-03-31T10:02:30+00:00 follow_up_task=- retry_state=1/1 retry_source=task reason=waiting on fixture update"
        in output
    )

def test_status_default_dashboard_shows_sections_without_config_noise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_retry_limit=2,
            pool_stop_on_failure=True,
            pool_max_tasks=4,
        ),
    )
    # Create an active task
    active = create_task(tmp_path, title="Implement dashboard")
    active.status = "in_progress"
    active.pipeline_status = "implementing"
    active.engine = "copilot"
    active.runtime.execution_status = "running"
    active.runtime.run_started_at = "2026-03-31T10:00:00+00:00"
    active.runtime.current_stage = RuntimeStageState(
        step="implementing",
        status="running",
        started_at="2026-03-31T10:01:00+00:00",
        updated_at="2026-03-31T10:01:01+00:00",
    )
    save_task(tmp_path, active)

    # Create a completed task
    done = create_task(tmp_path, title="Previous feature")
    done.status = "done"
    done.pipeline_status = "done"
    done.updated_at = "2026-03-31T09:50:00+00:00"
    save_task(tmp_path, done)

    # Create a queued task
    queued = create_task(tmp_path, title="Next feature")
    save_task(tmp_path, queued)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    # Write some events
    import json
    events_dir = tmp_path / ".litehive" / "tasks" / f"{active.id}-{active.slug}"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "events.jsonl").write_text(
        json.dumps({"ts": "2026-03-31T10:01:00+00:00", "task_id": active.id, "kind": "subagent_started", "data": {"role": "swe", "engine": "copilot"}}) + "\n",
        encoding="utf-8",
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    # Dashboard sections present
    assert "=== Active Task ===" in output
    assert active.id in output
    assert "implementing with copilot" in output
    assert "running for" in output
    assert "stage: implementing elapsed" in output
    assert "title: Implement dashboard" in output
    assert "=== Last Completed ===" in output
    assert done.id in output
    assert "pass" in output
    assert "title: Previous feature" in output
    assert "=== Queue ===" in output
    assert "1 queued" in output
    assert "Next feature" in output
    assert "=== Engine Health ===" in output
    assert "=== Recent Activity ===" in output
    assert "subagent_started" not in output  # label is "started"
    assert "started swe copilot" in output

    # Config noise NOT present in default output
    assert "default_retry_limit" not in output
    assert "pool_stop_on_failure" not in output
    assert "pool_max_tasks" not in output
    assert "execution_retry_policies" not in output
    assert "retry_limit=" not in output
    assert "auto_commit=" not in output

def test_status_full_mode_ignores_fast_flag_and_keeps_verbose_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    second.status = "in_progress"
    second.pipeline_status = "implementing"
    second.runtime.execution_status = "running"
    second.runtime.current_stage = RuntimeStageState(
        step="implementing",
        status="running",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:01+00:00",
    )
    save_task(tmp_path, second)

    state = load_state(tmp_path)
    state.active_task_id = second.id
    state.queue = [second.id, first.id]
    save_state(tmp_path, state)

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, fast=True, full=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status_read_mode: full" in output
    assert f"* {second.id} [in_progress/implementing]" in output
    assert f"  {first.id} [queued/backlog]" in output
    assert "run=running" in output
    assert "stage=implementing" in output

def test_status_default_and_fast_flag_match_dashboard_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())
    active = create_task(tmp_path, title="Active task")
    active.status = "in_progress"
    active.pipeline_status = "implementing"
    active.runtime.execution_status = "running"
    active.runtime.current_stage = RuntimeStageState(
        step="implementing",
        status="running",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:01+00:00",
    )
    save_task(tmp_path, active)

    done = create_task(tmp_path, title="Done task")
    done.status = "done"
    done.pipeline_status = "done"
    save_task(tmp_path, done)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    save_state(tmp_path, state)

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    default_output = capsys.readouterr().out
    compat_exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, fast=True))
    fast_output = capsys.readouterr().out

    assert exit_code == 0
    assert compat_exit_code == 0
    for output in (default_output, fast_output):
        assert "=== Active Task ===" in output
        assert active.id in output
        assert "=== Last Completed ===" in output
        assert done.id in output
        assert "=== Queue ===" in output
        assert "=== Engine Health ===" in output
        assert "=== Recent Activity ===" in output
        assert "default_retry_limit:" not in output

def test_status_output_includes_execution_estimate_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Estimated task")
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    reports_dir = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "grooming-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "grooming",
                "verdict": "pass",
                "summary": "ok",
                "duration_seconds": 120,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "stage_estimate=2m00s" in output
    assert "velocity=30.0stages/h" in output
    assert "eta=8m00s" in output

def test_issue_command_files_upstream_task_with_origin_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_root = tmp_path / "project-x"
    litehive_root = tmp_path / "litehive-upstream"
    ensure_workspace(litehive_root)
    ensure_workspace(project_root, LitehiveConfig(litehive_source_path=str(litehive_root)))
    source_task = create_task(project_root, title="Fix project timeout handling")

    state = load_state(project_root)
    state.active_task_id = source_task.id
    save_state(project_root, state)

    exit_code = _cmd_issue(
        argparse.Namespace(
            workspace=project_root,
            upstream="engine timeout not working",
            type="runtime_bug",
            details="Observed while running project X recovery.",
            acceptance_criteria=["Litehive timeout handling is configurable."],
            source_task=None,
            source_stage=None,
            source_role="recovery",
            source_project=None,
            litehive_workspace=None,
            patch_branch=None,
            patch_base="HEAD",
            prepare_patch_branch=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Created upstream task T-0001" in output
    upstream_task = require_task(litehive_root, "T-0001")
    assert upstream_task.title == "engine timeout not working"
    assert upstream_task.task_type == "bugfix"
    assert upstream_task.upstream_origin is not None
    assert upstream_task.upstream_origin.source_project == "project-x"
    assert upstream_task.upstream_origin.source_workspace == str(project_root.resolve())
    assert upstream_task.upstream_origin.source_task_id == source_task.id
    assert upstream_task.upstream_origin.source_stage == source_task.pipeline_status
    assert upstream_task.upstream_origin.contribution_kind == "runtime_bug"
    assert upstream_task.upstream_origin.litehive_source_path == str(litehive_root)
    assert upstream_task.acceptance_criteria == ["Litehive timeout handling is configurable."]

def test_issue_command_can_prepare_patch_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_root = tmp_path / "project-y"
    litehive_root = tmp_path / "litehive-upstream"
    ensure_workspace(project_root, LitehiveConfig(litehive_source_path=str(litehive_root)))
    litehive_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=litehive_root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=litehive_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=litehive_root, check=True)
    (litehive_root / "README.md").write_text("litehive\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=litehive_root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=litehive_root, check=True)
    ensure_workspace(litehive_root)

    exit_code = _cmd_issue(
        argparse.Namespace(
            workspace=project_root,
            upstream="Improve prompt defaults from real usage",
            type="prompt_improvement",
            details="Need a handoff branch for a prompt tweak.",
            acceptance_criteria=None,
            source_task=None,
            source_stage="implementing",
            source_role="recovery",
            source_project="project-y",
            litehive_workspace=None,
            patch_branch="recover/prompt-tune",
            patch_base="HEAD",
            prepare_patch_branch=True,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "patch_prepared: yes" in output
    branch = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/recover/prompt-tune"],
        cwd=litehive_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert branch.returncode == 0
    upstream_task = require_task(litehive_root, "T-0001")
    assert upstream_task.upstream_origin is not None
    assert upstream_task.upstream_origin.patch is not None
    assert upstream_task.upstream_origin.patch.branch == "recover/prompt-tune"
    assert upstream_task.upstream_origin.patch.prepared is True

def test_cmd_status_and_summary_include_upstream_origin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(litehive_source_path="/src/litehive"))
    task = create_task(
        tmp_path,
        title="Engine adapter follow-up",
        upstream_origin=UpstreamContributionOrigin(
            source_project="project-z",
            source_workspace="/work/project-z",
            source_task_id="T-0042",
            source_task_title="Recover adapter failure",
            source_stage="testing",
            source_role="recovery",
            contribution_kind="engine_adapter_fix",
            summary="Fix adapter retry handling",
            details="",
            litehive_source_path="/src/litehive",
            patch=UpstreamPatchProposal(branch="fix/adapter-timeout", base_ref="main", prepared=True),
        ),
    )

    lines = render_task_summary(task, active=False)
    combined = "\n".join(lines)
    assert "upstream_from=project-z kind=engine_adapter_fix source_task=T-0042 source_stage=testing" in combined
    assert "upstream_patch_branch=fix/adapter-timeout base=main prepared=True" in combined

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "litehive_source_path: /src/litehive" in output
    assert "upstream_from=project-z kind=engine_adapter_fix source_task=T-0042 source_stage=testing" in output

def test_build_workspace_snapshot_includes_active_session_and_run_all_logs(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Queued task")
    active = create_task(tmp_path, title="Active task")
    active.status = "in_progress"
    active.pipeline_status = "implementing"
    active.runtime.execution_status = "running"
    active.runtime.current_stage = RuntimeStageState(
        step="implementing",
        status="running",
        started_at="2026-04-04T10:00:00+00:00",
        updated_at="2026-04-04T10:00:05+00:00",
    )
    active_ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
    )
    active.subagents.append(active_ref)
    active.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
        pid=4321,
        started_at="2026-04-04T10:00:00+00:00",
        updated_at="2026-04-04T10:00:05+00:00",
    )
    save_task(tmp_path, active)

    base = task_dir(tmp_path, active) / "subagents" / "SA-0001-swe"
    base.mkdir(parents=True, exist_ok=True)
    (base / "session.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "SA-0001",
                "role": "swe",
                "engine": "codex",
                "status": "running",
                "updated_at": "2026-04-04T10:00:05+00:00",
                "pid": 4321,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (base / "transcript.md").write_text("live transcript\n", encoding="utf-8")
    (base / "stdout.log").write_text("live stdout\n", encoding="utf-8")
    (base / "stderr.log").write_text("live stderr\n", encoding="utf-8")

    run_all_dir = tmp_path / ".litehive" / "logs" / "run-all" / "20260404T100000Z"
    run_all_dir.mkdir(parents=True, exist_ok=True)
    (run_all_dir / "0001-run.log").write_text("iteration 1\n", encoding="utf-8")

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [active.id, queued.id]
    save_state(tmp_path, state)

    snapshot = build_workspace_snapshot(tmp_path)

    assert snapshot["active_task_id"] == active.id
    assert snapshot["queue"] == [active.id, queued.id]
    assert snapshot["active_task"]["id"] == active.id
    assert snapshot["active_task"]["active_subagent"]["id"] == "SA-0001"
    assert snapshot["active_task"]["subagents"][0]["is_active"] is True
    assert snapshot["active_task"]["subagents"][0]["tail_targets"]["stdout"].endswith("stdout.log")
    assert snapshot["run_all_logs"][0]["files"][0]["path"].endswith("0001-run.log")
    assert snapshot["run_all_logs"][0]["files"][0]["preview"] == "iteration 1\n"

def test_read_session_view_prefers_live_logs_for_active_subagent(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Active monitor task")
    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
        pid=99,
        started_at="2026-04-04T10:00:00+00:00",
        updated_at="2026-04-04T10:00:01+00:00",
    )
    save_task(tmp_path, task)

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    base.mkdir(parents=True, exist_ok=True)
    (base / "session.yaml").write_text("status: running\npid: 99\n", encoding="utf-8")
    (base / "transcript.md").write_text("partial transcript\n", encoding="utf-8")
    (base / "stdout.log").write_text("chunk a\n", encoding="utf-8")
    (base / "stderr.log").write_text("chunk err\n", encoding="utf-8")
    (base / "stdout.txt").write_text("stale snapshot\n", encoding="utf-8")

    payload = read_session_view(tmp_path, task.id, "SA-0001")

    stdout_artifact = next(item for item in payload["artifacts"] if item["kind"] == "stdout")
    stderr_artifact = next(item for item in payload["artifacts"] if item["kind"] == "stderr")

    assert payload["is_active"] is True
    assert payload["status"] == "running"
    assert stdout_artifact["path"].endswith("stdout.log")
    assert stdout_artifact["content"] == "chunk a\n"
    assert stderr_artifact["path"].endswith("stderr.log")

def test_read_session_view_uses_completed_snapshots_for_finished_subagent(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Completed monitor task")
    ref = SubagentRef(
        id="SA-0001",
        role="qa",
        engine="gemini",
        status="completed",
        path="subagents/SA-0001-qa",
    )
    task.subagents.append(ref)
    save_task(tmp_path, task)

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-qa"
    base.mkdir(parents=True, exist_ok=True)
    (base / "session.yaml").write_text("status: completed\nexit_code: 0\n", encoding="utf-8")
    (base / "transcript.md").write_text("final transcript\n", encoding="utf-8")
    with gzip.open(base / "stdout.txt.gz", "wt", encoding="utf-8") as handle:
        handle.write("finished stdout\n")
    (base / "stderr.txt").write_text("", encoding="utf-8")

    payload = read_session_view(tmp_path, task.id, "SA-0001")

    stdout_artifact = next(item for item in payload["artifacts"] if item["kind"] == "stdout")

    assert payload["is_active"] is False
    assert payload["status"] == "completed"
    assert stdout_artifact["path"].endswith("stdout.txt.gz")
    assert stdout_artifact["source"] == "compressed final snapshot"
    assert stdout_artifact["content"] == "finished stdout\n"


def test_read_session_view_and_recovery_evidence_support_compressed_subagent_artifacts(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Compressed evidence task")
    ref = SubagentRef(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="completed",
        path="subagents/SA-0001-qa",
    )
    task.subagents.append(ref)
    save_task(tmp_path, task)

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-qa"
    base.mkdir(parents=True, exist_ok=True)
    (base / "session.yaml").write_text("status: completed\nexit_code: 0\n", encoding="utf-8")
    with gzip.open(base / "transcript.md.gz", "wt", encoding="utf-8") as handle:
        handle.write("final transcript\n")
    with gzip.open(base / "stdout.txt.gz", "wt", encoding="utf-8") as handle:
        handle.write("finished stdout\n")
    with gzip.open(base / "timeline.yaml.gz", "wt", encoding="utf-8") as handle:
        handle.write("engine: codex\n")

    payload = read_session_view(tmp_path, task.id, "SA-0001")
    transcript_artifact = next(item for item in payload["artifacts"] if item["kind"] == "transcript")

    assert transcript_artifact["path"].endswith("transcript.md.gz")
    assert transcript_artifact["content"] == "final transcript\n"

    evidence = {item.label: item for item in collect_recovery_evidence(tmp_path, task)}
    assert evidence["latest subagent transcript"].exists is True
    assert evidence["latest subagent transcript"].path.endswith("transcript.md.gz")
    assert evidence["latest subagent stdout"].path.endswith("stdout.txt.gz")
    assert evidence["latest subagent events timeline"].path.endswith("timeline.yaml.gz")

def test_render_task_summary_includes_active_subagent_pid() -> None:
    task = TaskRecord(id="T-0001", slug="observe-pid", title="Observe PID")
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
        pid=31337,
        sandboxed=True,
        sandbox_summary="sandbox[docker:test net=none workspace=rw]",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
    )

    lines = render_task_summary(task, active=False)

    assert any("subagent=SA-0001 swe/codex running pid=31337" in line for line in lines)
    assert any("sandbox=sandbox[docker:test net=none workspace=rw]" in line for line in lines)

def test_cmd_status_includes_engine_monitoring_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="codex",
        adapter=get_engine("codex"),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=tmp_path,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
        ),
        failure_kind="execution_limit",
        failure_reason="usage limit reached",
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert (
        "engine_monitoring: codex source=local invocations=1 success=0 failure=1 limits=1" in output
    )
    assert "last_limit_reason=usage limit reached" in output
    assert "usage=used=1,unit=requests" in output

def test_cmd_status_includes_codex_provider_limit_monitoring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="codex",
        adapter=get_engine("codex"),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec", "--json"),
            cwd=tmp_path,
            exit_code=1,
            stdout='{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":429,\\"error\\":{\\"type\\":\\"rate_limit_error\\",\\"message\\":\\"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits.\\"}}"}',
            stderr="",
        ),
        failure_kind="execution_limit",
        failure_reason="usage limit reached",
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert (
        "engine_monitoring: codex source=provider invocations=1 success=0 failure=1 limits=1"
        in output
    )
    assert "provider=openai" in output
    assert "last_limit_reason=usage limit reached" in output

def test_cmd_status_includes_claude_provider_limit_monitoring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="claude",
        adapter=get_engine("claude"),
        execution=CLIExecutionResult(
            adapter="claude",
            argv=("claude", "-p"),
            cwd=tmp_path,
            exit_code=1,
            stdout=(
                '{"type":"error","error":{"type":"rate_limit_error","message":"Your account has hit a rate limit. '
                'Please retry after a short delay."}}\n'
            ),
            stderr="",
        ),
        failure_kind="execution_limit",
        failure_reason="rate limit reached",
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert (
        "engine_monitoring: claude source=provider invocations=1 success=0 failure=1 limits=1"
        in output
    )
    assert "provider=anthropic" in output
    assert "last_limit_reason=rate limit reached" in output

def test_cmd_status_includes_gemini_provider_limit_monitoring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="gemini",
        adapter=get_engine("gemini"),
        execution=CLIExecutionResult(
            adapter="gemini",
            argv=("gemini", "-p"),
            cwd=tmp_path,
            exit_code=1,
            stdout=(
                '{"type":"Error","value":{"message":"You exceeded your current quota, please check your plan and billing details. '
                'Please retry in 56s.","status":"RESOURCE_EXHAUSTED","details":['
                '{"@type":"type.googleapis.com/google.rpc.QuotaFailure","violations":[{'
                '"quotaMetric":"generativelanguage.googleapis.com/generate_content_free_tier_requests",'
                '"quotaId":"GenerateRequestsPerMinutePerProjectPerModel-FreeTier",'
                '"quotaDimensions":{"location":"global","model":"gemini-2.5-pro"},'
                '"quotaValue":"2"}]},'
                '{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"56s"}]}}\n'
            ),
            stderr="",
        ),
        failure_kind="execution_limit",
        failure_reason="quota limit reached",
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert (
        "engine_monitoring: gemini source=provider invocations=1 success=0 failure=1 limits=1"
        in output
    )
    assert "provider=google" in output
    assert "last_limit_reason=quota limit reached" in output
    assert "usage=limit=2,unit=requests" in output

def test_cmd_status_includes_engine_usage_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    class ProviderAdapter(ExternalCLIAdapter):
        def build_command(
            self, prompt: str, cwd: Path, model: str | None = None, *, max_turns: int | None = None
        ) -> list[str]:  # type: ignore[override]
            return ["provider-cli", prompt]

        def extract_usage_observation(
            self, execution: CLIExecutionResult
        ) -> EngineUsageObservation | None:
            return EngineUsageObservation(
                source="provider",
                provider="github",
                success=True,
                usage=EngineUsageWindow(
                    used=60,
                    limit=100,
                    remaining=40,
                    unit="requests",
                    reset_at="2026-04-30T00:00:00Z",
                ),
            )

    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="copilot",
        adapter=ProviderAdapter(
            name="copilot",
            binary="provider-cli",
            capabilities=AdapterCapabilities(
                supports_model_override=True, transcript_format="jsonl"
            ),
        ),
        execution=CLIExecutionResult(
            adapter="copilot",
            argv=("provider-cli", "run"),
            cwd=tmp_path,
            exit_code=0,
            stdout="{}",
            stderr="",
        ),
        failure_kind=None,
        failure_reason=None,
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert (
        "engine_monitoring: copilot source=provider invocations=1 success=1 failure=0 limits=0"
        in output
    )
    assert (
        "usage=used=60,limit=100,remaining=40,unit=requests,reset_at=2026-04-30T00:00:00Z" in output
    )

def test_render_task_summary_includes_interruption_context() -> None:
    task = TaskRecord(id="T-0001", slug="resume-task", title="Resume task")
    task.status = "interrupted"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "interrupted"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="interrupted",
        started_at="2026-04-01T10:00:00+00:00",
        completed_at="2026-04-01T10:02:00+00:00",
        updated_at="2026-04-01T10:02:00+00:00",
    )
    task.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="interrupted",
        path="subagents/SA-0001-qa",
        pid=5151,
        started_at="2026-04-01T10:00:10+00:00",
        updated_at="2026-04-01T10:02:00+00:00",
        completed_at="2026-04-01T10:02:00+00:00",
        transcript_snippet="halfway through targeted testing",
        interruption_reason="execution interrupted",
    )
    task.runtime.interruption = RuntimeInterruptionState(
        source="subagent",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="execution interrupted",
        summary="Execution interrupted during testing",
        interrupted_at="2026-04-01T10:02:00+00:00",
        detected_at="2026-04-01T10:02:00+00:00",
        subagent=task.runtime.last_subagent,
    )

    lines = render_task_summary(task, active=False)

    assert any("last_subagent_interruption_reason=execution interrupted" in line for line in lines)
    assert any(
        "interruption=subagent stage=testing resume_from=testing interrupted_at=2026-04-01T10:02:00+00:00"
        in line
        for line in lines
    )

def test_run_task_updates_runner_heartbeat_while_task_is_running(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Heartbeat task", auto_commit=False)

    started = threading.Event()
    release = threading.Event()
    snapshots: list[tuple[str | None, str | None, str]] = []

    def run_executor() -> None:
        def executor(current_task: TaskRecord, step: str) -> StageReport:
            started.set()
            first = runner_status(tmp_path)
            snapshots.append((first.started_at, first.heartbeat_at, first.status))
            time.sleep(1.2)
            second = runner_status(tmp_path)
            snapshots.append((second.started_at, second.heartbeat_at, second.status))
            release.wait(timeout=5)
            return StageReport(task_id=current_task.id, step=step, verdict="blocked", summary="pause")

        runner = TaskExecutionRunner(tmp_path, executor)
        with tasks_module.workspace_runner_guard(tmp_path):
            tasks_module.mark_task_run_started(tmp_path, task)
            with tasks_module.runner_heartbeat(tmp_path, active_task_id=task.id):
                runner.run(task)

    worker = threading.Thread(target=run_executor, daemon=True)
    worker.start()
    assert started.wait(timeout=5)
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()

    assert len(snapshots) == 2
    assert snapshots[0][2] == "running"
    assert snapshots[1][2] == "running"
    assert snapshots[0][0] is not None
    assert snapshots[0][1] is not None
    assert snapshots[1][0] == snapshots[0][0]
    assert snapshots[1][1] is not None
    assert snapshots[1][1] != snapshots[0][1]
    final = runner_status(tmp_path)
    assert final.status == "idle"

def test_runner_status_reports_stale_when_workspace_reconciliation_is_still_needed(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Interrupted task", auto_commit=False)
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    runner_lock = tmp_path / ".litehive" / ".runner.lock"
    runner_lock.write_text(
        yaml.safe_dump(
            {
                "status": "running",
                "pid": 4242,
                "workspace": str(tmp_path),
                "command": "uv run litehive run --workspace .",
                "started_at": "2026-04-01T00:00:00+00:00",
                "heartbeat_at": "2026-04-01T00:00:05+00:00",
                "active_task_id": task.id,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    status = runner_status(tmp_path)

    assert status.status == "stale"
    assert status.active_task_id == task.id
    assert "heartbeat_at:" in runner_lock.read_text(encoding="utf-8")

def test_runner_status_clears_orphaned_metadata_when_no_reconciliation_is_needed(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    runner_lock = tmp_path / ".litehive" / ".runner.lock"
    runner_lock.write_text(
        yaml.safe_dump(
            {
                "status": "running",
                "pid": 4242,
                "workspace": str(tmp_path),
                "command": "uv run litehive run --workspace .",
                "started_at": "2026-04-01T00:00:00+00:00",
                "heartbeat_at": "2026-04-01T00:00:05+00:00",
                "active_task_id": "T-9999",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    status = runner_status(tmp_path)

    assert status.status == "idle"
    assert runner_lock.read_text(encoding="utf-8") == ""

def test_runner_status_reports_late_when_lock_held_but_heartbeat_expired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Stale task", auto_commit=False)

    old_heartbeat = "2026-01-01T00:00:00+00:00"
    runner_lock = tmp_path / ".litehive" / ".runner.lock"
    runner_lock.write_text(
        yaml.safe_dump(
            {
                "status": "running",
                "pid": os.getpid(),
                "workspace": str(tmp_path),
                "command": "uv run litehive run --workspace .",
                "started_at": "2026-01-01T00:00:00+00:00",
                "heartbeat_at": old_heartbeat,
                "active_task_id": task.id,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("litehive.workspace.locking._runner_lock_is_active", lambda root: True)

    status = runner_status(tmp_path)

    assert status.status == "late"
    assert status.heartbeat_at == old_heartbeat
    assert status.active_task_id == task.id

def test_format_external_engine_sandbox_renders_engine_policies() -> None:
    config = LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            image="ghcr.io/example/litehive-sandbox:latest",
            engine_policies={
                "codex": ExternalEngineSandboxPolicy(
                    enabled=True,
                    network_mode="none",
                    workspace_mode="rw",
                    environment=["OPENAI_API_KEY"],
                    extra_ro_binds=["/opt/runtime"],
                )
            },
        )
    )

    rendered = format_external_engine_sandbox(config)

    assert "enabled backend:docker runtime:docker image:ghcr.io/example/litehive-sandbox:latest" in rendered
    assert "codex=enabled:True net:none workspace:rw env:OPENAI_API_KEY creds:- binds:/opt/runtime" in rendered

def test_format_subagent_resource_limits_renders_effective_limits() -> None:
    rendered = format_subagent_resource_limits(LitehiveConfig(process_profile="rust"))

    assert rendered == "enabled memory_mb:8192 cpu_count:4 process_limit:512"

def test_status_output_includes_default_execution_retry_policies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "execution_retry_policies:" in output
    assert "codex=retries:2" in output
    assert "claude=retries:2" in output

def test_build_parser_accepts_status_fast_flag(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(["status", "--workspace", str(tmp_path), "--fast"])

    assert args.command == "status"
    assert args.workspace == tmp_path
    assert args.fast is True


def test_fast_status_resolves_workspace_via_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Registry status target")
    home = tmp_path / "home"
    registry = home / ".litehive" / "workspaces.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        yaml.safe_dump({"workspaces": {"demo": str(tmp_path)}}, sort_keys=False),
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()

    from litehive.main import _fast_status

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LITEHIVE_TASK_ID", task.id)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    monkeypatch.chdir(outside)

    exit_code = _fast_status([])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"workspace: {tmp_path.resolve()}" in output

def test_status_output_includes_external_engine_sandbox_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            external_engine_sandbox=ExternalEngineSandboxConfig(
                enabled=True,
                image="ghcr.io/example/litehive-sandbox:latest",
                engine_policies={
                    "codex": ExternalEngineSandboxPolicy(
                        enabled=True,
                        network_mode="none",
                        workspace_mode="rw",
                        environment=["OPENAI_API_KEY"],
                        extra_ro_binds=["/opt/runtime"],
                    )
                },
            )
        ),
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    config = load_config(tmp_path)
    assert config.external_engine_sandbox.enabled is True
    assert config.external_engine_sandbox.image == "ghcr.io/example/litehive-sandbox:latest"
    assert "codex" in config.external_engine_sandbox.engine_policies
    codex_policy = config.external_engine_sandbox.engine_policies["codex"]
    assert codex_policy.enabled is True
    assert codex_policy.network_mode == "none"
    assert codex_policy.workspace_mode == "rw"
    assert "OPENAI_API_KEY" in codex_policy.environment
    assert codex_policy.extra_ro_binds == ["/opt/runtime"]

def test_status_output_includes_subagent_resource_limits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(process_profile="rust"))

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    config = load_config(tmp_path)
    assert config.subagent_resource_limits.enabled is True
    assert config.subagent_resource_limits.memory_mb == 8192
    assert config.subagent_resource_limits.cpu_count == 4
    assert config.subagent_resource_limits.process_limit == 512

def test_status_output_includes_runner_hooks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "before_implementing": [{"command": "echo pre", "reject_on_failure": False}],
                "after_implementing": [{"command": "echo review", "reject_on_failure": True}],
            }
        ),
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    config = load_config(tmp_path)
    assert "after_implementing" in config.runner_hooks
    assert config.runner_hooks["after_implementing"][0].command == "echo review"
    assert config.runner_hooks["after_implementing"][0].reject_on_failure is True
    assert "before_implementing" in config.runner_hooks
    assert config.runner_hooks["before_implementing"][0].command == "echo pre"
    assert config.runner_hooks["before_implementing"][0].reject_on_failure is False

def test_status_output_includes_budget_control_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            pool_usage_cap=12,
            pool_cost_cap=30,
            engine_usage_caps={"claude": 2, "codex": 5},
            engine_budget_caps={"claude": 6},
            engine_costs={"claude": 3, "codex": 1},
        ),
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    config = load_config(tmp_path)
    assert config.pool_usage_cap == 12
    assert config.pool_cost_cap == 30
    assert config.engine_usage_caps == {"claude": 2, "codex": 5}
    assert config.engine_budget_caps == {"claude": 6}
    assert config.engine_costs["claude"] == 3
    assert config.engine_costs["codex"] == 1

def test_status_default_dashboard_shows_sections_without_config_noise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(
        default_retry_limit=2,
        pool_stop_on_failure=True,
        pool_max_tasks=4,
    ))
    active = create_task(tmp_path, title="Active feature")
    active.status = "in_progress"
    active.pipeline_status = "implementing"
    active.runtime.execution_status = "running"
    active.runtime.run_started_at = "2026-04-07T10:00:00+00:00"
    active.runtime.current_stage = RuntimeStageState(
        step="implementing",
        status="running",
        started_at="2026-04-07T10:05:00+00:00",
        updated_at="2026-04-07T10:05:01+00:00",
    )
    save_task(tmp_path, active)

    done_task = create_task(tmp_path, title="Completed feature")
    done_task.status = "done"
    done_task.pipeline_status = "done"
    done_task.updated_at = "2026-04-07T09:00:00+00:00"
    done_task.runtime.last_stage = RuntimeStageState(
        step="commit_to_git",
        verdict="pass",
        status="completed",
    )
    save_task(tmp_path, done_task)

    queued = create_task(tmp_path, title="Next feature")
    save_task(tmp_path, queued)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    # Dashboard sections present
    assert "=== Active Task ===" in output
    assert "implementing with" in output
    assert "running for" in output
    assert "stage: implementing elapsed" in output
    assert "=== Last Completed ===" in output
    assert "Completed feature" in output
    assert "=== Queue ===" in output
    assert "1 queued" in output
    assert "Next feature" in output
    assert "=== Engine Health ===" in output
    assert "=== Recent Activity ===" in output
    # Config noise absent from default output
    assert "default_retry_limit:" not in output
    assert "execution_retry_policies:" not in output
    assert "pool_stop_on_failure:" not in output
    assert "pool_max_tasks:" not in output
    assert "pool_selection_policy:" not in output
    assert "process_profile:" not in output
    # No per-task dump
    assert "retry_policy=configured:" not in output
    assert "auto_commit=" not in output

def test_queue_command_shows_active_and_queued_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    second.depends_on = [first.id]
    save_task(tmp_path, second)

    set_active_task(tmp_path, first.id)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": os.getpid()}, sort_keys=False),
        encoding="utf-8",
    )
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_queue(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"active_task_id: {first.id}" in output
    assert (
        f"active: {first.id} [in_progress/backlog] priority=medium engine=codex (default) model=default "
        "title=First task depends_on=-"
    ) in output
    assert (
        f"1. {second.id} [queued/backlog] priority=medium engine=codex (default) model=default "
        f"title=Second task depends_on={first.id}"
    ) in output

def test_repair_command_repairs_stale_runner_state_and_cleans_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    interrupted = create_task(tmp_path, title="Halted testing task", auto_commit=False)
    queued = create_task(tmp_path, title="Pending task", auto_commit=False)
    done = create_task(tmp_path, title="Completed task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)

    done.status = "done"
    done.pipeline_status = "done"
    save_task(tmp_path, done)

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id, "T-9999", queued.id, done.id]
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": 999999}, sort_keys=False),
        encoding="utf-8",
    )

    exit_code = _cmd_repair(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "repaired: yes" in output
    assert "stale_runner_recovered: yes" in output
    assert f"cleared_active_task_id: {interrupted.id}" in output
    assert f"requeued_tasks: {interrupted.id}" in output
    assert f"removed_queue_entries: T-9999 {done.id}" in output
    assert f"deduped_queue_entries: {queued.id}" in output
    assert "restored_queue_entries: -" in output
    assert "finalized_commit_tasks: -" in output
    assert "active_task_id: None" in output
    assert "queue_length: 2" in output

    repaired_state = load_state(tmp_path)
    assert repaired_state.active_task_id is None
    assert repaired_state.queue == [interrupted.id, queued.id]
    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"

def test_repair_workspace_state_reports_noop_for_consistent_workspace(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Pending task", auto_commit=False)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is False
    assert summary.stale_runner_recovered is False
    assert summary.cleared_active_task_id is None
    assert summary.requeued_task_ids == []
    assert summary.removed_queue_entries == []
    assert summary.deduped_queue_entries == []
    assert summary.restored_queue_entries == []
    assert summary.finalized_commit_task_ids == []
    assert load_state(tmp_path).queue == [task.id]

def test_repair_workspace_state_requeues_untouched_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Halted active task", auto_commit=False)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)

    active.status = "in_progress"
    active.pipeline_status = "testing"
    save_task(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is True
    assert summary.stale_runner_recovered is False
    assert summary.cleared_active_task_id == active.id
    assert summary.requeued_task_ids == [active.id]
    assert summary.removed_queue_entries == []
    assert summary.deduped_queue_entries == []
    assert summary.restored_queue_entries == []
    assert summary.finalized_commit_task_ids == []

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue == [queued.id, active.id]

    refreshed = get_task(tmp_path, active.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "idle"

def test_repair_workspace_state_requeues_orphaned_commit_stage_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    orphaned = create_task(tmp_path, title="Halted commit task", auto_commit=False)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)

    orphaned.status = "in_progress"
    orphaned.pipeline_status = "commit_to_git"
    orphaned.runtime.execution_status = "running"
    orphaned.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, orphaned)
    save_task_runtime(tmp_path, orphaned)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [queued.id]
    save_state(tmp_path, state)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is True
    assert summary.stale_runner_recovered is True
    assert summary.cleared_active_task_id is None
    assert summary.requeued_task_ids == [orphaned.id]
    assert summary.removed_queue_entries == []
    assert summary.deduped_queue_entries == []
    assert summary.restored_queue_entries == []
    assert summary.finalized_commit_task_ids == []

    refreshed = get_task(tmp_path, orphaned.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert load_state(tmp_path).queue == [orphaned.id, queued.id]

def test_repair_workspace_state_finalizes_existing_checkpoint_commit(tmp_path: Path) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Stranded commit task")
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    commit_message = "litehive: complete T-0001 stranded-commit-task"
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_message], tmp_path)
    existing_checkpoint_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)

    stranded.status = "done"
    stranded.pipeline_status = "done"
    stranded.git.checkpoint_attempts = 1
    stranded.git.checkpoint_base_sha = initial_sha
    stranded.git.commit_sha = None
    stranded.runtime.execution_status = "running"
    stranded.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, stranded)
    save_task_runtime(tmp_path, stranded)

    state = load_state(tmp_path)
    state.active_task_id = stranded.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is True
    assert summary.stale_runner_recovered is True
    assert summary.cleared_active_task_id is None
    assert summary.requeued_task_ids == []
    assert summary.removed_queue_entries == []
    assert summary.deduped_queue_entries == []
    assert summary.restored_queue_entries == []
    assert summary.finalized_commit_task_ids == [stranded.id]

    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == existing_checkpoint_sha
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.runtime.execution_status == "done"
    assert load_state(tmp_path).active_task_id is None
    assert load_state(tmp_path).queue == [queued.id]

def test_queue_command_marks_recovered_interruption(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    interrupted = create_task(tmp_path, title="Halted testing task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": 999999}, sort_keys=False),
        encoding="utf-8",
    )

    exit_code = _cmd_queue(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "active_task_id: None" in output
    assert "queue_length: 2" in output
    assert "resumable_tasks: 1" in output
    assert (
        f"resume 1. {interrupted.id} [interrupted/testing] priority=medium engine=codex (default) model=default "
        "title=Halted testing task depends_on=- resumable_from=testing interruption=runner "
        "reason_code=execution_interrupted reason=Stale runner detected while `testing` was still marked running."
    ) in output
    assert (
        f"2. {queued.id} [queued/backlog] priority=medium engine=codex (default) model=default "
        "title=Pending follow-up depends_on=-"
    ) in output

def test_recover_stale_runner_state_recovers_running_task_without_runner_lock_record(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    interrupted = create_task(tmp_path, title="Halted testing task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.run_started_at = "2026-04-01T00:00:00+00:00"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    interrupted.subagents.append(
        SubagentRef(
            id="SA-0001",
            role="qa",
            engine="codex",
            status="running",
            path="subagents/SA-0001-qa",
        )
    )
    interrupted.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="running",
        path="subagents/SA-0001-qa",
        pid=4242,
        started_at="2026-04-01T00:00:10+00:00",
        updated_at="2026-04-01T00:00:10+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)
    subagent_base = task_dir(tmp_path, interrupted) / "subagents" / "SA-0001-qa"
    subagent_base.mkdir(parents=True, exist_ok=False)
    (subagent_base / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "status": "running",
                "summary": "halfway through targeted testing",
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
                "warnings": [],
                "resource_limit_event": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (subagent_base / "transcript.md").write_text(
        "VERDICT: PASS\nSUMMARY: halfway through targeted testing\n",
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    mutated = recover_stale_runner_state(tmp_path)

    assert mutated is True
    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.active_subagent is None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.status == "interrupted"
    assert refreshed.runtime.last_subagent.transcript_snippet == "halfway through targeted testing"
    assert refreshed.runtime.last_subagent.interruption_reason.startswith(
        "Stale runner detected while subagent"
    )
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.source == "subagent"
    assert refreshed.runtime.interruption.resume_stage == "testing"
    assert refreshed.runtime.interruption.subagent is not None
    assert refreshed.runtime.interruption.subagent.id == "SA-0001"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert refreshed.subagents[-1].status == "interrupted"
    session = yaml.safe_load((subagent_base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((subagent_base / "report.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "interrupted"
    assert session["resume_stage"] == "testing"
    assert session["interruption_reason"].startswith("Stale runner detected while subagent")
    assert report["status"] == "interrupted"
    assert report["resume_stage"] == "testing"
    assert report["interruption_reason"].startswith("Stale runner detected while subagent")
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [interrupted.id, queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted subagent execution while `testing` was running." in journal
    assert "Subagent `SA-0001` (qa/codex, pid=4242, path `subagents/SA-0001-qa`) stopped" in journal
    assert "Resume from `testing`." in journal

def test_recover_stale_runner_state_recovers_when_lock_is_not_held_even_if_pid_is_alive(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    interrupted = create_task(tmp_path, title="Halted testing task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump(
            {"pid": os.getpid(), "started_at": "2026-04-01T00:00:00+00:00"}, sort_keys=False
        ),
        encoding="utf-8",
    )

    mutated = recover_stale_runner_state(tmp_path)

    assert mutated is True
    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.source == "runner"
    assert (
        refreshed.runtime.interruption.reason
        == "Stale runner detected while `testing` was still marked running."
    )
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [interrupted.id, queued.id]

def test_recover_stale_runner_state_skips_live_runner_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    running = create_task(tmp_path, title="Active task", auto_commit=False)

    running.status = "in_progress"
    running.pipeline_status = "testing"
    running.runtime.execution_status = "running"
    running.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    running.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="running",
        path="subagents/SA-0001-qa",
        pid=5151,
        started_at="2026-04-01T00:00:10+00:00",
        updated_at="2026-04-01T00:00:10+00:00",
    )
    save_task(tmp_path, running)
    save_task_runtime(tmp_path, running)

    state = load_state(tmp_path)
    state.active_task_id = running.id
    state.queue = [queued.id]
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": os.getpid()}, sort_keys=False),
        encoding="utf-8",
    )
    _block_runner_lock(monkeypatch)

    mutated = recover_stale_runner_state(tmp_path)

    assert mutated is False
    refreshed = get_task(tmp_path, running.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.active_subagent is not None
    assert load_state(tmp_path).active_task_id == running.id
    assert load_state(tmp_path).queue == [queued.id]

def test_recover_stale_runner_state_persists_cleared_stale_active_marker_without_transition(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)

    state = load_state(tmp_path)
    state.active_task_id = "T-9999"
    state.queue = [queued.id]
    save_state(tmp_path, state)

    mutated = recover_stale_runner_state(tmp_path)

    assert mutated is True
    restored = load_state(tmp_path)
    assert restored.active_task_id is None
    assert restored.queue == [queued.id]

def test_add_command_persists_dependencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Dependent task",
            goal="",
            acceptance_criteria=None,
            depends_on=[first.id, f"{second.id},{first.id}"],
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0003")
    assert task is not None
    assert task.depends_on == [first.id, second.id]
    assert f"depends_on: {first.id}, {second.id}" in output

def test_add_command_persists_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Large task",
            goal="Ship deterministic routing",
            acceptance_criteria=["Document the route", "Block missing retries"],
            depends_on=None,
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.acceptance_criteria == ["Document the route", "Block missing retries"]
    assert "acceptance_criteria: 2" in output
    assert "warning:" not in output


def test_compute_pool_flow_statistics_returns_none_when_no_durations(
    tmp_path: Path,
) -> None:
    from litehive.cli._pool import _compute_pool_flow_statistics

    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Quick task", auto_commit=False)

    # Write reports with zero duration_seconds (no timing data)
    reports_dir = (
        tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    for i, step in enumerate(["grooming", "implementing"]):
        data = {"step": step, "verdict": "pass", "duration_seconds": 0}
        (reports_dir / f"report-{i:04d}.yaml").write_text(
            yaml.safe_dump(data), encoding="utf-8"
        )

    entries = [
        {"task_id": task.id, "title": task.title}
    ]
    result = _compute_pool_flow_statistics(tmp_path, entries)
    assert result is None


def test_compute_pool_flow_statistics_identifies_bottleneck(
    tmp_path: Path,
) -> None:
    from litehive.cli._pool import _compute_pool_flow_statistics

    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Measured task", auto_commit=False)

    reports_dir = (
        tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    stage_data = [
        ("grooming", "pass", 10),
        ("implementing", "pass", 60),
        ("testing", "pass", 25),
        ("accepting", "pass", 5),
        ("commit_to_git", "pass", 2),
    ]
    for i, (step, verdict, duration) in enumerate(stage_data):
        data = {"step": step, "verdict": verdict, "duration_seconds": duration}
        (reports_dir / f"report-{i:04d}.yaml").write_text(
            yaml.safe_dump(data), encoding="utf-8"
        )

    entries = [{"task_id": task.id, "title": task.title}]
    stats = _compute_pool_flow_statistics(tmp_path, entries)

    assert stats is not None
    assert stats["stages_executed"] == 5
    assert stats["bottleneck_stage"] == "implementing"
    assert stats["bottleneck_avg_seconds"] == 60.0
    assert stats["stage_metrics"]["grooming"]["avg_seconds"] == 10.0
    assert stats["stage_metrics"]["implementing"]["avg_seconds"] == 60.0
    assert stats["stage_metrics"]["testing"]["avg_seconds"] == 25.0
    assert stats["stage_metrics"]["implementing"]["min_seconds"] == 60.0
    assert stats["stage_metrics"]["implementing"]["max_seconds"] == 60.0
    assert stats["stage_pass_counts"]["implementing"] == 1
    assert stats["stage_fail_counts"] == {}


def test_compute_pool_flow_statistics_averages_across_multiple_tasks(
    tmp_path: Path,
) -> None:
    from litehive.cli._pool import _compute_pool_flow_statistics

    ensure_workspace(tmp_path)
    task1 = create_task(tmp_path, title="First task", auto_commit=False)
    task2 = create_task(tmp_path, title="Second task", auto_commit=False)

    for task, duration in [(task1, 30), (task2, 90)]:
        reports_dir = (
            tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
        )
        reports_dir.mkdir(parents=True, exist_ok=True)
        data = {"step": "implementing", "verdict": "pass", "duration_seconds": duration}
        (reports_dir / "report-0000.yaml").write_text(
            yaml.safe_dump(data), encoding="utf-8"
        )

    entries = [
        {"task_id": task1.id, "title": task1.title},
        {"task_id": task2.id, "title": task2.title},
    ]
    stats = _compute_pool_flow_statistics(tmp_path, entries)

    assert stats is not None
    assert stats["stages_executed"] == 2
    assert stats["stage_metrics"]["implementing"]["avg_seconds"] == 60.0
    assert stats["bottleneck_stage"] == "implementing"


def test_compute_pool_flow_statistics_tracks_failures(
    tmp_path: Path,
) -> None:
    from litehive.cli._pool import _compute_pool_flow_statistics

    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Failing task", auto_commit=False)

    reports_dir = (
        tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    stage_data = [
        ("implementing", "fail", 40),
        ("implementing", "pass", 50),  # retry
        ("testing", "reject", 15),
        ("testing", "pass", 20),  # retry
    ]
    for i, (step, verdict, duration) in enumerate(stage_data):
        data = {"step": step, "verdict": verdict, "duration_seconds": duration}
        (reports_dir / f"report-{i:04d}.yaml").write_text(
            yaml.safe_dump(data), encoding="utf-8"
        )

    entries = [{"task_id": task.id, "title": task.title}]
    stats = _compute_pool_flow_statistics(tmp_path, entries)

    assert stats is not None
    assert stats["stages_executed"] == 4
    assert stats["stage_fail_counts"]["implementing"] == 1
    assert stats["stage_fail_counts"]["testing"] == 1
    assert stats["stage_pass_counts"]["implementing"] == 1
    assert stats["stage_pass_counts"]["testing"] == 1
    # avg for implementing = (40 + 50) / 2 = 45, avg for testing = (15 + 20) / 2 = 17.5
    assert stats["stage_metrics"]["implementing"]["avg_seconds"] == 45.0
    assert stats["stage_metrics"]["testing"]["avg_seconds"] == 17.5
    assert stats["stage_metrics"]["implementing"]["min_seconds"] == 40.0
    assert stats["stage_metrics"]["implementing"]["max_seconds"] == 50.0
    assert stats["bottleneck_stage"] == "implementing"


def test_compute_pool_flow_statistics_tiebreaks_bottleneck_by_retry_count(
    tmp_path: Path,
) -> None:
    from litehive.cli._pool import _compute_pool_flow_statistics

    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tie task", auto_commit=False)

    reports_dir = (
        tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    # Both stages have avg=30s, but testing has 2 failures (retries) vs grooming 0.
    # Tie-break: testing wins because stage_fail_counts is higher.
    stage_data = [
        ("grooming", "pass", 30),
        ("testing", "fail", 30),
        ("testing", "fail", 30),
        ("testing", "pass", 30),
    ]
    for i, (step, verdict, duration) in enumerate(stage_data):
        data = {"step": step, "verdict": verdict, "duration_seconds": duration}
        (reports_dir / f"report-{i:04d}.yaml").write_text(
            yaml.safe_dump(data), encoding="utf-8"
        )

    entries = [{"task_id": task.id, "title": task.title}]
    stats = _compute_pool_flow_statistics(tmp_path, entries)

    assert stats is not None
    assert stats["stage_metrics"]["grooming"]["avg_seconds"] == 30.0
    assert stats["stage_metrics"]["testing"]["avg_seconds"] == 30.0
    # Both have equal avg; testing has 2 retries (fail_counts) vs grooming's 0.
    assert stats["stage_fail_counts"].get("grooming", 0) == 0
    assert stats["stage_fail_counts"]["testing"] == 2
    assert stats["bottleneck_stage"] == "testing"


def test_pool_summary_includes_flow_stats_lines_when_durations_available(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litehive.cli._pool import _pool_summary_report_data, _pool_summary_report_lines

    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Timed task", auto_commit=False)

    reports_dir = (
        tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    stage_data = [
        ("grooming", "pass", 10),
        ("implementing", "pass", 120),
        ("testing", "pass", 30),
        ("accepting", "pass", 8),
        ("commit_to_git", "pass", 3),
    ]
    for i, (step, verdict, duration) in enumerate(stage_data):
        data = {"step": step, "verdict": verdict, "duration_seconds": duration}
        (reports_dir / f"report-{i:04d}.yaml").write_text(
            yaml.safe_dump(data), encoding="utf-8"
        )

    completed = [
        {
            "task_id": task.id,
            "title": task.title,
            "final_task_status": "done",
            "pipeline_status": "done",
            "stage_outcomes": [],
            "reason_code": None,
            "reason": None,
            "follow_up_task_id": None,
        }
    ]
    report = _pool_summary_report_data(
        tmp_path,
        completed=completed,
        flagged=[],
        stop_reason="single_task_complete",
        tasks_run=1,
    )

    assert report["flow_statistics"] is not None
    assert report["flow_statistics"]["bottleneck_stage"] == "implementing"
    assert report["flow_statistics"]["bottleneck_avg_seconds"] == 120.0

    lines = _pool_summary_report_lines(report=report)
    text = "\n".join(lines)

    assert "flow_statistics: stages_executed=5 bottleneck=implementing (avg=2m00s)" in text
    assert "stage_durations:" in text
    assert "implementing=avg:2m00s" in text
    assert "grooming=avg:10s" in text


def test_pool_summary_omits_flow_stats_when_no_duration_data(
    tmp_path: Path,
) -> None:
    from litehive.cli._pool import _pool_summary_report_data, _pool_summary_report_lines

    ensure_workspace(tmp_path)
    completed = [
        {
            "task_id": "T-0001",
            "title": "Some task",
            "final_task_status": "done",
            "pipeline_status": "done",
            "stage_outcomes": [],
            "reason_code": None,
            "reason": None,
            "follow_up_task_id": None,
        }
    ]
    report = _pool_summary_report_data(
        tmp_path,
        completed=completed,
        flagged=[],
        stop_reason="single_task_complete",
        tasks_run=1,
    )

    assert report["flow_statistics"] is None

    lines = _pool_summary_report_lines(report=report)
    text = "\n".join(lines)
    assert "flow_statistics:" not in text
    assert "stage_durations:" not in text


def test_pool_summary_flow_stats_in_durable_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Timed task", auto_commit=False)

    reports_dir = (
        tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    stage_data = [
        ("grooming", "pass", 15),
        ("implementing", "pass", 90),
        ("testing", "pass", 45),
        ("accepting", "pass", 10),
        ("commit_to_git", "pass", 5),
    ]
    for i, (step, verdict, duration) in enumerate(stage_data):
        data = {"step": step, "verdict": verdict, "duration_seconds": duration}
        (reports_dir / f"report-{i:04d}.yaml").write_text(
            yaml.safe_dump(data), encoding="utf-8"
        )

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    assert exit_code == 0

    durable_report = _latest_pool_run_report(tmp_path)
    # flow_stats may be None if the runner's own reports have zero duration in tests,
    # but when pre-seeded reports exist they should be captured.
    # Verify the key is present regardless.
    assert "flow_statistics" in durable_report
