from tests.workspace_helpers import *  # noqa: F401,F403

def test_run_next_task_no_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    summary = run_next_task(tmp_path)

    assert summary.task is None
    assert summary.result is None

def test_drain_task_pool_no_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    summary = drain_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "queue_exhausted"

def test_drain_task_pool_drains_dynamic_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if task.id == first.id and get_task(tmp_path, "T-0002") is None:
            create_task(tmp_path, title="Second task", auto_commit=False)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [
        "T-0001",
        "T-0002",
    ]
    assert summary.stop_reason == "queue_exhausted"
    assert load_state(tmp_path).active_task_id is None
    assert load_state(tmp_path).queue == []
    second = get_task(tmp_path, "T-0002")
    assert second is not None
    assert second.status == "done"

def test_run_next_task_falls_back_to_next_engine_on_execution_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fallback task", engine="codex", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if engine_name == "codex":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="rate limit exceeded",
                    stderr="",
                ),
                transcript="rate limit exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="rate limit reached"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `opencode` after rate limit reached."
        in report["warnings"]
    )

def test_run_next_task_flags_when_limit_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{task.pipeline_status}-{engine_name}",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{task.pipeline_status}-{engine_name}",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=1,
                stdout="quota exceeded",
                stderr="",
            ),
            transcript="quota exceeded",
            exit_code=1,
            failure=EngineFailure(kind="execution_limit", reason="quota exceeded"),
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-exhausted-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert report["summary"] == "grooming blocked after exhausting engine fallbacks: quota exceeded"

def test_drain_task_pool_stops_by_default_when_limit_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="quota exceeded",
                    stderr="",
                ),
                transcript="quota exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="quota exceeded"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "execution_limit_fallbacks_exhausted"
    state = load_state(tmp_path)
    assert state.queue == ["T-0002"]
    assert state.pool_stop_reason == "execution_limit_fallbacks_exhausted"
    journal = (
        tmp_path / ".litehive" / "tasks" / "T-0001-exhausted-fallback-task" / "journal.md"
    ).read_text(encoding="utf-8")
    assert "Pool stopped: execution_limit_fallbacks_exhausted." in journal
    assert "grooming blocked after exhausting engine fallbacks: quota exceeded" in journal

def test_drain_task_pool_rereads_queue_order_between_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)
    third = create_task(tmp_path, title="Third task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if task.id == first.id:
            move_queued_task(tmp_path, third.id, 1)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [
        first.id,
        third.id,
        second.id,
    ]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []

def test_drain_task_pool_allows_future_queue_mutation_during_active_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)
    started = threading.Event()
    resume = threading.Event()
    completed: dict[str, object] = {}
    failures: list[BaseException] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if task.id == first.id:
            started.set()
            assert resume.wait(timeout=5)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    def run_pool() -> None:
        try:
            completed["summary"] = drain_task_pool(tmp_path)
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            failures.append(exc)
            resume.set()

    thread = threading.Thread(target=run_pool)
    thread.start()
    assert started.wait(timeout=5)

    third = create_task(tmp_path, title="Third task", auto_commit=False)
    move_queued_task(tmp_path, third.id, 1)
    updated = update_task_metadata(
        tmp_path,
        third.id,
        priority="high",
        goal="Run before the older pending work once the active task finishes.",
    )

    with pytest.raises(
        WorkspaceConflictError,
        match="runner is actively using task state that cannot be changed concurrently",
    ):
        update_task_metadata(tmp_path, first.id, priority="high")

    with pytest.raises(
        WorkspaceConflictError,
        match="workspace is already being mutated by another runner",
    ):
        dequeue_next_task_selection(tmp_path)

    resume.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []

    assert "summary" in completed
    summary = completed["summary"]
    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [
        first.id,
        third.id,
        second.id,
    ]
    assert summary.stop_reason == "queue_exhausted"
    refreshed = get_task(tmp_path, third.id)
    assert refreshed is not None
    assert refreshed.priority == "high"
    assert refreshed.goal == "Run before the older pending work once the active task finishes."
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []
    assert updated.id == third.id

