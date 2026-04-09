from tests.workspace_helpers import (
    GitError,
    LitehiveConfig,
    Path,
    RuntimeStageState,
    StageReport,
    SubagentManager,
    SubagentRef,
    SubagentResult,
    TaskExecutionRunner,
    _cmd_recover,
    _cmd_rollback,
    _cmd_run,
    _commit_to_git_report,
    _completed_subagent_result,
    _fail_atomic_write_on_path,
    _git_status_without_litehive,
    _init_git_repo,
    _latest_pool_run_report,
    _run,
    _write_cli_verdict,
    argparse,
    checkpoint_message,
    create_task,
    drain_task_pool,
    ensure_workspace,
    get_task,
    load_config,
    load_state,
    pytest,
    recover_completed_task,
    repair_workspace_state,
    require_task,
    resolve_next_task,
    rollback_completed_task,
    run_next_task,
    save_state,
    save_task,
    save_task_runtime,
    task_dir,
    task_file,
    task_runtime_file,
    tasks_module,
    update_task_metadata,
    yaml,
)
from litehive.pipeline import _attempt_stage_recovery, _classify_recovery_failure_owner


def _litehive_traceback_report(task_id: str, traceback_path: str) -> StageReport:
    return StageReport(
        task_id=task_id,
        step="implementing",
        verdict="fail",
        summary="implementing failed with unhandled error: boom",
        failure_diagnostics={
            "traceback": (
                "Traceback (most recent call last):\n"
                f'  File "{traceback_path}", line 10, in run_task\n'
                "    raise RuntimeError('boom')\n"
                "RuntimeError: boom\n"
            )
        },
    )


def _recovery_report_payload(tmp_path: Path, task) -> dict[str, object]:
    return yaml.safe_load(
        (task_dir(tmp_path, task) / "recovery" / "recovery-001.yaml").read_text(encoding="utf-8")
    )


def _set_queue_state(tmp_path: Path, *task_ids: str, active_task_id: str | None = None) -> None:
    state = load_state(tmp_path)
    state.active_task_id = active_task_id
    state.queue = list(task_ids)
    save_state(tmp_path, state)


def _write_report_file(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _flag_task_for_commit_recovery(
    tmp_path: Path,
    task,
    *,
    summary: str,
    accepting_files_changed: list[str],
    commit_warning: str | None = None,
    include_last_outcome: bool = False,
) -> None:
    task.status = "flagged"
    task.pipeline_status = "commit_to_git"
    task.runtime.execution_status = "flagged"
    task.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="blocked",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="fail",
        summary=summary,
    )
    if include_last_outcome:
        task.runtime.last_outcome.kind = "flagged"
        task.runtime.last_outcome.stage = "commit_to_git"
        task.runtime.last_outcome.reason_code = "verdict_fail"
        task.runtime.last_outcome.reason = summary
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    _write_report_file(
        reports_dir / "accepting-001.yaml",
        {
            "task_id": task.id,
            "step": "accepting",
            "verdict": "pass",
            "summary": "ready for final commit",
            "files_changed": accepting_files_changed,
            "tests": {"added": 1, "passing": 1},
        },
    )
    if commit_warning is not None:
        _write_report_file(
            reports_dir / "commit_to_git-002.yaml",
            {
                "task_id": task.id,
                "step": "commit_to_git",
                "verdict": "fail",
                "summary": summary,
                "warnings": [commit_warning],
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
            },
        )


def _prepare_done_accepted_task(tmp_path: Path, title: str, content: str):
    accepted = create_task(tmp_path, title=title)
    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{accepted.id}-{accepted.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text(content, encoding="utf-8")
    accepted.status = "done"
    accepted.pipeline_status = "done"
    accepted.runtime.execution_status = "done"
    accepted.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, accepted)
    save_task_runtime(tmp_path, accepted)
    _write_report_file(
        task_dir(tmp_path, accepted) / "reports" / "accepting-001.yaml",
        {
            "task_id": accepted.id,
            "step": "accepting",
            "verdict": "pass",
            "summary": "accepted and ready for final checkpoint",
            "files_changed": ["app.txt"],
            "tests": {"added": 1, "passing": 1},
        },
    )
    return accepted, worktree_path


def _prepare_existing_checkpoint_commit(tmp_path: Path, task, initial_sha: str, content: str) -> tuple[str, str]:
    (tmp_path / "app.txt").write_text(content, encoding="utf-8")
    commit_msg = checkpoint_message(task, attempt=1)
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_msg], tmp_path)
    existing_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    task.git.checkpoint_attempts = 1
    task.git.checkpoint_base_sha = initial_sha
    task.git.commit_sha = None
    return commit_msg, existing_sha


