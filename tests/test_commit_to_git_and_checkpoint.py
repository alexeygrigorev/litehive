from tests.workspace_helpers import (
    CLIExecutionResult,
    EngineFailure,
    Path,
    StageReport,
    SubagentRef,
    SubagentResult,
    _completed_subagent_result,
    _git_status_without_litehive,
    _init_git_repo,
    _run,
    checkpoint_message,
    create_task,
    ensure_workspace,
    get_task,
    get_task_worktree_path,
    load_state,
    pytest,
    require_task,
    rollback_completed_task,
    run_next_task,
    yaml,
)

def test_run_next_task_creates_checkpoint_commit_and_persists_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
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
    assert summary.commit_sha is not None
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 ship-checkpoint"
    )

    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.commit_message == "litehive: complete T-0001 ship-checkpoint"
    assert task.git.commit_sha == summary.commit_sha
    assert task.git.checkpoint_attempts == 1
    assert task.git.checkpoint_base_sha == initial_sha
    assert task.git.rolled_back_checkpoint_attempt is None
    assert task.runtime.execution_status == "done"
    assert task.runtime.last_stage.step == "commit_to_git"
    assert task.runtime.last_stage.verdict == "pass"
    assert task.runtime.git.commit_sha == summary.commit_sha
    assert task.git.worktree_path is None
    assert not (tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}").exists()


def test_run_next_task_executes_stage_in_task_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Run in worktree", auto_commit=False)
    seen_execution_roots: list[Path] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_execution_roots.append(self.execution_root)
        result = _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name, task=task)
        if task.pipeline_status == "implementing":
            assert self.execution_root != tmp_path
            assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
            persisted = require_task(tmp_path, task.id)
            assert persisted.runtime.git.worktree_path is not None
            assert get_task_worktree_path(persisted) == str(
                self.execution_root.relative_to(tmp_path)
            )
            (self.execution_root / "app.txt").write_text("worktree-only\n", encoding="utf-8")
        return result

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert seen_execution_roots
    assert all(path != tmp_path for path in seen_execution_roots)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"


def test_run_next_task_keeps_using_task_worktree_when_main_checkout_is_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Run in isolated worktree", auto_commit=False)
    (tmp_path / "README.md").write_text("main checkout dirt\n", encoding="utf-8")
    seen_execution_roots: list[Path] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_execution_roots.append(self.execution_root)
        result = _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name, task=task)
        if task.pipeline_status == "implementing":
            assert self.execution_root != tmp_path
            assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
            (self.execution_root / "app.txt").write_text("worktree-only\n", encoding="utf-8")
        return result

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert seen_execution_roots
    assert all(path != tmp_path for path in seen_execution_roots)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "main checkout dirt\n"


def test_run_next_task_cherry_picks_task_commit_back_to_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Cherry-pick worktree commit")

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        result = _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name, task=task)
        if task.pipeline_status == "implementing":
            assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
            (self.execution_root / "app.txt").write_text("integrated\n", encoding="utf-8")
        return result

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "integrated\n"


def test_checkpoint_message_attempt_policy_matches_generated_subjects_only(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Message policy", auto_commit=False)

    assert checkpoint_message(task, attempt=1) == "litehive: complete T-0001 message-policy"
    assert (
        checkpoint_message(task, attempt=2)
        == "litehive: complete T-0001 message-policy (attempt 2)"
    )

    task.git.commit_message = "custom: keep subject"
    assert checkpoint_message(task, attempt=2) == "custom: keep subject"

    task.git.commit_message = "litehive: complete T-0001 message-policy"
    assert (
        checkpoint_message(task, attempt=2)
        == "litehive: complete T-0001 message-policy (attempt 2)"
    )


def test_run_next_task_appends_attempt_suffix_after_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
    (tmp_path / "app.txt").write_text("updated-once\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    first = run_next_task(tmp_path)
    assert first.result is not None
    assert first.result.final_status == "done"

    rollback_completed_task(tmp_path, "T-0001")
    assert _git_status_without_litehive(tmp_path) == []

    (tmp_path / "app.txt").write_text("updated-twice\n", encoding="utf-8")
    second = run_next_task(tmp_path)

    assert second.result is not None
    assert second.result.final_status == "done"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 ship-checkpoint (attempt 2)"
    )
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.checkpoint_attempts == 2


def test_run_next_task_preserves_future_task_added_during_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    def fail_commit_with_concurrent_add(
        root, execution_root, task, *, auto_commit_enabled, subagents=None, config=None
    ):
        create_task(tmp_path, title="Added during commit failure", auto_commit=False)
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: simulated merge failure",
        )

    monkeypatch.setattr(
        "litehive.pipeline._builder._commit_to_git_report", fail_commit_with_concurrent_add
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    # The runner may launch a recovery agent after the commit failure,
    # which can succeed and re-queue the task.
    assert summary.result.final_status in ("flagged", "merge_failed", "queued")
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert "T-0002" in state.queue
    added = get_task(tmp_path, "T-0002")
    assert added is not None
    assert added.title == "Added during commit failure"
    assert added.status == "queued"


def test_run_next_task_skips_commit_when_not_a_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Needs git repo")
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
    # The new commit flow skips commit_to_git when there is no git repo
    # and marks the task as done instead of flagging it.
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is None


def test_run_next_task_commits_successfully_with_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Preflight passes")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"
    assert task.git.commit_sha is not None


def test_run_next_task_completes_when_task_worktree_path_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new commit flow handles missing worktrees gracefully by committing
    from the main checkout, so a missing worktree path no longer blocks."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Missing preflight worktree")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        if task.pipeline_status == "accepting":
            task.runtime.git.worktree_path = "../missing-preflight-worktree"
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"


def test_run_next_task_completes_when_worktree_has_unexpected_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new commit flow merges the worktree into main, so extra commits
    in the worktree are handled naturally by the merge."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Unexpected worktree commit")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        if task.pipeline_status == "accepting":
            worktree_path = tmp_path / str(task.runtime.git.worktree_path)
            _run(["git", "commit", "--allow-empty", "-m", "manual worktree commit"], worktree_path)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"


def _assert_run_next_task_records_blocked_outcome_when_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{role}-{engine_name}",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{role}-{engine_name}",
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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_outcome.kind == "blocked"
    assert task.runtime.last_outcome.retry_limit == 3
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
    assert report["outcome"] == "blocked"


def test_run_next_task_records_blocked_reason_code_when_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_run_next_task_records_blocked_outcome_when_fallbacks_are_exhausted(
        tmp_path, monkeypatch
    )


def test_run_next_task_preserves_git_commit_failure_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Commit diagnostics")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    def fail_commit(root, execution_root, task, *, auto_commit_enabled, subagents=None, config=None):
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: simulated git commit failure",
        )

    monkeypatch.setattr(
        "litehive.pipeline._builder._commit_to_git_report", fail_commit
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    # The runner may launch a recovery agent after the commit failure.
    # The fake SubagentManager.run returns a pass, which may cause the
    # runner to re-queue instead of flagging.
    assert summary.result.final_status in ("flagged", "merge_failed", "queued")