def test_drain_task_pool_picks_up_requeued_task_between_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    requeued = create_task(tmp_path, title="Retried task", auto_commit=False)
    requeued.status = "flagged"
    requeued.pipeline_status = "testing"
    save_task(tmp_path, requeued)

    state = load_state(tmp_path)
    state.queue = [first.id]
    save_state(tmp_path, state)
    requeued_once = False

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        nonlocal requeued_once
        if task.id == first.id and not requeued_once:
            requeue_task(tmp_path, requeued.id)
            requeued_once = True
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [
        first.id,
        requeued.id,
    ]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []
    refreshed = get_task(tmp_path, requeued.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"

def test_drain_task_pool_honors_stop_condition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path, stop_when=lambda executions: len(executions) >= 1)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "stop_condition_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]

def test_drain_task_pool_restores_preselected_active_task_when_stop_condition_hits(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == first.id
    assert load_state(tmp_path).queue == [second.id]

    summary = drain_task_pool(tmp_path, stop_when=lambda executions: True)

    assert summary.executions == []
    assert summary.stop_reason == "stop_condition_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [second.id, first.id]

def test_drain_task_pool_pauses_for_human_checkpoint_before_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    checkpointed = create_task(
        tmp_path,
        title="Needs review before acceptance",
        human_checkpoints=["before_acceptance"],
        acceptance_criteria=["Feature works correctly."],
        auto_commit=False,
    )
    queued = create_task(
        tmp_path,
        title="Waiting behind review",
        acceptance_criteria=["Feature works correctly."],
        auto_commit=False,
    )

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [checkpointed.id]
    assert summary.stop_reason == "human_checkpoint_before_acceptance"
    refreshed = get_task(tmp_path, checkpointed.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "accepting"
    assert refreshed.runtime.execution_status == "paused"
    assert load_state(tmp_path).queue == [checkpointed.id, queued.id]

    resume_summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in resume_summary.executions if execution.task is not None
    ] == [checkpointed.id, queued.id]
    assert resume_summary.stop_reason == "queue_exhausted"
    refreshed = get_task(tmp_path, checkpointed.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"

def test_drain_task_pool_pauses_for_human_checkpoint_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    checkpointed = create_task(
        tmp_path,
        title="Needs review before commit",
        human_checkpoints=["before_commit"],
        acceptance_criteria=["Feature works correctly."],
        auto_commit=False,
    )
    queued = create_task(
        tmp_path,
        title="Waiting behind commit review",
        acceptance_criteria=["Feature works correctly."],
        auto_commit=False,
    )

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [checkpointed.id]
    assert summary.stop_reason == "human_checkpoint_before_commit"
    refreshed = get_task(tmp_path, checkpointed.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "paused"
    assert load_state(tmp_path).queue == [checkpointed.id, queued.id]

    resume_summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in resume_summary.executions if execution.task is not None
    ] == [checkpointed.id, queued.id]
    assert resume_summary.stop_reason == "queue_exhausted"
    refreshed = get_task(tmp_path, checkpointed.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"

def test_restore_untouched_active_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)
    set_active_task(tmp_path, first.id)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        restore_untouched_active_task(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id == first.id
    assert restored.queue == [second.id]
    refreshed = get_task(tmp_path, first.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"

def test_restore_untouched_active_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    interrupted = create_task(tmp_path, title="Halted task", auto_commit=False)

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

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, interrupted),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        restore_untouched_active_task(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id == interrupted.id
    assert restored.queue == [queued.id]
    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.current_stage.status == "running"
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted run recovered. Resume from `testing`." not in journal

def test_restore_untouched_active_task_requeues_interrupted_commit_stage_task(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    stranded = create_task(tmp_path, title="Halted commit stage", auto_commit=False)

    stranded.status = "in_progress"
    stranded.pipeline_status = "commit_to_git"
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

    restore_untouched_active_task(tmp_path)

    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.run_started_at is None
    assert refreshed.runtime.current_stage.step == "commit_to_git"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.source == "runner"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id, stranded.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `commit_to_git` was running." in journal
    assert "Resume from `commit_to_git`." in journal

def test_restore_untouched_active_task_requeues_interrupted_non_commit_task(tmp_path: Path) -> None:
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

    restore_untouched_active_task(tmp_path)

    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.run_started_at is None
    assert refreshed.runtime.current_stage.step == "testing"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.source == "runner"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id, interrupted.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `testing` was running." in journal
    assert "Resume from `testing`." in journal

def test_repair_workspace_state_requeues_system_interrupted_task_but_not_cli_stopped_task(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    system_task = create_task(tmp_path, title="System halted task", auto_commit=False)
    parked_task = create_task(tmp_path, title="CLI stopped task", auto_commit=False)

    system_task.status = "interrupted"
    system_task.pipeline_status = "testing"
    system_task.runtime.execution_status = "interrupted"
    system_task.runtime.interruption = RuntimeInterruptionState(
        source="runner",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="runner died",
        summary="Interrupted run recovered. Resume from `testing`.",
    )
    save_task(tmp_path, system_task)
    save_task_runtime(tmp_path, system_task)

    parked_task.status = "parked"
    parked_task.pipeline_status = "testing"
    parked_task.runtime.execution_status = "interrupted"
    parked_task.runtime.interruption = RuntimeInterruptionState(
        source="runner",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="Task stopped via CLI",
        summary="Execution interrupted via `litehive stop`. Resume from `testing`.",
    )
    save_task(tmp_path, parked_task)
    save_task_runtime(tmp_path, parked_task)

    repair_workspace_state(tmp_path)

    state = load_state(tmp_path)
    assert system_task.id in state.queue
    assert parked_task.id not in state.queue

def test_repair_workspace_state_restores_flagged_task_into_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Flagged reprocess task", auto_commit=False)
    flagged.status = "flagged"
    flagged.pipeline_status = "testing"
    flagged.runtime.execution_status = "flagged"
    save_task(tmp_path, flagged)
    save_task_runtime(tmp_path, flagged)

    repair_workspace_state(tmp_path)

    state = load_state(tmp_path)
    assert flagged.id in state.queue

def test_dequeue_next_task_selection_recovers_flagged_task_to_implementation_entry_stage(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Recover flagged task", auto_commit=False)
    flagged.status = "flagged"
    flagged.pipeline_status = "testing"
    flagged.runtime.execution_status = "flagged"
    save_task(tmp_path, flagged)
    save_task_runtime(tmp_path, flagged)

    state = load_state(tmp_path)
    state.queue = [flagged.id]
    save_state(tmp_path, state)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == flagged.id
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.execution_status == "idle"

def test_resolve_next_task_reconciles_orphaned_non_commit_running_task_before_new_work(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    new_task = create_task(tmp_path, title="New task", auto_commit=False)
    orphaned = create_task(tmp_path, title="Orphaned testing task", auto_commit=False)

    orphaned.status = "in_progress"
    orphaned.pipeline_status = "testing"
    orphaned.runtime.execution_status = "running"
    orphaned.runtime.current_stage = RuntimeStageState(
        step="testing",
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
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.current_stage.step == "testing"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert load_state(tmp_path).queue == [orphaned.id, new_task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Reconciled stale runner state and requeued the task at `testing`." in journal

def test_restore_untouched_active_task_requeues_stranded_done_commit_without_checkpoint(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    stranded = create_task(tmp_path, title="Halted save stage", auto_commit=False)

    stranded.status = "done"
    stranded.pipeline_status = "done"
    stranded.git.checkpoint_attempts = 1
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

    restore_untouched_active_task(tmp_path)

    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.git.commit_sha is None
    assert refreshed.runtime.execution_status == "interrupted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id, stranded.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `commit_to_git` was running." in journal
    assert "Resume from `commit_to_git`." in journal

def test_peek_next_task_selection_auto_recovers_stale_runner_state(tmp_path: Path) -> None:
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

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": 999999, "started_at": "2026-04-01T00:00:00+00:00"}, sort_keys=False),
        encoding="utf-8",
    )

    selection = peek_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == interrupted.id
    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.active_subagent is None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.status == "interrupted"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert refreshed.subagents[-1].status == "interrupted"
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [interrupted.id, queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted subagent execution while `testing` was running." in journal
    assert "Resume from `testing`." in journal

def test_dequeue_next_task_selection_rolls_back_claim_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    _fail_atomic_write_on_path(
        monkeypatch,
        tmp_path / ".litehive" / "state.yaml",
        message="state write failed",
    )

    with pytest.raises(OSError, match="state write failed"):
        dequeue_next_task_selection(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id is None
    assert restored.queue == [first.id, second.id]
    refreshed = get_task(tmp_path, first.id)
    assert refreshed is not None
    assert refreshed.status == "queued"

def test_dequeue_next_task_selection_rolls_back_claim_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, first),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        dequeue_next_task_selection(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id is None
    assert restored.queue == [first.id, second.id]
    refreshed = get_task(tmp_path, first.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.runtime.execution_status == "idle"

def test_dequeue_next_task_selection_rolls_back_claim_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, first),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        dequeue_next_task_selection(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id is None
    assert restored.queue == [first.id, second.id]
    refreshed = get_task(tmp_path, first.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.runtime.execution_status == "idle"

def test_finish_task_run_transition_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finish task", auto_commit=False)
    set_active_task(tmp_path, task.id)

    task.runtime.execution_status = "running"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path=".litehive/tasks/T-0001-finish-task/subagents/SA-0001",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
    )
    save_task_runtime(tmp_path, task)
    task.status = "done"
    task.pipeline_status = "done"

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        finish_task_run_transition(tmp_path, task, "done")

    restored = load_state(tmp_path)
    assert restored.active_task_id == task.id
    assert restored.queue == []
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.active_subagent is not None

def test_finish_task_run_transition_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finish task", auto_commit=False)
    set_active_task(tmp_path, task.id)

    task.runtime.execution_status = "running"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path=".litehive/tasks/T-0001-finish-task/subagents/SA-0001",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
    )
    save_task_runtime(tmp_path, task)
    task.status = "done"
    task.pipeline_status = "done"

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        finish_task_run_transition(tmp_path, task, "done")

    restored = load_state(tmp_path)
    assert restored.active_task_id == task.id
    assert restored.queue == []
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.active_subagent is not None

def test_finish_task_run_transition_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finish task", auto_commit=False)
    set_active_task(tmp_path, task.id)

    task.runtime.execution_status = "running"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path=".litehive/tasks/T-0001-finish-task/subagents/SA-0001",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
    )
    save_task_runtime(tmp_path, task)
    task.status = "done"
    task.pipeline_status = "done"

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        finish_task_run_transition(tmp_path, task, "done")

    restored = load_state(tmp_path)
    assert restored.active_task_id == task.id
    assert restored.queue == []
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.active_subagent is not None

def test_drain_task_pool_stops_after_max_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(max_tasks=1))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "max_tasks_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]

def test_drain_task_pool_stops_on_first_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Failing task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="quota exceeded",
                    stderr="",
                ),
                transcript="quota exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="quota exceeded"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(
        tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_failure=True)
    )

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "failure_detected"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]

def test_drain_task_pool_stops_on_execution_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="budget exceeded",
                    stderr="",
                ),
                transcript="budget exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="budget limit reached"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(
        tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_execution_limit=True)
    )

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "execution_limit_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]

def test_drain_task_pool_stops_on_quota_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Third task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if task.id in {"T-0001", "T-0002"}:
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="quota exceeded",
                    stderr="",
                ),
                transcript="quota exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="quota exceeded"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(quota_threshold=2))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001", "T-0002"]
    assert summary.stop_reason == "quota_threshold_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0003"]