def _mark_running_commit_stage(task) -> None:
    task.status = "in_progress"
    task.pipeline_status = "commit_to_git"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )


def _assert_requeued_for_commit_recovery(
    tmp_path: Path, task_id: str, expected_queue: list[str]
) -> None:
    refreshed = require_task(tmp_path, task_id)
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == expected_queue


def _completed_recovery_subagent_result(role: str, engine_name: str) -> SubagentResult:
    return SubagentResult(
        ref=SubagentRef(
            id="SA-recovery-1",
            role=role,
            engine=engine_name,
            status="completed",
            path="subagents/recovery-1",
            sandboxed=False,
            sandbox_summary="host",
        ),
        execution=None,
        transcript="fixed the litehive bug",
        exit_code=0,
        failure=None,
    )


def _prepare_stranded_commit_task(tmp_path: Path, title: str):
    task = create_task(tmp_path, title=title)
    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("updated\n", encoding="utf-8")
    task.status = "done"
    task.pipeline_status = "done"
    task.git.checkpoint_attempts = 1
    task.git.checkpoint_base_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    task.git.commit_sha = None
    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    _write_report_file(
        task_dir(tmp_path, task) / "reports" / "accepting-001.yaml",
        {
            "task_id": task.id,
            "step": "accepting",
            "verdict": "pass",
            "summary": "accepting complete",
            "files_changed": ["app.txt"],
            "tests": {"added": 0, "passing": 0},
        },
    )
    return task

def test_attempt_stage_recovery_launches_agent_for_litehive_traceback_with_no_source_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery now requires a Litehive source repo and records a blocker when unavailable."""
    ensure_workspace(tmp_path, LitehiveConfig(litehive_source_path="/missing/litehive"))
    task = create_task(tmp_path, title="External project task", auto_commit=False)
    save_task(tmp_path, task)

    failed_report = StageReport(
        task_id=task.id,
        step="implementing",
        verdict="fail",
        summary="implementing failed with unhandled error: boom",
        failure_diagnostics={
            "traceback": (
                "Traceback (most recent call last):\n"
                '  File "/usr/lib/python3.12/site-packages/litehive/runtime.py", line 1, in run_task\n'
                "    raise RuntimeError('boom')\n"
                "RuntimeError: boom\n"
            )
        },
    )

    run_called = False

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal run_called
        run_called = True
        raise AssertionError("recovery agent should not start without a Litehive source repo")

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    report = _attempt_stage_recovery(
        tmp_path,
        tmp_path,
        task,
        "implementing",
        failed_report,
        subagents=SubagentManager(tmp_path),
        config=load_config(tmp_path),
    )

    assert report is None
    assert run_called is False
    recovery_report = yaml.safe_load(
        (task_dir(tmp_path, task) / "recovery" / "recovery-001.yaml").read_text(encoding="utf-8")
    )
    assert recovery_report["trigger"] == "stage_failure"
    assert recovery_report["runnable_state"] == "blocked"
    assert "no Litehive source repo was available" in recovery_report["summary"]


def test_classify_recovery_failure_owner_prefers_project_paths_over_name_overlap(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(litehive_source_path="/missing/litehive"))
    failed_report = StageReport(
        task_id="T-0001",
        step="implementing",
        verdict="fail",
        summary="implementing failed with unhandled error: boom",
        failure_diagnostics={
            "traceback": (
                "Traceback (most recent call last):\n"
                f'  File "{tmp_path / "litehive" / "module.py"}", line 4, in explode\n'
                "    raise RuntimeError('boom')\n"
                "RuntimeError: boom\n"
            )
        },
    )

    owner, traceback_text, source_root = _classify_recovery_failure_owner(
        tmp_path,
        failed_report,
        config=load_config(tmp_path),
    )

    assert owner == "project"
    assert "RuntimeError: boom" in traceback_text
    assert source_root == Path("/missing/litehive")


def test_attempt_stage_recovery_launches_recovery_agent_for_litehive_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When failure_owner is litehive and litehive_source_path exists, the
    self-heal path launches a recovery agent against the litehive source tree."""
    litehive_root = tmp_path / "litehive-src"
    litehive_root.mkdir()
    _init_git_repo(litehive_root)
    ensure_workspace(tmp_path, LitehiveConfig(litehive_source_path=str(litehive_root)))
    task = create_task(tmp_path, title="External project task", auto_commit=False)
    save_task(tmp_path, task)

    failed_report = _litehive_traceback_report(
        task.id, str(litehive_root / "litehive" / "runtime.py")
    )

    observed: dict[str, object] = {}

    def fake_run(self, task_arg, role, engine_name, prompt, **kwargs):  # type: ignore[no-untyped-def]
        observed.update({"role": role, "engine": engine_name, "prompt": prompt})
        _write_cli_verdict(
            tmp_path,
            task_arg,
            "implementing",
            verdict="pass",
            message="fixed the litehive bug",
        )
        return _completed_recovery_subagent_result(role, engine_name)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    report = _attempt_stage_recovery(
        tmp_path,
        tmp_path,
        task,
        "implementing",
        failed_report,
        subagents=SubagentManager(tmp_path),
        config=load_config(tmp_path),
    )

    assert report is not None
    assert observed["role"] == "recovery"
    assert "SELF-HEAL" in observed["prompt"]
    assert "Failed subagent diagnostics:" in observed["prompt"]
    assert "submit your own detailed recovery report" in observed["prompt"]
    assert report.retry_decision == "retry"
    recovery_report = _recovery_report_payload(tmp_path, task)
    assert recovery_report["trigger"] == "litehive_self_heal"
    assert recovery_report["failure_classification"] == "litehive"


def test_attempt_stage_recovery_returns_none_when_recovery_agent_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the recovery agent cannot resolve the failure, _attempt_stage_recovery
    returns None so the runner flags the task."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="External project task", auto_commit=False)
    save_task(tmp_path, task)

    failed_report = _litehive_traceback_report(
        task.id, "/usr/lib/python3.12/site-packages/litehive/runtime.py"
    )

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(
                id="SA-recovery-1",
                role=role,
                engine=engine_name,
                status="failed",
                path="subagents/recovery-1",
                sandboxed=False,
                sandbox_summary="host",
            ),
            execution=None,
            transcript="VERDICT: FAIL\nSUMMARY: could not fix the issue",
            exit_code=1,
            failure=None,
        )

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    report = _attempt_stage_recovery(
        tmp_path,
        tmp_path,
        task,
        "implementing",
        failed_report,
        subagents=SubagentManager(tmp_path),
        config=load_config(tmp_path),
    )

    assert report is None
    recovery_report = _recovery_report_payload(tmp_path, task)
    assert recovery_report["trigger"] == "stage_failure"
    assert recovery_report["runnable_state"] == "blocked"


def test_runner_requeues_same_stage_after_successful_litehive_self_heal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="External project task",
        acceptance_criteria=["The current implementing stage should resume after self-heal."],
        auto_commit=False,
    )
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    def exploding_executor(task_arg, step):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "litehive.pipeline._attempt_stage_recovery",
        lambda *args, **kwargs: StageReport(
            task_id=task.id,
            step="implementing",
            verdict="pass",
            summary="Litehive self-heal merged to main and requeued implementing.",
            retry_decision="retry",
            failure_classification="litehive_bug",
        ),
    )

    runner = TaskExecutionRunner(
        tmp_path, exploding_executor, subagents=object(), config=load_config(tmp_path)
    )
    result = runner.run(task)

    refreshed = require_task(tmp_path, task.id)
    state = load_state(tmp_path)
    assert result.final_status == "queued"
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert state.queue[0] == task.id


def test_run_next_task_skips_commit_stage_when_auto_commit_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Skip commit", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.commit_sha is None
    assert task.runtime.git.commit_sha is None


def test_run_next_task_skips_commit_stage_when_workspace_auto_commit_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(auto_commit=False))
    create_task(tmp_path, title="Skip commit from config")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.commit_sha is None
    assert task.runtime.git.commit_sha is None


def test_run_next_task_flags_task_when_repo_has_unrelated_dirty_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Dirty repo should block commit")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("unrelated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"
    assert task.pipeline_status == "done"