def test_drain_task_pool_stops_on_budget_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Budget task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="budget exceeded",
                    stderr="",
                ),
                transcript="budget exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="budget limit reached"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(budget_threshold=1))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "budget_threshold_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]

def test_drain_task_pool_stops_on_dirty_git_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    summary = drain_task_pool(
        tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_dirty_git=True)
    )

    assert summary.executions == []
    assert summary.stop_reason == "dirty_git_state"

def test_run_single_task_allows_dirty_git_owned_by_interrupted_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    task = create_task(tmp_path, title="Interrupted task", auto_commit=False)
    task.status = "interrupted"
    task.pipeline_status = "testing"
    task.acceptance_criteria = ["Resume the interrupted testing stage."]
    task.runtime.execution_status = "interrupted"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="interrupted",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="blocked",
        summary="Execution interrupted. Resume from `testing`.",
    )
    task.runtime.interruption = RuntimeInterruptionState(
        source="runner",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="Interrupted run recovered after stale runner detection.",
        summary="Resume from `testing`.",
        interrupted_at="2026-04-01T00:01:00+00:00",
        detected_at="2026-04-01T00:01:05+00:00",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    (task_dir(tmp_path, task) / "reports" / "implementing-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "implementing",
                "verdict": "pass",
                "summary": "implemented task changes",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "app.txt").write_text("dirty\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, current_task.pipeline_status)
        ),
    )

    summary = run_single_task(
        tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_dirty_git=True)
    )

    assert summary.execution is not None
    assert summary.execution.task is not None
    assert summary.execution.task.id == task.id
    assert summary.stop_reason == "single_task_complete"


def test_run_single_task_blocks_dirty_git_owned_by_parked_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    task = create_task(tmp_path, title="Parked task", auto_commit=False)
    task.status = "parked"
    task.pipeline_status = "testing"
    task.acceptance_criteria = ["Resume the parked testing stage manually."]
    task.runtime.execution_status = "interrupted"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="interrupted",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="blocked",
        summary="Execution interrupted via `litehive stop`. Resume from `testing`.",
    )
    task.runtime.interruption = RuntimeInterruptionState(
        source="runner",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="Task stopped via CLI",
        summary="Execution interrupted via `litehive stop`. Resume from `testing`.",
        interrupted_at="2026-04-01T00:01:00+00:00",
        detected_at="2026-04-01T00:01:05+00:00",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    (task_dir(tmp_path, task) / "reports" / "implementing-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "implementing",
                "verdict": "pass",
                "summary": "implemented task changes",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "app.txt").write_text("dirty\n", encoding="utf-8")

    def fail_stage(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("parked task should not run past dirty-worktree gate")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fail_stage)

    summary = run_single_task(
        tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_dirty_git=True)
    )

    assert summary.execution is None
    assert summary.stop_reason == "dirty_git_state"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "parked"
    assert refreshed.pipeline_status == "testing"


def test_drain_task_pool_stops_on_pool_usage_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(pool_usage_cap=4))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "pool_usage_cap_reached"
    state = load_state(tmp_path)
    assert state.queue == ["T-0002"]