def test_run_next_task_flags_task_when_other_task_state_is_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="Ship first task")
    create_task(tmp_path, title="Unrelated pending task")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.task.id == first.id
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is not None
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 ship-first-task"
    )
    task = get_task(tmp_path, first.id)
    assert task is not None
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None
    task_yaml = yaml.safe_load(
        (tmp_path / ".litehive" / "tasks" / "T-0001-ship-first-task" / "task.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert task_yaml["git"]["commit_sha"] == task.git.commit_sha


def test_rollback_command_requeues_checkpointed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fix after done")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)

    exit_code = _cmd_rollback(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    rollback_output = capsys.readouterr().out

    assert exit_code == 0
    assert "rollback_commit:" in rollback_output
    assert (
        "recovery_policy: rollback reverted the checkpoint and requeued the task" in rollback_output
    )
    assert (
        "next_commit_message: litehive: complete T-0001 fix-after-done (attempt 2)"
        in rollback_output
    )
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: rollback T-0001 fix-after-done (attempt 1)"
    )
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.git.rolled_back_checkpoint_attempt == 1
    assert load_state(tmp_path).queue == ["T-0001"]


def test_recover_command_requeues_completed_task_without_revert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Recover without revert")
    (tmp_path / "app.txt").write_text("ship-again\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    recover_output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 Recover without revert" in recover_output
    assert "pipeline_status: implementing" in recover_output
    assert (
        "recovery_policy: recover requeued the task without reverting workspace code"
        in recover_output
    )
    assert (
        "next_commit_message: litehive: complete T-0001 recover-without-revert (attempt 2)"
        in recover_output
    )
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "ship-again\n"
    assert load_state(tmp_path).queue == ["T-0001"]


def test_recover_completed_task_clears_checkpoint_pointer_and_next_run_uses_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Recover rerun")
    (tmp_path / "app.txt").write_text("first-pass\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    first = run_next_task(tmp_path)
    assert first.result is not None
    assert first.result.final_status == "done"

    recovered = recover_completed_task(tmp_path, "T-0001")
    assert recovered.git.commit_sha is None
    assert recovered.runtime.git.commit_sha is None
    assert recovered.git.checkpoint_attempts == 1
    assert recovered.git.checkpoint_base_sha == initial_sha
    assert recovered.git.rolled_back_checkpoint_attempt is None

    (tmp_path / "app.txt").write_text("second-pass\n", encoding="utf-8")
    second = run_next_task(tmp_path)

    assert second.result is not None
    assert second.result.final_status == "done"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 recover-rerun (attempt 2)"
    )
    refreshed = require_task(tmp_path, "T-0001")
    assert refreshed.git.checkpoint_attempts == 2
    assert refreshed.git.checkpoint_base_sha == first.commit_sha
    assert refreshed.git.commit_sha == second.commit_sha


def test_drain_task_pool_requires_continue_or_rollback_before_unrelated_checkpointed_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="Ship first task")
    second = create_task(tmp_path, title="Unrelated pending task")
    (tmp_path / "app.txt").write_text("first-pass\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    first_summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in first_summary.executions if execution.task is not None
    ] == [first.id]
    assert first_summary.stop_reason == "continue_or_rollback_required"
    first_task = require_task(tmp_path, first.id)
    second_task = require_task(tmp_path, second.id)
    assert first_task.status == "done"
    assert first_task.pipeline_status == "done"
    assert first_task.git.commit_sha is not None
    assert second_task.status == "queued"
    assert second_task.pipeline_status == "backlog"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [second.id]
    assert state.pool_stop_reason == "continue_or_rollback_required"
    journal = (task_dir(tmp_path, first_task) / "journal.md").read_text(encoding="utf-8")
    assert "Pool stopped: continue_or_rollback_required." in journal
    assert (
        "Either continue with a new `litehive run`/pool run or roll back the checkpoint first."
        in journal
    )

    (tmp_path / "app.txt").write_text("second-pass\n", encoding="utf-8")
    resumed = drain_task_pool(tmp_path)
    resumed_second = require_task(tmp_path, second.id)

    assert [e.task.id for e in resumed.executions if e.task is not None] == [second.id]
    assert resumed.stop_reason == "queue_exhausted"
    assert resumed_second.status == "done"
    assert resumed_second.pipeline_status == "done"
    assert resumed_second.git.commit_sha is not None
    assert load_state(tmp_path).queue == []


def test_cmd_run_drain_reports_continue_or_rollback_guidance_after_checkpoint_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship first task")
    create_task(tmp_path, title="Unrelated pending task")
    (tmp_path / "app.txt").write_text("first-pass\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "progress_status: operator_action_required" in output
    assert (
        "summary: Pool stopped after a checkpoint commit. Continue with a new run or roll back the checkpoint before unrelated queued work proceeds."
        in output
    )
    assert "stop_condition: continue or rollback required" in output
    assert "stop_reason: continue_or_rollback_required" in output
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["progress_status"] == "operator_action_required"
    assert (
        durable_report["summary"]
        == "Pool stopped after a checkpoint commit. Continue with a new run or roll back the checkpoint before unrelated queued work proceeds."
    )


def test_recover_completed_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    task = create_task(tmp_path, title="Recover without revert")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks.persistence._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        recover_completed_task(tmp_path, task.id)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == [queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task recovered for another implementation pass." not in journal


def test_recover_completed_task_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    task = create_task(tmp_path, title="Recover without revert")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        recover_completed_task(tmp_path, task.id)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha is None
    assert load_state(tmp_path).queue == [queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task recovered for another implementation pass." not in journal


def test_recover_completed_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    task = create_task(tmp_path, title="Recover without revert")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        recover_completed_task(tmp_path, task.id)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha is None
    assert load_state(tmp_path).queue == [queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task recovered for another implementation pass." not in journal


def test_drain_task_pool_recovers_stranded_commit_stage_before_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = _prepare_stranded_commit_task(tmp_path, "Stranded commit task")
    _set_queue_state(tmp_path)

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [stranded.id]
    assert summary.stop_reason == "queue_exhausted"
    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha is not None
    assert refreshed.runtime.execution_status == "done"
    assert refreshed.runtime.last_stage.step == "commit_to_git"
    assert refreshed.runtime.last_stage.verdict == "pass"
    assert load_state(tmp_path).queue == []


def test_commit_to_git_ignores_unrelated_main_checkout_changes_when_task_worktree_is_clean(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finalize isolated worktree commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("updated from task worktree\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("unrelated main checkout dirt\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready to commit",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None


def test_commit_to_git_fast_forwards_main_when_worktree_commit_is_direct_descendant(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fast forward worktree commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("fast-forwarded\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready to integrate",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.git.commit_sha is not None
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == task.git.commit_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "fast-forwarded\n"


def test_commit_to_git_cherry_picks_when_main_moved_after_worktree_started(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Cherry pick divergent worktree commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("from worktree\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready to integrate",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    (tmp_path / "README.md").write_text("main moved\n", encoding="utf-8")
    _run(["git", "add", "README.md"], tmp_path)
    _run(["git", "commit", "-m", "main changed"], tmp_path)
    _run(["git", "rev-parse", "HEAD"], tmp_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass", f"commit_to_git failed: {report.summary}"
    assert task.git.commit_sha is not None
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == task.git.commit_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "from worktree\n"


def test_commit_to_git_rebases_worktree_onto_current_main_before_integrating(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Rebase before commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)

    # Worktree edits app.txt line 2
    (worktree_path / "app.txt").write_text("base\nworktree addition\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # Main adds a new file (non-conflicting change)
    (tmp_path / "other.txt").write_text("main work\n", encoding="utf-8")
    _run(["git", "add", "other.txt"], tmp_path)
    _run(["git", "commit", "-m", "main: add other.txt"], tmp_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.git.commit_sha is not None
    # Main should have both the worktree change and the main change
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\nworktree addition\n"
    assert (tmp_path / "other.txt").read_text(encoding="utf-8") == "main work\n"


def test_commit_to_git_treats_clean_task_worktree_as_done(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Already integrated task")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "already integrated",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert task.runtime.git.worktree_path is None

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.git.worktree_path is None
    assert refreshed.runtime.git.worktree_path is None


def test_commit_to_git_integrates_existing_litehive_checkpoint_from_clean_worktree(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover clean worktree checkpoint")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("checkpointed\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.git.checkpoint_attempts = 1
    task.git.checkpoint_base_sha = initial_sha
    save_task(tmp_path, task)
    commit_message = checkpoint_message(task, attempt=1)
    _run(["git", "add", "app.txt"], worktree_path)
    _run(["git", "commit", "-m", commit_message], worktree_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "checkpointed\n"


def test_commit_to_git_reconciles_existing_checkpoint_commit_without_duplicate_retry(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume committed checkpoint")
    (tmp_path / "app.txt").write_text("checkpointed\n", encoding="utf-8")

    task.git.checkpoint_attempts = 1
    task.git.checkpoint_base_sha = initial_sha
    save_task(tmp_path, task)

    commit_message = checkpoint_message(task, attempt=1)
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_message], tmp_path)
    _run(["git", "rev-parse", "HEAD"], tmp_path)
    _run(["git", "rev-list", "--count", "HEAD"], tmp_path)

    report = _commit_to_git_report(tmp_path, tmp_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None


def test_commit_to_git_integrates_agent_precommit_in_task_worktree(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent committed early")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("agent-commit\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    _run(["git", "add", "app.txt"], worktree_path)
    _run(["git", "commit", "-m", "manual agent commit"], worktree_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.git.commit_sha is not None
    assert task.status == "done"
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "agent-commit\n"


def test_commit_to_git_runs_after_merge_hook_on_main_and_finishes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "after_commit": [
                    {"command": "grep -q '^from worktree$' app.txt", "reject_on_failure": True}
                ]
            }
        ),
    )
    task = create_task(tmp_path, title="Post-merge verification passes")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("from worktree\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)

    report = _commit_to_git_report(
        tmp_path,
        worktree_path,
        task,
        auto_commit_enabled=True,
        config=load_config(tmp_path),
    )

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert report.hook_results[0]["point"] == "after_commit"
    assert report.hook_results[0]["status"] == "passed"
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "from worktree\n"


def test_commit_to_git_requeues_implementing_when_after_merge_hook_fails(tmp_path: Path) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "after_commit": [
                    {"command": "echo post-merge failed >&2; exit 7", "reject_on_failure": True}
                ]
            }
        ),
    )
    task = create_task(tmp_path, title="Post-merge verification fails")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("merged before failure\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)

    report = _commit_to_git_report(
        tmp_path,
        worktree_path,
        task,
        auto_commit_enabled=True,
        config=load_config(tmp_path),
    )

    assert report.verdict == "blocked"
    assert report.retry_decision == "retry"
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert task.git.commit_sha != initial_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "merged before failure\n"
    assert not worktree_path.exists()
    assert report.hook_results[0]["point"] == "after_commit"
    assert report.hook_results[0]["status"] == "failed"
    assert "without reverting the merge" in report.summary


def test_commit_to_git_skips_after_merge_when_hook_not_configured(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig())
    task = create_task(tmp_path, title="No post-merge verification configured")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("no hook configured\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)

    report = _commit_to_git_report(
        tmp_path,
        worktree_path,
        task,
        auto_commit_enabled=True,
        config=load_config(tmp_path),
    )

    assert report.verdict == "pass"
    assert report.hook_results == []
    assert task.status == "done"
    assert task.pipeline_status == "done"


def test_commit_to_git_handles_metadata_only_worktree_conflict(tmp_path: Path) -> None:
    """When a worktree has only metadata changes that conflict with main's
    state files, the merge will fail. Without a subagent to resolve the
    conflict, the commit returns fail."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Metadata only commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / ".litehive" / "state.yaml").parent.mkdir(parents=True, exist_ok=True)
    (worktree_path / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\n", encoding="utf-8"
    )

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "metadata only",
                "files_changed": ["path/to/file", "none", "-", " N/A "],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    # Without a merge-resolver subagent, metadata-only conflicts cause a fail
    assert report.verdict == "fail"


def test_resolve_next_task_finalizes_existing_checkpoint_commit_without_retry(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Stranded commit task")
    new_task = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
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

    _set_queue_state(tmp_path, new_task.id, active_task_id=stranded.id)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == new_task.id
    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == existing_checkpoint_sha
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.runtime.execution_status == "done"
    assert refreshed.runtime.last_stage.step == "commit_to_git"
    assert refreshed.runtime.last_stage.verdict == "pass"
    assert refreshed.runtime.current_stage.step is None
    assert load_state(tmp_path).queue == [new_task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered existing checkpoint commit after interrupted `commit_to_git`" in journal
    assert _run(["git", "rev-list", "--count", "HEAD"], tmp_path) == "2"
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_message


def test_resolve_next_task_finalizes_running_commit_stage_with_existing_checkpoint_before_new_work(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Running commit stage")
    follow_up = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    commit_message = checkpoint_message(stranded, attempt=1)
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_message], tmp_path)
    existing_checkpoint_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    commit_count_before = _run(["git", "rev-list", "--count", "HEAD"], tmp_path)

    stranded.status = "in_progress"
    stranded.pipeline_status = "commit_to_git"
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
    state.queue = [follow_up.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == follow_up.id
    refreshed = require_task(tmp_path, stranded.id)
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == existing_checkpoint_sha
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.runtime.execution_status == "done"
    assert load_state(tmp_path).queue == [follow_up.id]
    assert _run(["git", "rev-list", "--count", "HEAD"], tmp_path) == commit_count_before
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_message


def test_resolve_next_task_recovers_orphaned_commit_stage_before_new_work(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    new_task = create_task(tmp_path, title="New task", auto_commit=False)
    orphaned = create_task(tmp_path, title="Orphaned commit stage", auto_commit=False)

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
    state.queue = [new_task.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == orphaned.id
    refreshed = get_task(tmp_path, orphaned.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.run_started_at is None
    assert refreshed.runtime.current_stage.step == "commit_to_git"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert load_state(tmp_path).queue == [orphaned.id, new_task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `commit_to_git` was running." in journal
    assert "Resume from `commit_to_git`." in journal


def test_resolve_next_task_recovers_orphaned_interrupted_commit_stage_before_new_work(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    new_task = create_task(tmp_path, title="New task", auto_commit=False)
    orphaned = create_task(tmp_path, title="Halted commit stage", auto_commit=False)

    orphaned.status = "interrupted"
    orphaned.pipeline_status = "commit_to_git"
    orphaned.runtime.execution_status = "interrupted"
    orphaned.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="interrupted",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="blocked",
        summary="Interrupted `commit_to_git` run recovered. Resume from `commit_to_git`.",
    )
    save_task(tmp_path, orphaned)
    save_task_runtime(tmp_path, orphaned)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [new_task.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == orphaned.id
    refreshed = get_task(tmp_path, orphaned.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert load_state(tmp_path).queue == [orphaned.id, new_task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `commit_to_git` was running." in journal
    assert "Resume from `commit_to_git`." in journal


def test_resolve_next_task_recovers_flagged_commit_stage_after_passing_review(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    follow_up = create_task(tmp_path, title="Later task", auto_commit=False)
    flagged = create_task(tmp_path, title="Accepted but not committed", auto_commit=False)
    _flag_task_for_commit_recovery(
        tmp_path,
        flagged,
        summary="commit never ran",
        accepting_files_changed=["litehive/tasks.py"],
    )
    _write_report_file(
        task_dir(tmp_path, flagged) / "reports" / "testing-001.yaml",
        {
            "task_id": flagged.id,
            "step": "testing",
            "verdict": "pass",
            "summary": "ready for final commit",
            "files_changed": ["litehive/tasks.py"],
            "tests": {"added": 1, "passing": 1},
        },
    )
    _set_queue_state(tmp_path, follow_up.id)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == flagged.id
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [flagged.id, follow_up.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered flagged accepted task back to `queued/commit_to_git`" in journal


def test_resolve_next_task_recovers_flagged_commit_stage_after_failed_commit_report(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    follow_up = create_task(tmp_path, title="Later task", auto_commit=False)
    flagged = create_task(tmp_path, title="Accepted but merge conflicted", auto_commit=False)
    _flag_task_for_commit_recovery(
        tmp_path,
        flagged,
        summary="CommitToGit failed: merge conflict while integrating task checkpoint",
        accepting_files_changed=["litehive/tasks.py"],
        commit_warning="merge conflict while integrating task checkpoint",
        include_last_outcome=True,
    )
    _set_queue_state(tmp_path, follow_up.id)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == flagged.id
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [flagged.id, follow_up.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered flagged accepted task back to `queued/commit_to_git`" in journal


def test_resolve_next_task_recovers_done_accepted_task_without_checkpoint_commit(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    follow_up = create_task(tmp_path, title="Later task", auto_commit=False)
    accepted, _ = _prepare_done_accepted_task(
        tmp_path, "Accepted without checkpoint", "resumed-commit\n"
    )
    _set_queue_state(tmp_path, follow_up.id)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == accepted.id
    refreshed = require_task(tmp_path, accepted.id)
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [accepted.id, follow_up.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered accepted task back to `queued/commit_to_git`" in journal


def test_commit_to_git_resumes_recovered_done_accepted_worktree_task(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    accepted, worktree_path = _prepare_done_accepted_task(
        tmp_path, "Resume final checkpoint", "runner-owned-commit\n"
    )
    _set_queue_state(tmp_path)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == accepted.id
    refreshed = require_task(tmp_path, accepted.id)
    report = _commit_to_git_report(tmp_path, worktree_path, refreshed, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.git.worktree_path is None
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "runner-owned-commit\n"


def test_repair_workspace_state_recovers_flagged_commit_stage_after_failed_commit_report(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Repair conflicted integration", auto_commit=False)
    _flag_task_for_commit_recovery(
        tmp_path,
        flagged,
        summary="CommitToGit failed: cherry-pick conflict while integrating task checkpoint",
        accepting_files_changed=["litehive/runtime.py"],
        commit_warning="cherry-pick conflict while integrating task checkpoint",
    )
    _set_queue_state(tmp_path)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is True
    assert summary.requeued_task_ids == [flagged.id]
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered flagged accepted task back to `queued/commit_to_git`" in journal


def test_rollback_completed_task_restores_state_when_rollback_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on commit failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)

    def fail_rollback_commit(root: Path, message: str):  # type: ignore[no-untyped-def]
        if message.startswith("litehive: rollback "):
            raise GitError("git rollback commit failed")
        return None

    monkeypatch.setattr("litehive.pipeline.recovery.execution_recovery.commit_task", fail_rollback_commit)

    with pytest.raises(GitError, match="git rollback commit failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.rolled_back_checkpoint_attempt is None
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_rollback_completed_task_restores_state_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on persist failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks.persistence._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_rollback_completed_task_restores_state_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on task persist failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)
    task = require_task(tmp_path, "T-0001")

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_rollback_completed_task_restores_state_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on persist failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)
    task = require_task(tmp_path, "T-0001")

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_recover_command_reroutes_large_task_without_acceptance_criteria_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Fix missing criteria",
        goal="Ship CLI tool",
        acceptance_criteria=["Task completes"],
    )
    task.priority = "high"
    save_task(tmp_path, task)
    (tmp_path / "app.txt").write_text("ship-again\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)
    update_task_metadata(tmp_path, task.id, acceptance_criteria=[])

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    recover_output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in recover_output
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in recover_output
    )
    assert (
        "Use `--acceptance-criteria` to persist at least one structured bullet."
        not in recover_output
    )
    recovered = get_task(tmp_path, task.id)
    assert recovered is not None
    assert recovered.pipeline_status == "grooming"
    assert load_state(tmp_path).queue == ["T-0001"]


def test_rollback_requires_completed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Not done yet")

    exit_code = _cmd_rollback(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "is not completed; cannot rollback" in output


def test_recover_requires_completed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Still queued")

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "is not completed; cannot recover" in output


def test_commit_to_git_rerun_reconciles_existing_checkpoint(tmp_path: Path) -> None:
    """Rerunning commit_to_git when the checkpoint commit already exists
    must record the existing SHA, mark the task done, and not create a
    second checkpoint commit."""
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Rerun checkpoint reconciliation")
    (tmp_path / "app.txt").write_text("checkpointed\n", encoding="utf-8")

    task.git.checkpoint_attempts = 1
    task.git.checkpoint_base_sha = initial_sha
    save_task(tmp_path, task)

    commit_msg = checkpoint_message(task, attempt=1)
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_msg], tmp_path)
    _run(["git", "rev-parse", "HEAD"], tmp_path)
    commit_count_before = _run(["git", "rev-list", "--count", "HEAD"], tmp_path)

    report = _commit_to_git_report(tmp_path, tmp_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None
    # No duplicate commit was created
    assert _run(["git", "rev-list", "--count", "HEAD"], tmp_path) == commit_count_before
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_msg


def test_recovery_finalizes_stranded_commit_to_git_with_existing_checkpoint(
    tmp_path: Path,
) -> None:
    """Stale-runner recovery must reconcile an existing checkpoint commit
    for a stranded commit_to_git task before queuing new work, without
    incrementing checkpoint_attempts or advancing git history."""
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Stranded commit")
    follow_up = create_task(tmp_path, title="Follow-up task", auto_commit=False)
    commit_msg, existing_sha = _prepare_existing_checkpoint_commit(
        tmp_path, stranded, initial_sha, "updated\n"
    )
    commit_count_before = _run(["git", "rev-list", "--count", "HEAD"], tmp_path)
    _mark_running_commit_stage(stranded)
    save_task(tmp_path, stranded)
    save_task_runtime(tmp_path, stranded)
    _set_queue_state(tmp_path, follow_up.id, active_task_id=stranded.id)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == follow_up.id
    refreshed = require_task(tmp_path, stranded.id)
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == existing_sha
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.runtime.execution_status == "done"
    # Git history was not advanced
    assert _run(["git", "rev-list", "--count", "HEAD"], tmp_path) == commit_count_before
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_msg