def test_drain_task_pool_stops_on_pool_cost_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True))
    create_task(tmp_path, title="First task", engine="claude", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status)
        ),
    )

    summary = drain_task_pool(
        tmp_path,
        stop_conditions=TaskPoolStopConditions(
            pool_cost_cap=12,
            engine_costs={"claude": 3, "codex": 1},
        ),
    )

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "pool_cost_cap_reached"
    state = load_state(tmp_path)
    assert state.queue == ["T-0002"]

def test_run_next_task_skips_engine_when_usage_cap_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fallback task", engine="codex", auto_commit=False)
    calls: list[str] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        calls.append(engine_name)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task(
        tmp_path,
        require_task(tmp_path, "T-0001"),
        budget_ledger=EngineBudgetLedger(
            engine_usage_caps={"codex": 0},
            engine_costs={"codex": 1, "opencode": 1},
        ),
    )

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert calls
    assert calls[0] == "opencode"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"

def test_run_next_task_blocks_when_claude_budget_is_exhausted_before_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            claude_enabled=True,
            engine_budget_caps={"claude": 2},
            engine_costs={"claude": 3},
        ),
    )
    create_task(tmp_path, title="Claude task", engine="claude", auto_commit=False)

    def fail_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("SubagentManager.run should not be called when claude is over budget")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fail_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-claude-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert "engine budget cap reached for `claude`" in report["summary"]
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []

def test_resolve_next_task_prefers_active_without_mutating_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")

    set_active_task(tmp_path, first.id)
    state_before = load_state(tmp_path)

    task = resolve_next_task(tmp_path)
    state_after = load_state(tmp_path)

    assert task is not None
    assert task.id == first.id
    assert state_before == state_after
    assert second.id in state_after.queue

def test_resolve_next_task_clears_stale_active_and_returns_queued_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending task")
    # Simulate a stale active_task_id by writing state directly (T-9999 does not exist on disk)
    state = load_state(tmp_path)
    state.active_task_id = "T-9999"
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == queued.id
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id]

def test_resolve_next_task_skips_ineligible_active_and_queue_entries(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Flagged active task")
    queued = create_task(tmp_path, title="Real pending task")
    completed = create_task(tmp_path, title="Completed prior task")

    active.status = "flagged"
    save_task(tmp_path, active)
    completed.status = "done"
    completed.pipeline_status = "done"
    save_task(tmp_path, completed)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [completed.id, queued.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == active.id

def test_resolve_next_task_prefers_ready_prerequisite_over_earlier_blocked_dependent(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked dependent")
    unrelated = create_task(tmp_path, title="Unrelated ready task")
    prerequisite = create_task(tmp_path, title="Ready prerequisite")

    blocked.depends_on = [prerequisite.id]
    save_task(tmp_path, blocked)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == unrelated.id

def test_resolve_next_task_fifo_prefers_earliest_ready_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="fifo"))
    first = create_task(tmp_path, title="First ready task")
    second = create_task(tmp_path, title="Second ready task")

    second.priority = "high"
    save_task(tmp_path, second)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == first.id

def test_resolve_next_task_priority_first_prefers_high_priority_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="priority_first"))
    first = create_task(tmp_path, title="First ready task")
    second = create_task(tmp_path, title="Second ready task")

    first.priority = "low"
    second.priority = "high"
    save_task(tmp_path, first)
    save_task(tmp_path, second)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == second.id

def test_resolve_next_task_priority_first_still_resumes_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="priority_first"))
    interrupted = create_task(tmp_path, title="Halted task")
    queued = create_task(tmp_path, title="New high priority task")

    queued.priority = "high"
    save_task(tmp_path, queued)
    set_active_task(tmp_path, interrupted.id)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == interrupted.id

def test_resolve_next_task_fifo_prefers_interrupted_queued_task_before_new_work(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="fifo"))
    new_task = create_task(tmp_path, title="New task")
    interrupted = create_task(tmp_path, title="Halted task")

    new_task.priority = "high"
    interrupted.priority = "low"
    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    save_task(tmp_path, new_task)
    save_task(tmp_path, interrupted)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == interrupted.id

def test_resolve_next_task_priority_first_prefers_high_priority_queue_head(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="priority_first"))
    new_task = create_task(tmp_path, title="New task")
    interrupted = create_task(tmp_path, title="Halted task")

    new_task.priority = "high"
    interrupted.priority = "low"
    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    save_task(tmp_path, new_task)
    save_task(tmp_path, interrupted)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == new_task.id

def test_resolve_next_task_dependency_aware_respects_queue_order_before_interrupted_work(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="dependency_aware"))
    queued = create_task(tmp_path, title="Next head task")
    interrupted = create_task(tmp_path, title="Halted task")

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    save_task(tmp_path, interrupted)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == queued.id

def test_resolve_next_task_dependency_aware_respects_queue_head_before_downstream_count(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="dependency_aware"))
    first = create_task(tmp_path, title="Unrelated ready task")
    root = create_task(tmp_path, title="Dependency root")
    mid = create_task(tmp_path, title="Mid dependency")
    leaf = create_task(tmp_path, title="Leaf dependency")

    mid.depends_on = [root.id]
    leaf.depends_on = [mid.id]
    save_task(tmp_path, mid)
    save_task(tmp_path, leaf)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == first.id

def test_peek_next_task_selection_reports_blocked_dependencies(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked dependent")
    prerequisite = create_task(tmp_path, title="Prerequisite")

    blocked.depends_on = [prerequisite.id, "T-9999"]
    save_task(tmp_path, blocked)

    selection = peek_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == prerequisite.id
    assert [entry.task_id for entry in selection.blocked] == [blocked.id]
    assert selection.blocked[0].blocked_by == [
        f"{prerequisite.id} (queued/backlog)",
        "T-9999 (missing)",
    ]

def test_drain_task_pool_skips_stale_queue_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Real task", auto_commit=False)
    state = load_state(tmp_path)
    state.queue = ["T-9999", task.id]
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, current_task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [task.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []

def test_drain_task_pool_skips_ineligible_active_and_queue_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Flagged active task", auto_commit=False)
    queued = create_task(tmp_path, title="Real task", auto_commit=False)
    completed = create_task(tmp_path, title="Completed prior task", auto_commit=False)

    active.status = "flagged"
    save_task(tmp_path, active)
    completed.status = "done"
    completed.pipeline_status = "done"
    save_task(tmp_path, completed)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [completed.id, queued.id]
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, current_task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [active.id, queued.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []

def test_drain_task_pool_reports_blocked_tasks_remaining(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked task", auto_commit=False)
    missing = "T-9999"

    blocked.depends_on = [missing]
    save_task(tmp_path, blocked)

    summary = drain_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "blocked_tasks_remaining"
    assert [entry.task_id for entry in summary.blocked] == [blocked.id]
    assert summary.blocked[0].blocked_by == [f"{missing} (missing)"]
    assert load_state(tmp_path).queue == [blocked.id]

def test_drain_task_pool_reports_and_requeues_blocked_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked active task", auto_commit=False)
    blocked.depends_on = ["T-9999"]
    save_task(tmp_path, blocked)

    state = load_state(tmp_path)
    state.active_task_id = blocked.id
    state.queue = []
    save_state(tmp_path, state)

    summary = drain_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "blocked_tasks_remaining"
    assert [entry.task_id for entry in summary.blocked] == [blocked.id]
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [blocked.id]

def test_drain_task_pool_stops_after_requeueing_interrupted_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Halted task", auto_commit=False)

    def fake_run(self, current_task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "testing":
            raise KeyboardInterrupt()
        return _completed_subagent_result(tmp_path, current_task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)
    summary = drain_task_pool(tmp_path)

    assert summary.executions
    assert summary.executions[0].result is not None
    assert summary.executions[0].result.final_status == "interrupted"
    assert summary.stop_reason == "task_interrupted"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert load_state(tmp_path).queue == []

def test_runner_requeues_commit_stage_after_keyboard_interrupt(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Halted commit", auto_commit=False)
    task.pipeline_status = "commit_to_git"
    task.status = "in_progress"
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "commit_to_git":
            task.status = "done"
            task.pipeline_status = "done"
            raise KeyboardInterrupt()
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "interrupted"
    finish_task_run_transition(tmp_path, task, result.final_status)
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert load_state(tmp_path).queue == [task.id]

def test_drain_task_pool_drains_active_task_without_queued_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resumed task", auto_commit=False)
    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = []
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, current_task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [task.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []

def test_drain_task_pool_continues_after_requeueing_review_rejection_when_other_work_is_runnable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    active = create_task(tmp_path, title="Active review loop", auto_commit=False)
    queued = create_task(tmp_path, title="Waiting behind active", auto_commit=False)
    active.status = "in_progress"
    active.pipeline_status = "testing"
    active.retry_policy.max_retries = 2  # allow 1 testing fail + 1 accepting reject
    save_task(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state(tmp_path, state)
    failed_once = False

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        nonlocal failed_once
        transcript = "\n".join(
            [
                "VERDICT: PASS",
                f"SUMMARY: {task.pipeline_status} passed",
                "FILES_CHANGED:",
                "TESTS_ADDED: 0",
                "TESTS_PASSING: 0",
                "WARNINGS:",
            ]
        )
        if task.id == active.id and task.pipeline_status == "testing" and not failed_once:
            failed_once = True
            transcript = "\n".join(
                [
                    "VERDICT: FAIL",
                    "SUMMARY: qa wants another implementation pass",
                    "FILES_CHANGED:",
                    "TESTS_ADDED: 0",
                    "TESTS_PASSING: 0",
                    "WARNINGS:",
                ]
            )
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{task.id}-{task.pipeline_status}-codex",
                role=role,
                engine=engine_name,
                status="completed",
                path=f"subagents/{task.id}-{task.pipeline_status}-codex",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=0,
                stdout=transcript,
                stderr="",
            ),
            transcript=transcript,
            exit_code=0,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [active.id, active.id, queued.id]
    assert summary.stop_reason == "queue_exhausted"
    refreshed_active = get_task(tmp_path, active.id)
    assert refreshed_active is not None
    assert refreshed_active.status == "done"
    assert refreshed_active.pipeline_status == "done"
    refreshed_queued = get_task(tmp_path, queued.id)
    assert refreshed_queued is not None
    assert refreshed_queued.status == "done"
    assert refreshed_queued.pipeline_status == "done"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []

def test_drain_task_pool_stops_after_requeueing_review_rejection_when_only_blocked_work_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        transcript = "\n".join(
            [
                "VERDICT: PASS",
                f"SUMMARY: {task.pipeline_status} passed",
                "FILES_CHANGED:",
                "TESTS_ADDED: 0",
                "TESTS_PASSING: 0",
                "WARNINGS:",
            ]
        )
        if task.id == active.id and task.pipeline_status == "testing":
            task.depends_on = ["T-9998"]
            save_task(tmp_path, task)
            transcript = "\n".join(
                [
                    "VERDICT: FAIL",
                    "SUMMARY: qa wants another implementation pass",
                    "FILES_CHANGED:",
                    "TESTS_ADDED: 0",
                    "TESTS_PASSING: 0",
                    "WARNINGS:",
                ]
            )
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{task.id}-{task.pipeline_status}-codex",
                role=role,
                engine=engine_name,
                status="completed",
                path=f"subagents/{task.id}-{task.pipeline_status}-codex",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=0,
                stdout=transcript,
                stderr="",
            ),
            transcript=transcript,
            exit_code=0,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [active.id]
    assert summary.stop_reason == "blocked_tasks_remaining"
    assert [entry.task_id for entry in summary.blocked] == [active.id, blocked.id]
    refreshed_active = get_task(tmp_path, active.id)
    assert refreshed_active is not None
    assert refreshed_active.status == "queued"
    assert refreshed_active.pipeline_status == "implementing"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [active.id, blocked.id]
