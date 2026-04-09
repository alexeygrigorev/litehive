from tests.workspace_helpers import (
    CLIExecutionResult,
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
    Path,
    PurePosixPath,
    RuntimeStageState,
    SandboxCredentialInput,
    SandboxLauncher,
    StageReport,
    SubagentManager,
    SubagentRef,
    SubagentResult,
    TaskExecutionRunner,
    TaskRecord,
    _allowed_commit_paths,
    _cmd_resume_task,
    _cmd_run,
    _cmd_update,
    _completed_subagent_result,
    _init_git_repo,
    _repo_root,
    _role_for_step,
    _run,
    _stage_subagent_result,
    _unexpected_dirty_paths,
    _with_fake_uv,
    _write_cli_verdict,
    _write_fake_uv,
    argparse,
    classify_execution_limit,
    commit_task,
    create_task,
    ensure_workspace,
    extract_engine_continuation,
    finish_task_run_transition,
    format_external_engine_sandbox,
    get_engine,
    get_task,
    implementation_entry_stage,
    load_config,
    load_state,
    mark_subagent_started,
    needs_normalization,
    parse_stage_report_text,
    pytest,
    require_task,
    reroute_stage_for_acceptance_criteria,
    run_next_task,
    save_task,
    save_task_runtime,
    stage_prompt,
    stage_report_from_subagent,
    subprocess,
    sys,
    task_dir,
    task_requires_acceptance_criteria,
    yaml,
)


def test_runner_advances_task_to_done(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Implement feature", auto_commit=False)

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    reports = tmp_path / ".litehive" / "tasks" / "T-0001-implement-feature" / "reports"
    assert (reports / "grooming-001.yaml").exists()
    assert (reports / "implementing-002.yaml").exists()
    assert (reports / "testing-003.yaml").exists()
    assert (reports / "accepting-004.yaml").exists()
    assert (reports / "commit_to_git-005.yaml").exists()


def test_empty_swe_guard_skipped_when_prior_implementing_pass_exists(tmp_path: Path) -> None:
    """Analysis/planning tasks that re-enter implementing via recovery should
    not be rejected by the empty SWE guard when a prior implementing pass report
    already exists (the work was already done in an earlier run)."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Plan feature", auto_commit=False)
    # Advance to implementing
    task.pipeline_status = "implementing"  # type: ignore[assignment]
    save_task(tmp_path, task)

    # Write a prior implementing pass report to simulate work done in an earlier run
    reports_dir = task_dir(tmp_path, task) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    prior_report = {
        "task_id": task.id,
        "step": "implementing",
        "verdict": "pass",
        "summary": "Analysis complete, follow-up tasks created",
        "files_changed": [],
        "tests": {"added": 0, "passing": 0},
    }
    (reports_dir / "implementing-001.yaml").write_text(
        yaml.dump(prior_report), encoding="utf-8"
    )

    # Executor returns pass with no file changes and no tests (analysis task)
    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "implementing":
            return StageReport(
                task_id=task.id, step=step, verdict="pass",
                summary="Analysis already done", files_changed=[], tests={"added": 0, "passing": 0},
            )
        return StageReport(
            task_id=task.id, step=step, verdict="pass",
            summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1},
        )

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    # Should NOT get flagged — the prior pass means the guard is skipped
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status != "flagged", (
        f"Task was flagged despite prior implementing pass: {refreshed.status}"
    )
    # Should advance past implementing (to testing or beyond)
    assert refreshed.pipeline_status != "implementing"


def test_empty_swe_guard_skipped_when_prior_guard_rejection_exists(tmp_path: Path) -> None:
    """The guard overwrites pass→reject before saving, so a prior guard
    rejection (with the distinctive summary) should also bypass the guard
    on subsequent runs — otherwise the task loops forever."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Plan feature v2", auto_commit=False)
    task.pipeline_status = "implementing"  # type: ignore[assignment]
    save_task(tmp_path, task)

    # Write a prior guard rejection report (what the guard actually saves)
    reports_dir = task_dir(tmp_path, task) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    prior_report = {
        "task_id": task.id,
        "step": "implementing",
        "verdict": "reject",
        "summary": (
            "SWE reported pass but produced no file changes and no tests. "
            "This usually means the agent did not actually write code."
        ),
        "files_changed": [],
        "tests": {"added": 0, "passing": 0},
    }
    (reports_dir / "implementing-001.yaml").write_text(
        yaml.dump(prior_report), encoding="utf-8"
    )

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "implementing":
            return StageReport(
                task_id=task.id, step=step, verdict="pass",
                summary="Analysis already done", files_changed=[], tests={"added": 0, "passing": 0},
            )
        return StageReport(
            task_id=task.id, step=step, verdict="pass",
            summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1},
        )

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status != "flagged", (
        f"Task was flagged despite prior guard rejection: {refreshed.status}"
    )
    assert refreshed.pipeline_status != "implementing"


def test_empty_swe_guard_rejects_when_no_prior_pass(tmp_path: Path) -> None:
    """Without a prior implementing pass, an empty SWE pass should still be
    rejected by the guard (the agent likely didn't do any work)."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Real feature", auto_commit=False)
    task.pipeline_status = "implementing"  # type: ignore[assignment]
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "implementing":
            return StageReport(
                task_id=task.id, step=step, verdict="pass",
                summary="Done", files_changed=[], tests={"added": 0, "passing": 0},
            )
        return StageReport(
            task_id=task.id, step=step, verdict="pass",
            summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1},
        )

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=0)
    result = runner.run(task)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "flagged", (
        f"Expected flagged but got {refreshed.status} — guard should reject empty passes"
    )


def test_empty_swe_guard_allows_verified_preimplemented_cli_pass(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Existing feature", auto_commit=False)
    task.pipeline_status = "implementing"  # type: ignore[assignment]
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "implementing":
            return StageReport(
                task_id=task.id,
                step=step,
                verdict="pass",
                summary="Already implemented and verified with pytest; acceptance criteria confirmed.",
                feedback=(
                    "Already implemented before this run. Verified existing behavior with pytest "
                    "and confirmed the acceptance criteria still hold."
                ),
                submitted_via_cli=True,
                files_changed=[],
                tests={"added": 0, "passing": 0},
            )
        return StageReport(
            task_id=task.id, step=step, verdict="pass",
            summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1},
        )

    runner = TaskExecutionRunner(tmp_path, executor)
    runner.run(task)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status != "flagged"
    assert refreshed.pipeline_status != "implementing"


def test_empty_swe_guard_rejects_cli_pass_without_preimplemented_evidence(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Suspicious empty pass", auto_commit=False)
    task.pipeline_status = "implementing"  # type: ignore[assignment]
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "implementing":
            return StageReport(
                task_id=task.id,
                step=step,
                verdict="pass",
                summary="Verified.",
                feedback="Confirmed it looks good.",
                submitted_via_cli=True,
                files_changed=[],
                tests={"added": 0, "passing": 0},
            )
        return StageReport(
            task_id=task.id, step=step, verdict="pass",
            summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1},
        )

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=0)
    runner.run(task)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"


def test_runtime_routes_grooming_to_planner_and_accepting_to_reviewer() -> None:
    assert _role_for_step("grooming") == "planner"
    assert _role_for_step("implementing") == "swe"
    assert _role_for_step("testing") == "qa"
    assert _role_for_step("accepting") == "reviewer"


def test_runtime_routes_flagged_and_interrupted_retries_to_recovery(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Flagged task")
    flagged.status = "flagged"
    flagged.pipeline_status = "implementing"
    flagged.runtime.last_outcome.kind = "flagged"
    save_task(tmp_path, flagged)

    interrupted = create_task(tmp_path, title="Halted task")
    interrupted.status = "interrupted"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.last_outcome.kind = "interrupted"
    save_task(tmp_path, interrupted)

    assert _role_for_step("implementing", require_task(tmp_path, flagged.id)) == "recovery"
    assert _role_for_step("testing", require_task(tmp_path, interrupted.id)) == "recovery"
    assert _role_for_step("grooming", require_task(tmp_path, interrupted.id)) == "planner"


def test_subagent_manager_persists_planner_and_reviewer_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Role split task")

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            del model, max_turns
            if on_started is not None:
                on_started(4242)
            step = prompt.split("Stage: ", 1)[1].splitlines()[0]
            return _stage_subagent_result(cwd, step).execution  # type: ignore[return-value]

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents._manager._supports_live_execution", lambda engine: False)
    monkeypatch.setattr("litehive.subagents._manager.get_engine", lambda _: FakeEngine())
    manager = SubagentManager(tmp_path)

    planner_result = manager.run(
        task, role="planner", engine_name="codex", prompt="Stage: grooming"
    )
    task = require_task(tmp_path, task.id)
    reviewer_result = manager.run(
        task, role="reviewer", engine_name="codex", prompt="Stage: accepting"
    )
    task = require_task(tmp_path, task.id)

    assert planner_result.failure is None
    assert reviewer_result.failure is None
    assert [ref.role for ref in task.subagents] == ["planner", "reviewer"]
    assert task.subagents[0].path.endswith("-planner")
    assert task.subagents[-1].path.endswith("-reviewer")


def test_runner_requeues_task_after_testing_rejection(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=3))
    task = create_task(tmp_path, title="Review loop")

    def executor(task, step):  # type: ignore[no-untyped-def]
        verdict = "fail" if step == "testing" else "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=3)
    result = runner.run(task)

    assert result.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.runtime.retry_count == 1
    assert task.runtime.retry_limit == 3
    assert task.runtime.retry_source == "global"


def test_runner_requeues_commit_to_git_retry_request_back_to_implementing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Post-merge fixup",
        acceptance_criteria=["Keep merged main and fix failures from implementing."],
    )
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        assert step == "commit_to_git"
        task.status = "queued"
        task.pipeline_status = "implementing"
        return StageReport(
            task_id=task.id,
            step=step,
            verdict="blocked",
            summary="after_merge failed on merged main",
            retry_decision="retry",
            hook_results=[
                {
                    "point": "after_merge",
                    "command": "exit 7",
                    "blocking": True,
                    "exit_code": 7,
                    "status": "failed",
                    "artifact": "artifacts/after_merge-001.yaml",
                }
            ],
        )

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "queued"
    refreshed = require_task(tmp_path, task.id)
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert load_state(tmp_path).queue[0] == task.id

def test_runner_does_not_override_qa_verdict(tmp_path: Path) -> None:
    """QA verdict is final and the runner lets testing pass advance normally."""
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=3))
    task = create_task(
        tmp_path,
        title="Enforce workflow verification",
        goal="Prove control-plane lifecycle behavior through the real CLI",
        acceptance_criteria=[
            "commit_to_git succeeds and records the final checkpoint commit",
        ],
        auto_commit=False,
    )
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    def executor(current_task, step):  # type: ignore[no-untyped-def]
        return StageReport(
            task_id=current_task.id,
            step=step,
            verdict="pass",
            summary=f"{step} ok",
            feedback="Verified with pytest.",
        )

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=3)
    result = runner.run(task)

    assert result.final_status == "done"


def test_runner_accepts_workflow_testing_with_real_lifecycle_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Verify workflow lifecycle",
        goal="Prove control-plane lifecycle behavior through the real CLI",
        acceptance_criteria=[
            "commit_to_git succeeds and records the final checkpoint commit",
            "An interrupted task resumes from the correct stage in a real workspace run",
            "daemon or pool behavior is proven through the real CLI execution path",
        ],
        auto_commit=False,
    )

    commit_workspace = tmp_path / "proof-commit"
    commit_workspace.mkdir()
    _init_git_repo(commit_workspace)
    ensure_workspace(commit_workspace)
    create_task(commit_workspace, title="Ship example change")

    resume_workspace = tmp_path / "proof-resume"
    resume_workspace.mkdir()
    ensure_workspace(resume_workspace, LitehiveConfig(auto_commit=False))
    create_task(resume_workspace, title="Finish example change", auto_commit=False)

    resume_once = {"seen": False}

    def fake_run(
        self,
        current_task,
        role,
        engine_name,
        prompt,
        model=None,
        max_turns=None,
        resume_session_id=None,
    ):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "implementing" and (
            self.execution_root == commit_workspace
            or commit_workspace in self.execution_root.parents
        ):
            app_path = self.execution_root / "app.txt"
            if app_path.exists():
                app_path.write_text("proof commit lifecycle\n", encoding="utf-8")
        if current_task.pipeline_status == "testing" and (
            self.execution_root == resume_workspace
            or resume_workspace in self.execution_root.parents
        ):
            if not resume_once["seen"]:
                resume_once["seen"] = True
                raise KeyboardInterrupt()
        return _completed_subagent_result(
            self.execution_root, current_task.pipeline_status, engine_name=engine_name, task=current_task
        )

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    assert _cmd_run(argparse.Namespace(workspace=commit_workspace, dry_run=False, drain=False)) == 0
    commit_output = capsys.readouterr().out
    assert "status: done" in commit_output
    assert "commit:" in commit_output
    assert "commit_to_git=pass" in commit_output

    assert _cmd_run(argparse.Namespace(workspace=resume_workspace, dry_run=False, drain=False)) == 0
    interrupted_output = capsys.readouterr().out
    assert "status: interrupted" in interrupted_output

    assert (
        _cmd_resume_task(
            argparse.Namespace(workspace=resume_workspace, task_id="T-0001", front=True)
        )
        == 0
    )
    resume_output = capsys.readouterr().out
    assert "pipeline_status: testing" in resume_output

    assert _cmd_run(argparse.Namespace(workspace=resume_workspace, dry_run=False, drain=False)) == 0
    resumed_output = capsys.readouterr().out
    assert "status: done" in resumed_output

    wrapper_workspace = tmp_path / "proof-wrapper"
    wrapper_workspace.mkdir()
    (wrapper_workspace / ".litehive").mkdir()
    counts_dir = tmp_path / "proof-wrapper-counts"
    counts_dir.mkdir()
    status_count_file = counts_dir / "status-count"
    run_count_file = counts_dir / "run-count"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "python" && "${{3:-}}" == "-" ]]; then
  shift 2
  exec python3 "$@"
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  count="$(cat "{run_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{run_count_file}"
  if [[ "$count" -eq 1 ]]; then
    cat > "{wrapper_workspace / ".litehive" / "state.yaml"}" <<'STATE'
active_task_id: null
queue:
  - T-0001
pool_stop_reason: null
STATE
  else
    cat > "{wrapper_workspace / ".litehive" / "state.yaml"}" <<'STATE'
active_task_id: null
queue: []
pool_stop_reason: queue_exhausted
STATE
  fi
  echo "tasks_run: 1"
  echo "stop_reason: queue_exhausted"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "repair" ]]; then
  echo "repaired: no"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )
    (wrapper_workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\nqueue:\n  - T-0001\npool_stop_reason: null\n",
        encoding="utf-8",
    )
    daemon_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "litehive.main",
            "daemon",
            "run",
            "--foreground",
            "--workspace",
            str(wrapper_workspace),
        ],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=_with_fake_uv(fake_uv, xdg_config_home=tmp_path / "config-home"),
        check=False,
    )
    assert daemon_result.returncode == 0
    assert "== iteration 1 ==" in daemon_result.stdout
    assert "No active or queued tasks remain. Stopping." in daemon_result.stdout

    evidence = "\n\n".join(
        [
            "$ uv run litehive run --workspace .\n" + commit_output.strip(),
            "$ uv run litehive run --workspace .\n" + interrupted_output.strip(),
            "$ uv run litehive resume T-0001 --workspace .\n" + resume_output.strip(),
            "$ uv run litehive run --workspace .\n" + resumed_output.strip(),
            "$ litehive daemon run --foreground --workspace .\n" + daemon_result.stdout.strip(),
        ]
    )
    assert "commit_to_git=pass" in evidence
    assert "status: interrupted" in evidence
    assert "pipeline_status: testing" in evidence
    assert "No active or queued tasks remain. Stopping." in evidence


def test_runner_does_not_override_acceptance_verdict(tmp_path: Path) -> None:
    """Reviewer verdict is final and the runner lets acceptance pass advance."""
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=3))
    task = create_task(
        tmp_path,
        title="Accept workflow lifecycle evidence",
        goal="Only accept lifecycle claims that QA proved through the real CLI",
        acceptance_criteria=["commit_to_git succeeds"],
        auto_commit=False,
    )
    task.pipeline_status = "accepting"
    save_task(tmp_path, task)

    def executor(current_task, step):  # type: ignore[no-untyped-def]
        return StageReport(
            task_id=current_task.id,
            step=step,
            verdict="pass",
            summary=f"{step} ok",
            feedback="PM reviewed and approved.",
        )

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=3)
    result = runner.run(task)

    assert result.final_status == "done"


def test_unexpected_dirty_paths_computes_allowed_set_once(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Optimize commit dirty-path scan")

    dirty_entries = [
        f" M .litehive/tasks/{task.id}-{task.slug}/task.yaml",
        " M litehive/runtime.py",
        " M README.md",
        "?? docs/state-machine.md",
    ]

    allowed_paths = {
        PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}",
        PurePosixPath("litehive") / "runtime.py",
    }

    unexpected = _unexpected_dirty_paths(dirty_entries, allowed_paths)

    assert unexpected == ["README.md", "docs/state-machine.md"]


def test_unexpected_dirty_paths_ignores_unrelated_litehive_workspace_churn(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Ignore workspace churn during commit")

    allowed_paths = {
        PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}",
        PurePosixPath("litehive") / "runtime.py",
    }

    dirty_entries = [
        f" M .litehive/tasks/{task.id}-{task.slug}/task.yaml",
        " M .litehive/tasks/T-0099-something-else/task.yaml",
        "?? .litehive/tasks/T-0099-something-else/journal.md",
        " M README.md",
    ]

    unexpected = _unexpected_dirty_paths(dirty_entries, allowed_paths)

    assert unexpected == ["README.md"]


def test_unexpected_dirty_paths_ignores_stray_tmpdir_workspace_cleanup(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Ignore stray tmpdir cleanup")

    allowed_paths = {
        PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}",
        PurePosixPath("litehive") / "runtime.py",
    }

    dirty_entries = [
        ' D "\\"$tmpdir\\"/.litehive/config.yaml"',
        ' D "\\"$tmpdir\\"/.litehive/context.md"',
        ' D "\\"$tmpdir\\"/.litehive/state.yaml"',
        " M README.md",
    ]

    unexpected = _unexpected_dirty_paths(dirty_entries, allowed_paths)

    assert unexpected == ["README.md"]


def test_allowed_commit_paths_ignores_placeholder_file_entries(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Ignore placeholder changed files during commit")
    reports_dir = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    (reports_dir / "implementing-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "implementing",
                "verdict": "pass",
                "summary": "placeholder files changed",
                "files_changed": ["none", "litehive/runtime.py", " N/A ", "-"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    allowed_paths = _allowed_commit_paths(tmp_path, task)

    assert PurePosixPath("litehive/runtime.py") in allowed_paths
    assert PurePosixPath("none") not in allowed_paths
    assert PurePosixPath("N/A") not in allowed_paths
    assert PurePosixPath("-") not in allowed_paths


def test_live_session_progress_updates_runtime_heartbeat(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Live heartbeat update")
    task.pipeline_status = "testing"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-02T00:00:00+00:00",
        updated_at="2026-04-02T00:00:00+00:00",
    )
    ref = SubagentRef(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="running",
        path="subagents/SA-0001-qa",
    )
    task.subagents.append(ref)
    save_task(tmp_path, task)
    mark_subagent_started(tmp_path, task, ref)
    task_dir_path = task_dir(tmp_path, task) / "subagents" / "SA-0001-qa"
    task_dir_path.mkdir(parents=True, exist_ok=True)

    manager = SubagentManager(tmp_path)
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout="VERDICT: PASS\nSUMMARY: still running\n",
        stderr="",
        pid=12345,
    )
    manager._write_session_progress(task, task_dir_path, ref, "prompt", execution)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.updated_at != "2026-04-02T00:00:00+00:00"
    assert refreshed.runtime.current_stage.updated_at != "2026-04-02T00:00:00+00:00"
    assert refreshed.runtime.active_subagent is not None
    assert refreshed.runtime.active_subagent.updated_at != "2026-04-02T00:00:00+00:00"
    assert refreshed.runtime.active_subagent.pid == 12345
    assert refreshed.runtime.active_subagent.transcript_snippet


def test_commit_task_can_commit_only_selected_paths_with_other_unstaged_changes(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "litehive"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "litehive@example.com"], cwd=tmp_path, check=True
    )

    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("leave unstaged\n", encoding="utf-8")

    checkpoint = commit_task(tmp_path, "selected commit", paths=["tracked.txt"])

    assert checkpoint is not None
    status_lines = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "?? other.txt" in status_lines
    assert not any("tracked.txt" in line for line in status_lines)


def test_runner_preserves_retry_count_when_requeued_task_is_rejected_again(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=3))
    task = create_task(
        tmp_path, title="Review loop", acceptance_criteria=["Feature works correctly."]
    )
    task.status = "queued"
    task.pipeline_status = "accepting"
    task.runtime.retry_count = 1
    task.runtime.retry_limit = 3
    task.runtime.retry_source = "global"
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        verdict = "reject" if step == "accepting" else "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=3)
    result = runner.run(task)

    assert result.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.runtime.retry_count == 2
    assert task.runtime.retry_limit == 3
    assert task.runtime.retry_source == "global"

    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-review-loop"
            / "reports"
            / "accepting-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["retry_count"] == 2
    assert accepting_report["retry_limit"] == 3
    assert accepting_report["retry_decision"] == "retry"


def test_runner_requeues_implementing_rejection_without_sink_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Implementation rejection",
        acceptance_criteria=["Implement the requested change."],
    )
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        verdict = "reject" if step == "implementing" else "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=3)
    result = runner.run(task)

    assert result.final_status == "queued"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.last_outcome.kind is None
    assert refreshed.runtime.last_outcome.reason_code is None

    implementing_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-implementation-rejection"
            / "reports"
            / "implementing-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert implementing_report["verdict"] == "reject"
    assert implementing_report["retry_decision"] == "retry"


def test_runner_infers_acceptance_criteria_from_task_context_after_grooming(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="First prerequisite")
    task = create_task(
        tmp_path,
        title="Implement feature",
        goal="Ship deterministic dispatch",
        depends_on=[prerequisite.id],
        auto_commit=False,
    )

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    finish_task_run_transition(tmp_path, task, result.final_status)
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.runtime.execution_status == "done"
    assert updated.acceptance_criteria == [
        "The delivered change achieves the stated goal: Ship deterministic dispatch.",
        f"The result aligns with the prerequisite task context needed from: {prerequisite.id}.",
        "Focused verification demonstrates the targeted behavior works as intended.",
    ]


def test_runner_blocks_large_task_without_inferable_acceptance_criteria_during_grooming(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="First prerequisite")
    task = create_task(tmp_path, title="Implement feature", depends_on=[prerequisite.id])

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "flagged"
    finish_task_run_transition(tmp_path, task, result.final_status)
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    assert flagged.status == "flagged"
    assert flagged.flag_reason == "missing_acceptance_criteria"
    assert flagged.runtime.last_outcome.reason_code == "missing_acceptance_criteria"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0002-implement-feature"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert (
        "Structured acceptance criteria are required before implementation for larger tasks."
        in report["summary"]
    )


def test_runner_persists_grooming_generated_acceptance_criteria(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path, title="Implement feature", goal="Ship deterministic dispatch", auto_commit=False
    )

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "grooming":
            import json

            payload = {
                "verdict": "pass",
                "summary": "grooming complete",
                "acceptance_criteria": [
                    "The system auto-populates missing acceptance criteria from successful grooming output.",
                    "Tasks still block before implementation when grooming cannot define concrete criteria.",
                ],
            }
            return parse_stage_report_text(
                task_id=task.id,
                step="grooming",
                transcript=f"STAGE_RESULT:\n{json.dumps(payload)}\n",
                subagent_status="completed",
            )
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    finish_task_run_transition(tmp_path, task, result.final_status)
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.acceptance_criteria == [
        "The system auto-populates missing acceptance criteria from successful grooming output.",
        "Tasks still block before implementation when grooming cannot define concrete criteria.",
    ]


def test_runner_persists_grooming_generated_pm_sizing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path, title="Implement feature", goal="Ship deterministic dispatch", auto_commit=False
    )

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "grooming":
            import json

            payload = {
                "verdict": "pass",
                "summary": "grooming complete",
                "task_update": {
                    "pm_complexity": "complex",
                    "planned_effort": "l",
                },
            }
            return parse_stage_report_text(
                task_id=task.id,
                step="grooming",
                transcript=f"STAGE_RESULT:\n{json.dumps(payload)}\n",
                subagent_status="completed",
            )
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    finish_task_run_transition(tmp_path, task, result.final_status)
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.pm_complexity == "complex"
    assert updated.planned_effort == "l"


def test_large_task_acceptance_criteria_requirement_heuristic() -> None:
    minimal = TaskRecord(id="T-0001", slug="small-task", title="Small task")
    assert task_requires_acceptance_criteria(minimal) is False
    assert implementation_entry_stage(minimal) == "implementing"
    assert (
        reroute_stage_for_acceptance_criteria(
            minimal.model_copy(update={"pipeline_status": "testing"})
        )
        == "testing"
    )

    goal_only = TaskRecord(
        id="T-0002",
        slug="goal-task",
        title="Goal task",
        goal="Ship deterministic routing",
    )
    assert task_requires_acceptance_criteria(goal_only) is True
    assert implementation_entry_stage(goal_only) == "grooming"
    assert (
        reroute_stage_for_acceptance_criteria(
            goal_only.model_copy(update={"pipeline_status": "testing"})
        )
        == "grooming"
    )

    dependency_scoped = TaskRecord(
        id="T-0003",
        slug="dependency-task",
        title="Dependency task",
        depends_on=["T-0001"],
    )
    assert task_requires_acceptance_criteria(dependency_scoped) is True

    priority_scoped = TaskRecord(
        id="T-0004",
        slug="priority-task",
        title="Priority task",
        priority="high",
    )
    assert task_requires_acceptance_criteria(priority_scoped) is True

    planned = TaskRecord(
        id="T-0005",
        slug="planned-task",
        title="Planned task",
        plan=["Inspect current flow", "Implement gate"],
    )
    assert task_requires_acceptance_criteria(planned) is True

    explicitly_scoped = planned.model_copy(
        update={"acceptance_criteria": ["The result ships deterministic routing."]}
    )
    assert task_requires_acceptance_criteria(explicitly_scoped) is True
    assert implementation_entry_stage(explicitly_scoped) == "implementing"
    assert (
        reroute_stage_for_acceptance_criteria(
            explicitly_scoped.model_copy(update={"pipeline_status": "accepting"})
        )
        == "accepting"
    )


def test_runner_normalizes_implementing_stage_without_acceptance_criteria_to_grooming(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path, title="Resume feature", goal="Ship deterministic routing", auto_commit=False
    )
    task.plan = ["Inspect current flow", "Implement gate"]
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    stages_executed: list[str] = []

    def executor(task, step):  # type: ignore[no-untyped-def]
        stages_executed.append(step)
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    assert stages_executed[0] == "grooming", "task should be normalized to grooming first"
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    journal = (task_dir(tmp_path, updated) / "journal.md").read_text(encoding="utf-8")
    assert "Rerouted to grooming for normalization" in journal


def test_runner_normalizes_later_stage_without_acceptance_criteria_to_grooming(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path, title="Resume feature", goal="Ship deterministic routing", auto_commit=False
    )
    task.plan = ["Inspect current flow", "Implement gate"]
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    stages_executed: list[str] = []

    def executor(task, step):  # type: ignore[no-untyped-def]
        stages_executed.append(step)
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    assert stages_executed[0] == "grooming", "task should be normalized to grooming first"
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    journal = (task_dir(tmp_path, updated) / "journal.md").read_text(encoding="utf-8")
    assert "Rerouted to grooming for normalization" in journal


def test_runner_normalizes_underspecified_queued_task_through_grooming(
    tmp_path: Path,
) -> None:
    """Queued task at implementing with no acceptance criteria gets rerouted to grooming."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Legacy queued task", auto_commit=False)
    task.pipeline_status = "implementing"
    task.status = "queued"
    save_task(tmp_path, task)

    stages_executed: list[str] = []

    def executor(task, step):  # type: ignore[no-untyped-def]
        stages_executed.append(step)
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    assert stages_executed[0] == "grooming", "task should start from grooming after normalization"
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    journal = (task_dir(tmp_path, updated) / "journal.md").read_text(encoding="utf-8")
    assert "Rerouted to grooming for normalization" in journal
    assert "missing acceptance criteria" in journal


def test_runner_normalizes_interrupted_task_without_criteria_through_grooming(
    tmp_path: Path,
) -> None:
    """Interrupted task at testing with no acceptance criteria gets rerouted to grooming."""
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path, title="Interrupted legacy task", goal="Deliver the fix", auto_commit=False
    )
    task.pipeline_status = "testing"
    task.status = "interrupted"
    task.runtime.last_outcome.kind = "interrupted"
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    stages_executed: list[str] = []

    def executor(task, step):  # type: ignore[no-untyped-def]
        stages_executed.append(step)
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    assert stages_executed[0] == "grooming", "task should start from grooming after normalization"
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    journal = (task_dir(tmp_path, updated) / "journal.md").read_text(encoding="utf-8")
    assert "missing acceptance criteria" in journal


def test_runner_skips_normalization_for_well_specified_task(tmp_path: Path) -> None:
    """Task with goal and acceptance criteria continues from its current stage without grooming."""
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Well specified task",
        goal="Deliver the feature",
        acceptance_criteria=["Feature works end to end"],
        auto_commit=False,
    )
    task.pipeline_status = "implementing"
    task.status = "queued"
    save_task(tmp_path, task)

    stages_executed: list[str] = []

    def executor(task, step):  # type: ignore[no-untyped-def]
        stages_executed.append(step)
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    assert stages_executed[0] == "implementing", (
        "well-specified task should not be rerouted to grooming"
    )
    assert "grooming" not in stages_executed


def test_needs_normalization_returns_none_for_task_already_at_grooming() -> None:
    """Tasks already at grooming should not be flagged for normalization."""
    task = TaskRecord(id="T-0001", slug="test", title="Test task")
    task.pipeline_status = "grooming"
    task.goal = ""
    task.acceptance_criteria = []
    assert needs_normalization(task) is None


def test_needs_normalization_detects_missing_criteria() -> None:
    """Tasks past grooming without acceptance criteria are detected."""
    task = TaskRecord(id="T-0001", slug="test", title="Test task")
    task.pipeline_status = "implementing"
    task.goal = ""
    task.acceptance_criteria = []
    reason = needs_normalization(task)
    assert reason is not None
    assert "missing acceptance criteria" in reason
    assert "missing goal" in reason  # secondary signal when goal also empty

    task.goal = "Real goal"
    reason = needs_normalization(task)
    assert reason is not None
    assert "missing acceptance criteria" in reason
    assert "missing goal" not in reason

    task.acceptance_criteria = ["Criterion"]
    assert needs_normalization(task) is None

    # Task with criteria but no goal is NOT underspecified
    task.goal = ""
    task.acceptance_criteria = ["Criterion"]
    assert needs_normalization(task) is None


def test_runner_cancels_task_with_explicit_reason(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Cancelled run")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            raise KeyboardInterrupt()
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "interrupted"
    finish_task_run_transition(tmp_path, task, result.final_status)
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "interrupted"
    assert load_state(tmp_path).queue == []
    assert task.runtime.execution_status == "interrupted"
    assert task.runtime.last_outcome.kind == "interrupted"
    assert task.runtime.last_outcome.stage == "testing"
    assert task.runtime.last_outcome.reason_code == "execution_interrupted"
    assert task.runtime.last_outcome.reason == "Execution interrupted during testing"
    assert task.runtime.interruption is not None
    assert task.runtime.interruption.source == "runner"
    assert task.runtime.interruption.stage == "testing"
    assert task.runtime.interruption.resume_stage == "testing"
    assert task.runtime.interruption.reason == "Execution interrupted during testing"
    assert task.runtime.current_stage.step == "testing"
    assert task.runtime.current_stage.status == "interrupted"
    journal = (tmp_path / ".litehive" / "tasks" / "T-0001-cancelled-run" / "journal.md").read_text(
        encoding="utf-8"
    )
    assert "Interrupted runner execution while `testing` was running." in journal
    assert "Resume from `testing`." in journal
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-cancelled-run"
            / "reports"
            / "testing-003.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["retry_decision"] == "final"
    assert report["outcome"] == "interrupted"
    assert report["outcome_reason_code"] == "execution_interrupted"
    assert report["outcome_reason"] == "Execution interrupted during testing"


def test_runner_fails_task_when_stage_executor_crashes(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Executor crash")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            raise RuntimeError("boom")
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "queued"
    finish_task_run_transition(tmp_path, task, result.final_status)
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "queued"
    assert load_state(tmp_path).queue == [task.id]
    assert task.runtime.last_outcome.kind == "flagged"
    assert task.runtime.last_outcome.stage == "testing"
    assert task.runtime.last_outcome.reason_code == "stage_exception"
    assert task.runtime.last_outcome.reason == "testing failed with unhandled error: boom"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-executor-crash"
            / "reports"
            / "testing-003.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"] == ["boom"]
    assert report["retry_decision"] == "final"
    assert report["outcome"] == "flagged"
    assert report["outcome_reason_code"] == "stage_exception"
    assert report["outcome_reason"] == "testing failed with unhandled error: boom"


def test_run_next_task_uses_task_retry_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    task = create_task(tmp_path, title="Override retry limit", auto_commit=False)
    task.retry_policy.max_retries = 1
    save_task(tmp_path, task)
    attempts = {"testing": 0}

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        if task.pipeline_status == "testing":
            attempts["testing"] += 1
            if attempts["testing"] == 1:
                return _stage_subagent_result(
                    tmp_path,
                    "testing",
                    role=role,
                    engine_name=engine_name,
                    verdict="FAIL",
                    summary="tests failed once",
                    files_changed=[],
                    tests_added=0,
                    tests_passing=0,
                    task=task,
                )
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.runtime.retry_limit == 1
    assert task.runtime.retry_count == 1
    assert task.runtime.retry_source == "task"
    assert task.runtime.last_outcome.kind is None
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-override-retry-limit"
            / "reports"
            / "testing-003.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["retry_count"] == 1
    assert report["retry_limit"] == 1
    assert report["retry_source"] == "task"
    assert report["retry_decision"] == "retry"


def test_run_next_task_requeues_after_qa_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    task = create_task(tmp_path, title="Iterate until accepted", auto_commit=False)
    task.retry_policy.max_retries = 3
    save_task(tmp_path, task)

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        if task.pipeline_status == "testing":
            return _stage_subagent_result(
                tmp_path,
                "testing",
                role=role,
                engine_name=engine_name,
                verdict="FAIL",
                summary="testing needs another implementation pass",
                files_changed=[],
                tests_added=0,
                tests_passing=0,
                task=task,
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.task.id == task.id
    assert summary.result is not None
    assert summary.result.final_status == "queued"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.retry_count == 1
    assert refreshed.runtime.retry_limit == 3
    assert refreshed.runtime.retry_source == "task"
    assert refreshed.runtime.last_outcome.kind is None

    testing_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-iterate-until-accepted"
            / "reports"
            / "testing-003.yaml"
        ).read_text(encoding="utf-8")
    )
    assert testing_report["verdict"] == "fail"
    assert testing_report["retry_count"] == 1
    assert testing_report["retry_limit"] == 3
    assert testing_report["retry_source"] == "task"
    assert testing_report["retry_decision"] == "retry"


def test_cli_run_end_to_end_requeues_after_qa_failure_then_commits_in_temp_git_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    task = create_task(
        tmp_path, title="QA harness task", acceptance_criteria=["Feature works correctly."]
    )
    task.retry_policy.max_retries = 2
    save_task(tmp_path, task)

    attempts = {"testing": 0}

    def fake_run(
        self,
        current_task,
        role,
        engine_name,
        prompt,
        model=None,
        max_turns=None,
        resume_session_id=None,
    ):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "implementing":
            (self.execution_root / "app.txt").write_text(
                f"iteration {attempts['testing'] + 1}\n",
                encoding="utf-8",
            )
        if current_task.pipeline_status == "testing":
            attempts["testing"] += 1
            if attempts["testing"] == 1:
                return _stage_subagent_result(
                    self.execution_root,
                    "testing",
                    role=role,
                    engine_name=engine_name,
                    verdict="FAIL",
                    summary="testing needs another implementation pass",
                    files_changed=[],
                    tests_added=0,
                    tests_passing=0,
                    task=current_task,
                )
        return _stage_subagent_result(
            self.execution_root,
            current_task.pipeline_status,
            role=role,
            engine_name=engine_name,
            files_changed=["app.txt"],
            task=current_task,
        )

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    assert _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False)) == 0
    first_output = capsys.readouterr().out
    assert "status: queued" in first_output
    assert "last_verdict: fail" in first_output
    assert "stage_outcomes: grooming=pass, implementing=pass, testing=fail" in first_output
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == initial_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"

    requeued = get_task(tmp_path, task.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "implementing"
    assert requeued.runtime.retry_count == 1
    assert requeued.runtime.retry_limit == 2

    assert _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False)) == 0
    second_output = capsys.readouterr().out
    assert "status: done" in second_output
    assert "last_verdict: pass" in second_output
    assert "stage_outcomes:" in second_output
    assert "grooming=pass" in second_output
    assert "implementing=pass" in second_output
    assert "testing=pass" in second_output
    assert "accepting=pass" in second_output
    assert "commit_to_git=pass" in second_output
    assert "commit:" in second_output

    finished = get_task(tmp_path, task.id)
    assert finished is not None
    assert finished.status == "done"
    assert finished.pipeline_status == "done"
    assert finished.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert finished.git.commit_sha != initial_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "iteration 2\n"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 qa-harness-task"
    )

    reports = task_dir(tmp_path, finished) / "reports"
    assert (reports / "grooming-001.yaml").exists()
    implementing_reports = sorted(reports.glob("implementing-*.yaml"))
    testing_reports = sorted(reports.glob("testing-*.yaml"))
    accepting_reports = sorted(reports.glob("accepting-*.yaml"))
    commit_reports = sorted(reports.glob("commit_to_git-*.yaml"))
    assert len(implementing_reports) == 2
    assert len(testing_reports) == 2
    assert len(accepting_reports) == 1
    assert len(commit_reports) == 1

    parsed_testing_reports = [
        yaml.safe_load(path.read_text(encoding="utf-8")) for path in testing_reports
    ]
    commit_report = yaml.safe_load(commit_reports[0].read_text(encoding="utf-8"))
    assert {report["verdict"] for report in parsed_testing_reports} == {"fail", "pass"}
    failed_testing = next(
        report for report in parsed_testing_reports if report["verdict"] == "fail"
    )
    assert failed_testing["retry_decision"] == "retry"
    assert commit_report["verdict"] == "pass"


def test_opencode_strips_provider_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {}

    class FakePopen:
        def __init__(self, cmd, cwd, env, stdout, stderr, text, stdin=None):  # type: ignore[no-untyped-def]
            calls["cmd"] = cmd
            calls["cwd"] = cwd
            calls["env"] = env
            self.pid = 4242
            self.returncode = 0

        def communicate(self, input=None):  # type: ignore[no-untyped-def]
            return ("ok", "")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/fake")
    monkeypatch.setattr("subprocess.Popen", FakePopen)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("OPENCODE_API_KEY", "secret2")

    engine = get_engine("opencode")
    result = engine.run("hello", tmp_path)

    assert result.returncode == 0
    assert calls["cwd"] == str(tmp_path)
    assert list(calls["cmd"]) == [
        "opencode",
        "run",
        "--format",
        "json",
        "--dir",
        str(tmp_path),
        "hello",
    ]
    assert "OPENAI_API_KEY" not in calls["env"]
    assert "OPENCODE_API_KEY" not in calls["env"]


def test_sandbox_launcher_wraps_selected_engine_with_docker_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            image="ghcr.io/example/litehive-sandbox:latest",
            runtime_args=["--pull=never"],
            engine_policies={
                "codex": ExternalEngineSandboxPolicy(
                    enabled=True,
                    network_mode="none",
                    workspace_mode="rw",
                    environment=["OPENAI_API_KEY"],
                    credential_inputs=[
                        SandboxCredentialInput(
                            env_var="GOOGLE_APPLICATION_CREDENTIALS",
                            mount_path="/run/credentials/google.json",
                        )
                    ],
                )
            },
        )
    )
    launcher = SandboxLauncher(tmp_path, config)
    creds_path = tmp_path / "google.json"
    creds_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    invocation = get_engine("codex").build_invocation("ship it", tmp_path)
    invocation = invocation.__class__(
        argv=invocation.argv,
        cwd=invocation.cwd,
        env={
            "OPENAI_API_KEY": "secret",
            "GOOGLE_APPLICATION_CREDENTIALS": str(creds_path),
            "ANTHROPIC_API_KEY": "should-not-leak",
        },
    )

    wrapped = launcher.wrap_invocation("codex", "codex", invocation)
    joined = " ".join(wrapped.argv)

    assert wrapped.cwd == tmp_path
    assert wrapped.argv[:5] == ("docker", "run", "--rm", "--init", "--pull=never")
    assert "--network none" in joined
    assert "--read-only" in joined
    assert f"src={tmp_path},dst=/workspace" in joined
    assert "src=/usr/bin/codex,dst=/litehive/bin/codex,readonly" in joined
    assert "src=/usr/bin/opencode" not in joined
    assert "--env OPENAI_API_KEY=secret" in joined
    assert "--env ANTHROPIC_API_KEY=should-not-leak" not in joined
    assert f"src={creds_path},dst=/run/credentials/google.json,readonly" in joined
    assert "--env GOOGLE_APPLICATION_CREDENTIALS=/run/credentials/google.json" in joined
    assert (
        "/litehive/bin/codex exec --json --dangerously-bypass-approvals-and-sandbox --cd /workspace"
        in joined
    )


def test_sandbox_launcher_applies_resource_limit_flags_from_profile_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LitehiveConfig(process_profile="rust")
    launcher = SandboxLauncher(tmp_path, config)

    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    invocation = get_engine("codex").build_invocation("ship it", tmp_path)

    wrapped = launcher.wrap_invocation("codex", "codex", invocation)
    joined = " ".join(wrapped.argv)

    assert wrapped.argv[:4] == ("docker", "run", "--rm", "--init")
    assert "--memory 8192m" in joined
    assert "--cpus 4" in joined
    assert "--pids-limit 512" in joined
    assert f"src={tmp_path},dst=/workspace" in joined


def test_sandbox_launcher_classifies_cpu_limit_events() -> None:
    launcher = SandboxLauncher(Path("/tmp/workspace"), LitehiveConfig(process_profile="rust"))

    event = launcher.classify_resource_limit_event(
        "codex",
        exit_code=1,
        stdout="",
        stderr="fatal error: CPU time limit exceeded by cgroup cpu controller",
    )

    assert event is not None
    assert event.resource == "cpu"
    assert event.reason == "CPU limit exceeded"
    assert event.observed_signal == "cpu_limit"
    assert event.cpu_count == 4.0


def test_sandbox_launcher_wraps_selected_engine_with_bubblewrap_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            backend="bubblewrap",
            runtime_binary="bwrap",
            engine_policies={
                "codex": ExternalEngineSandboxPolicy(
                    enabled=True,
                    network_mode="none",
                    workspace_mode="rw",
                    environment=["OPENAI_API_KEY"],
                    credential_inputs=[
                        SandboxCredentialInput(
                            env_var="GOOGLE_APPLICATION_CREDENTIALS",
                            mount_path="/run/credentials/google.json",
                        )
                    ],
                )
            },
        )
    )
    launcher = SandboxLauncher(tmp_path, config)
    creds_path = tmp_path / "google.json"
    creds_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    invocation = get_engine("codex").build_invocation("ship it", tmp_path)
    invocation = invocation.__class__(
        argv=invocation.argv,
        cwd=invocation.cwd,
        env={
            "OPENAI_API_KEY": "secret",
            "GOOGLE_APPLICATION_CREDENTIALS": str(creds_path),
            "ANTHROPIC_API_KEY": "should-not-leak",
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/test",
        },
    )

    wrapped = launcher.wrap_invocation("codex", "codex", invocation)
    joined = " ".join(wrapped.argv)

    assert wrapped.cwd == tmp_path
    assert wrapped.argv[0] == "bwrap"
    assert "--unshare-all" in wrapped.argv
    assert "--die-with-parent" in wrapped.argv
    assert "--clearenv" in wrapped.argv
    assert "--share-net" not in wrapped.argv  # network_mode=none
    assert f"--bind {tmp_path} {tmp_path}" in joined  # rw workspace
    assert f"--ro-bind /usr/bin/codex /usr/bin/codex" in joined  # engine binary
    assert "--setenv OPENAI_API_KEY secret" in joined
    assert "ANTHROPIC_API_KEY" not in joined  # not in allowed env
    assert f"--ro-bind {creds_path} /run/credentials/google.json" in joined
    assert "--setenv GOOGLE_APPLICATION_CREDENTIALS /run/credentials/google.json" in joined
    assert "--setenv HOME /home/test" in joined
    assert "--setenv PATH /usr/bin:/bin" in joined
    assert "--" in wrapped.argv  # separator before command


def test_sandbox_bubblewrap_readonly_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            backend="bubblewrap",
            runtime_binary="bwrap",
            default_workspace_mode="ro",
            engine_policies={
                "codex": ExternalEngineSandboxPolicy(
                    enabled=True,
                    workspace_mode="ro",
                )
            },
        )
    )
    launcher = SandboxLauncher(tmp_path, config)
    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    invocation = get_engine("codex").build_invocation("ship it", tmp_path)

    wrapped = launcher.wrap_invocation("codex", "codex", invocation)
    joined = " ".join(wrapped.argv)

    assert f"--ro-bind {tmp_path} {tmp_path}" in joined
    assert f"--bind {tmp_path}" not in joined


def test_sandbox_bubblewrap_shares_net_when_not_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            backend="bubblewrap",
            runtime_binary="bwrap",
            engine_policies={
                "codex": ExternalEngineSandboxPolicy(
                    enabled=True,
                    network_mode="bridge",
                )
            },
        )
    )
    launcher = SandboxLauncher(tmp_path, config)
    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    invocation = get_engine("codex").build_invocation("ship it", tmp_path)

    wrapped = launcher.wrap_invocation("codex", "codex", invocation)

    assert "--share-net" in wrapped.argv


def test_sandbox_bubblewrap_policy_summary_includes_backend_and_mounts(
    tmp_path: Path,
) -> None:
    config = LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            backend="bubblewrap",
            runtime_binary="bwrap",
            engine_policies={
                "codex": ExternalEngineSandboxPolicy(
                    enabled=True,
                    network_mode="none",
                    workspace_mode="rw",
                    environment=["OPENAI_API_KEY"],
                )
            },
        )
    )
    launcher = SandboxLauncher(tmp_path, config)

    summary = launcher.policy_summary("codex")

    assert summary.enabled is True
    assert summary.backend == "bubblewrap"
    assert summary.runtime == "bwrap"
    assert summary.network_mode == "none"
    assert summary.workspace_mode == "rw"
    assert "OPENAI_API_KEY" in summary.environment
    assert len(summary.propagated_mounts) > 0
    assert "/usr" in summary.propagated_mounts

    as_dict = summary.as_dict()
    assert as_dict["backend"] == "bubblewrap"
    assert len(as_dict["propagated_mounts"]) > 0

    text = summary.summary
    assert text.startswith("sandbox[bwrap")
    assert "net=none" in text
    assert "workspace=rw" in text


def test_sandbox_config_backend_defaults_to_docker() -> None:
    config = ExternalEngineSandboxConfig()
    assert config.backend == "docker"


def test_sandbox_config_invalid_backend_raises() -> None:
    with pytest.raises(ValueError, match="external_engine_sandbox.backend must be one of"):
        LitehiveConfig(
            external_engine_sandbox=ExternalEngineSandboxConfig(
                enabled=True,
                backend="invalid",
            )
        )


def test_load_config_round_trips_bubblewrap_backend(tmp_path: Path) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            external_engine_sandbox=ExternalEngineSandboxConfig(
                enabled=True,
                backend="bubblewrap",
                runtime_binary="bwrap",
                engine_policies={
                    "codex": ExternalEngineSandboxPolicy(
                        enabled=True,
                        network_mode="none",
                        workspace_mode="rw",
                        environment=["OPENAI_API_KEY"],
                    )
                },
            )
        ),
    )

    config = load_config(tmp_path)

    assert config.external_engine_sandbox.backend == "bubblewrap"
    assert config.external_engine_sandbox.runtime_binary == "bwrap"
    assert config.external_engine_sandbox.enabled is True


def test_format_external_engine_sandbox_includes_backend() -> None:
    config = LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            backend="bubblewrap",
            runtime_binary="bwrap",
            engine_policies={
                "codex": ExternalEngineSandboxPolicy(
                    enabled=True,
                    network_mode="none",
                    workspace_mode="rw",
                )
            },
        )
    )

    rendered = format_external_engine_sandbox(config)

    assert "backend:bubblewrap" in rendered
    assert "runtime:bwrap" in rendered


def test_gemini_build_invocation_includes_model_and_jsonl_flags(tmp_path: Path) -> None:
    invocation = get_engine("gemini").build_invocation(
        "ship it",
        tmp_path,
        model="gemini-2.5-pro",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "gemini",
        "-p",
        "ship it",
        "--output-format",
        "stream-json",
        "--yolo",
        "-m",
        "gemini-2.5-pro",
    ]


def test_copilot_build_invocation_includes_model_and_jsonl_flags(tmp_path: Path) -> None:
    invocation = get_engine("copilot").build_invocation(
        "ship it",
        tmp_path,
        model="gpt-5",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "copilot",
        "-p",
        "ship it",
        "--output-format",
        "json",
        "--allow-all-tools",
        "--autopilot",
        "--no-auto-update",
        "--add-dir",
        str(tmp_path),
        "--model",
        "gpt-5",
    ]


def test_engine_capabilities_report_availability_and_contract_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")

    codex = get_engine("codex").detect_capabilities()
    opencode = get_engine("opencode").detect_capabilities()
    gemini = get_engine("gemini").detect_capabilities()
    copilot = get_engine("copilot").detect_capabilities()

    assert codex.available is True
    assert codex.supports_model_override is False
    assert codex.transcript_format == "jsonl"
    assert opencode.available is True
    assert opencode.supports_model_override is True
    assert opencode.strips_environment is True
    assert gemini.available is True
    assert gemini.supports_model_override is True
    assert gemini.transcript_format == "jsonl"
    assert copilot.available is True
    assert copilot.supports_model_override is True
    assert copilot.transcript_format == "jsonl"


def test_codex_build_invocation_includes_workspace_and_prompt(tmp_path: Path) -> None:
    invocation = get_engine("codex").build_invocation("ship it", tmp_path)

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "codex",
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(tmp_path),
        "--skip-git-repo-check",
        "ship it",
    ]


def test_codex_renders_jsonl_transcript_and_usage_observation(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec", "--json"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"019d5098-77ba-7dc1-8b89-d3bff176bdb1"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}',
                '{"type":"turn.completed","usage":{"input_tokens":15442,"cached_input_tokens":5504,"output_tokens":18}}',
            ]
        ),
        stderr="",
    )

    adapter = get_engine("codex")

    assert adapter.render_transcript(execution) == "OK"

    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "openai"
    assert observation.usage is not None
    assert observation.usage.used == 15460
    assert observation.usage.unit == "tokens"
    assert observation.metadata["input_tokens"] == 15442
    assert observation.metadata["cached_input_tokens"] == 5504
    assert observation.metadata["output_tokens"] == 18


def test_codex_renders_jsonl_error_payloads_and_extracts_limit_observation(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec", "--json"),
        cwd=tmp_path,
        exit_code=1,
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"019d5098-77ba-7dc1-8b89-d3bff176bdb1"}',
                '{"type":"turn.started"}',
                '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":429,\\"error\\":{\\"type\\":\\"rate_limit_error\\",\\"message\\":\\"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 5:26 PM.\\"}}"}',
                '{"type":"turn.failed","error":{"message":"{\\"type\\":\\"error\\",\\"status\\":429,\\"error\\":{\\"type\\":\\"rate_limit_error\\",\\"message\\":\\"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 5:26 PM.\\"}}"}}',
            ]
        ),
        stderr="",
    )

    adapter = get_engine("codex")
    transcript = adapter.render_transcript(execution)

    assert "usage limit" in transcript
    assert classify_execution_limit(transcript) == "usage limit reached"

    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "openai"
    assert observation.success is False
    assert observation.limit_reason == "usage limit reached"
    assert observation.usage is None
    assert observation.metadata["error_status"] == 429
    assert observation.metadata["error_type"] == "rate_limit_error"
    assert observation.metadata["retry_at_hint"] == "5:26 PM"
    assert observation.metadata["purchase_more_credits"] is True


def test_classify_codex_usage_limit_matches_exact_message() -> None:
    from litehive.agents.adapters.codex import _classify_codex_usage_limit

    result = _classify_codex_usage_limit(
        "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at 5:26 PM."
    )

    assert result is not None
    assert result.limit_reason == "usage limit reached"
    assert result.retry_at == "5:26 PM"
    assert result.purchase_more_credits is True


def test_classify_codex_usage_limit_extracts_date_with_timezone() -> None:
    from litehive.agents.adapters.codex import _classify_codex_usage_limit

    result = _classify_codex_usage_limit(
        "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at 2026-04-09 10:00 UTC."
    )

    assert result is not None
    assert result.retry_at == "2026-04-09 10:00 UTC"


def test_classify_codex_usage_limit_without_date() -> None:
    from litehive.agents.adapters.codex import _classify_codex_usage_limit

    result = _classify_codex_usage_limit(
        "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits."
    )

    assert result is not None
    assert result.limit_reason == "usage limit reached"
    assert result.retry_at is None
    assert result.purchase_more_credits is True


def test_classify_codex_usage_limit_returns_none_for_unrelated_error() -> None:
    from litehive.agents.adapters.codex import _classify_codex_usage_limit

    assert _classify_codex_usage_limit("Connection refused") is None
    assert _classify_codex_usage_limit("") is None
    assert _classify_codex_usage_limit(None) is None


def test_classify_codex_usage_limit_with_smart_apostrophe() -> None:
    from litehive.agents.adapters.codex import _classify_codex_usage_limit

    result = _classify_codex_usage_limit(
        "You\u2019ve hit your usage limit. Purchase more credits."
    )

    assert result is not None
    assert result.limit_reason == "usage limit reached"


def test_codex_extract_usage_observation_uses_specific_detector_over_generic(tmp_path: Path) -> None:
    """The codex-specific detector should fire and populate metadata from the exact message."""
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec", "--json"),
        cwd=tmp_path,
        exit_code=1,
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"test-thread"}',
                '{"type":"error","message":"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 5:26 PM."}',
            ]
        ),
        stderr="",
    )

    adapter = get_engine("codex")
    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.limit_reason == "usage limit reached"
    assert observation.metadata["retry_at_hint"] == "5:26 PM"
    assert observation.metadata["purchase_more_credits"] is True


def test_codex_extract_usage_observation_falls_back_to_generic_for_unknown_errors(tmp_path: Path) -> None:
    """Unknown limit patterns should still be caught by the generic classifier."""
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec", "--json"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"error","message":"quota exceeded for this account"}',
        stderr="",
    )

    adapter = get_engine("codex")
    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.limit_reason == "quota exceeded"


def test_codex_extract_usage_observation_stderr_uses_specific_detector(tmp_path: Path) -> None:
    """Codex-specific detector also works on stderr fallback path."""
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec", "--json"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"thread.started","thread_id":"test-thread"}',
        stderr="ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 3:00 AM.",
    )

    adapter = get_engine("codex")
    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.limit_reason == "usage limit reached"
    assert observation.metadata["retry_at_hint"] == "3:00 AM"
    assert observation.metadata["purchase_more_credits"] is True


def test_opencode_build_invocation_includes_dir_model_and_prompt(tmp_path: Path) -> None:
    invocation = get_engine("opencode").build_invocation(
        "ship it",
        tmp_path,
        model="zai-coding-plan/glm-5.1",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "opencode",
        "run",
        "--format",
        "json",
        "--dir",
        str(tmp_path),
        "--model",
        "zai-coding-plan/glm-5.1",
        "ship it",
    ]


def test_opencode_renders_json_transcript_and_usage_observation(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="opencode",
        argv=("opencode", "run", "--format", "json"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"step_start","timestamp":1,"sessionID":"ses_123","part":{"id":"prt_1","type":"step-start"}}',
                '{"type":"text","timestamp":2,"sessionID":"ses_123","part":{"id":"prt_2","type":"text","text":"OK"}}',
                '{"type":"step_finish","timestamp":3,"sessionID":"ses_123","part":{"id":"prt_3","type":"step-finish","reason":"stop","cost":0,"tokens":{"total":10971,"input":10509,"output":14,"reasoning":11,"cache":{"read":448,"write":0}}}}',
            ]
        ),
        stderr="",
    )

    adapter = get_engine("opencode")

    assert adapter.render_transcript(execution) == "OK"

    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "z.ai"
    assert observation.usage is not None
    assert observation.usage.used == 10971
    assert observation.usage.unit == "tokens"
    assert observation.metadata["input_tokens"] == 10509
    assert observation.metadata["output_tokens"] == 14
    assert observation.metadata["reasoning_tokens"] == 11
    assert observation.metadata["cache_read_tokens"] == 448
    assert observation.metadata["cache_write_tokens"] == 0
    assert observation.metadata["finish_reason"] == "stop"


def test_opencode_extract_usage_observation_reads_limit_error_payload(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="opencode",
        argv=("opencode", "run", "--format", "json"),
        cwd=tmp_path,
        exit_code=1,
        stdout=(
            '{"type":"error","timestamp":1,"sessionID":"ses_123",'
            '"error":{"name":"RateLimitError","data":{"message":"429 Too Many Requests: rate limit exceeded"}}}\n'
        ),
        stderr="",
    )

    adapter = get_engine("opencode")
    transcript = adapter.render_transcript(execution)

    assert "rate limit exceeded" in transcript
    assert classify_execution_limit(transcript) == "rate limit reached"

    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "z.ai"
    assert observation.success is False
    assert observation.limit_reason == "rate limit reached"
    assert observation.metadata["error_name"] == "RateLimitError"
    assert observation.metadata["error_message"] == "429 Too Many Requests: rate limit exceeded"


def test_gemini_renders_jsonl_transcript_and_stage_report(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="gemini",
        argv=("gemini", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"init","session_id":"abc","model":"gemini-2.5-pro"}',
                '{"type":"message","role":"assistant","content":"VERDICT: PASS\\n","delta":true}',
                '{"type":"message","role":"assistant","content":"SUMMARY: implemented Gemini adapter\\n","delta":true}',
                '{"type":"message","role":"assistant","content":"FILES_CHANGED:\\n- litehive/engines.py\\n","delta":true}',
                '{"type":"message","role":"assistant","content":"TESTS_ADDED: 4\\nTESTS_PASSING: 4\\nWARNINGS:\\n","delta":true}',
                '{"type":"result","status":"success"}',
            ]
        ),
        stderr="",
    )

    engine = get_engine("gemini")

    assert engine.render_transcript(execution).splitlines()[0] == "VERDICT: PASS"
    report = engine.parse_stage_report(
        task_id="T-0004",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )

    # Without CLI verdict, text VERDICT: PASS is not parsed — verdict is fail.
    assert report.verdict == "fail"
    assert any("litehive report" in w for w in report.warnings)


def test_codex_renders_jsonl_transcript_and_stage_report(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"thread_123"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"VERDICT: PASS\\nSUMMARY: implemented Codex event adapter\\nFILES_CHANGED:\\n- litehive/engines.py\\nTESTS_ADDED: 2\\nTESTS_PASSING: 2\\nWARNINGS:\\n"}}',
                '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
            ]
        ),
        stderr="",
    )

    engine = get_engine("codex")

    assert engine.render_transcript(execution).splitlines()[0] == "VERDICT: PASS"
    report = engine.parse_stage_report(
        task_id="T-0092",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )

    # Without CLI verdict, text VERDICT: PASS is not parsed — verdict is fail.
    assert report.verdict == "fail"
    assert any("litehive report" in w for w in report.warnings)


def test_codex_render_transcript_ignores_non_message_events_until_text_arrives(
    tmp_path: Path,
) -> None:
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"thread_123"}',
                '{"type":"turn.started"}',
                '{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"pwd","aggregated_output":"","exit_code":null,"status":"in_progress"}}',
            ]
        ),
        stderr="",
    )

    assert get_engine("codex").render_transcript(execution) == ""


def test_codex_render_transcript_replaces_updated_agent_message_text(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"thread_123"}',
                '{"type":"item.updated","item":{"id":"item_0","type":"agent_message","text":"VERDICT: PASS\\nSUMMARY: draft\\n"}}',
                '{"type":"item.updated","item":{"id":"item_0","type":"agent_message","text":"VERDICT: PASS\\nSUMMARY: final\\n"}}',
                '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"VERDICT: PASS\\nSUMMARY: final\\n"}}',
            ]
        ),
        stderr="",
    )

    assert get_engine("codex").render_transcript(execution) == "VERDICT: PASS\nSUMMARY: final"


def test_codex_stage_report_uses_failed_command_output_when_no_agent_message(
    tmp_path: Path,
) -> None:
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=1,
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"thread_123"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"pytest","aggregated_output":"tests failed","exit_code":1,"status":"failed"}}',
            ]
        ),
        stderr="",
    )

    report = get_engine("codex").parse_stage_report(
        task_id="T-0092",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "tests failed"
    # Without CLI verdict, failed agents produce fail (not blocked).
    assert report.verdict == "fail"


def test_codex_stage_report_ignores_stale_failed_command_output_after_restart(
    tmp_path: Path,
) -> None:
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"thread_123"}',
                '{"type":"item.updated","item":{"id":"item_1","type":"command_execution","command":"pytest","aggregated_output":"tests failed","exit_code":1,"status":"failed"}}',
                '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"pytest","aggregated_output":"all green","exit_code":0,"status":"completed"}}',
            ]
        ),
        stderr="",
    )

    report = get_engine("codex").parse_stage_report(
        task_id="T-0092",
        step="testing",
        execution=execution,
        subagent_status="completed",
    )

    # Without CLI verdict, verdict is fail regardless of exit code.
    assert report.verdict == "fail"


def test_gemini_stage_report_uses_tool_error_when_no_assistant_message(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="gemini",
        argv=("gemini", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"tool_result","status":"error","error":{"message":"permission denied"}}',
        stderr="",
    )

    report = get_engine("gemini").parse_stage_report(
        task_id="T-0004",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "permission denied"
    # Without CLI verdict, failed agents produce fail (not blocked).
    assert report.verdict == "fail"


def test_copilot_renders_jsonl_transcript_and_stage_report(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"assistant.turn_start","data":{"turnId":"0"}}',
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"VERDICT: PASS\\n"},"ephemeral":true}',
                '{"type":"assistant.message","data":{"messageId":"m1","content":"VERDICT: PASS\\nSUMMARY: implemented Copilot adapter\\nFILES_CHANGED:\\n- litehive/engines.py\\nTESTS_ADDED: 2\\nTESTS_PASSING: 2\\nWARNINGS:\\n"}}',
                '{"type":"result","exitCode":0}',
            ]
        ),
        stderr="",
    )

    engine = get_engine("copilot")

    assert engine.render_transcript(execution).splitlines()[0] == "VERDICT: PASS"
    report = engine.parse_stage_report(
        task_id="T-0005",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )

    # Without CLI verdict, text VERDICT: PASS is not parsed — verdict is fail.
    assert report.verdict == "fail"
    assert any("litehive report" in w for w in report.warnings)


def test_copilot_stage_report_uses_json_error_when_no_assistant_message(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"error","data":{"message":"authentication required"}}',
        stderr="",
    )

    report = get_engine("copilot").parse_stage_report(
        task_id="T-0005",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "authentication required"
    # Without CLI verdict, failed agents produce fail (not blocked).
    assert report.verdict == "fail"


def test_copilot_render_transcript_falls_back_to_message_deltas(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"assistant.turn_start","data":{"turnId":"0"}}',
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"VERDICT: PASS\\n"},"ephemeral":true}',
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"SUMMARY: streamed only\\n"}}',
                '{"type":"assistant.turn_end","data":{"turnId":"0"}}',
            ]
        ),
        stderr="",
    )

    transcript = get_engine("copilot").render_transcript(execution)

    assert transcript == "VERDICT: PASS\nSUMMARY: streamed only"


def test_copilot_stage_report_uses_failed_tool_result_when_no_message(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout=(
            '{"type":"tool.execution_complete","data":{"toolName":"write","success":false,'
            '"result":{"content":"disk full"}}}'
        ),
        stderr="",
    )

    report = get_engine("copilot").parse_stage_report(
        task_id="T-0005",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "disk full"
    assert report.verdict == "fail"


def test_copilot_stream_event_adapter_extracts_final_message(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"assistant.turn_start","data":{"turnId":"1"}}',
                '{"type":"assistant.message","data":{"messageId":"m1","content":"VERDICT: PASS\\nSUMMARY: all good\\n"}}',
                '{"type":"result","exitCode":0}',
            ]
        ),
        stderr="",
    )

    transcript = get_engine("copilot").render_transcript(execution)

    assert transcript.startswith("VERDICT: PASS")
    assert "SUMMARY: all good" in transcript


def test_copilot_stream_event_adapter_falls_back_to_deltas_when_no_final_message(
    tmp_path: Path,
) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"VERDICT: PASS\\n"}}',
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"SUMMARY: delta only\\n"}}',
            ]
        ),
        stderr="",
    )

    transcript = get_engine("copilot").render_transcript(execution)

    assert transcript == "VERDICT: PASS\nSUMMARY: delta only"


def test_copilot_stream_event_adapter_extracts_tool_error_without_message(
    tmp_path: Path,
) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"tool.execution_complete","data":{"toolName":"shell","success":false,"result":{"content":"permission denied"}}}',
        stderr="",
    )

    report = get_engine("copilot").parse_stage_report(
        task_id="T-0096",
        step="implementing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "permission denied"
    assert report.verdict == "fail"


def test_copilot_engine_continuation_returns_none(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"assistant.turn_start","data":{"turnId":"1"}}',
                '{"type":"assistant.message","data":{"messageId":"m1","content":"done"}}',
                '{"type":"result","exitCode":0}',
            ]
        ),
        stderr="",
    )

    continuation = extract_engine_continuation("copilot", execution)

    assert continuation is None


def test_execution_result_transcript_combines_stdout_and_stderr(tmp_path: Path) -> None:
    result = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=1,
        stdout="SUMMARY: failed\n",
        stderr="missing binary",
    )

    assert result.transcript == "SUMMARY: failed\n\n[stderr]\nmissing binary"


def test_parse_stage_report_text_fails_on_text_only_transcript() -> None:
    """Text VERDICT:/SUMMARY: without STAGE_RESULT JSON produces fail."""
    report = parse_stage_report_text(
        task_id="T-0003",
        step="implementing",
        transcript=(
            "VERDICT: PASS\n"
            "SUMMARY: adapter contract added\n"
            "FILES_CHANGED:\n"
            "- litehive/engines.py\n"
        ),
        subagent_status="completed",
    )

    # No CLI verdict and no valid STAGE_RESULT → fail
    assert report.verdict == "fail"
    assert any("litehive report" in w for w in report.warnings)


def test_parse_stage_report_text_no_text_follow_up_extraction() -> None:
    """Text-only transcript without structured STAGE_RESULT produces a fail verdict."""
    report = parse_stage_report_text(
        task_id="T-0003",
        step="accepting",
        transcript=(
            "VERDICT: PASS\n"
            "SUMMARY: acceptance complete\n"
            "FOLLOW_UP_TASKS:\n"
            '[{"title":"Fix","rationale":"Reason","blocking":false}]'
        ),
        subagent_status="completed",
    )

    assert report.verdict == "fail"


def test_stage_report_from_subagent_fails_without_cli_verdict(tmp_path: Path) -> None:
    """Without CLI verdict in thread, verdict is always fail."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Adapter task")
    result = SubagentResult(
        ref=SubagentRef(
            id="SA-0001",
            role="swe",
            engine="codex",
            status="completed",
            path="subagents/SA-0001-swe",
        ),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=tmp_path,
            exit_code=0,
            stdout="VERDICT: PASS\nSUMMARY: execution transcript parsed",
            stderr="",
        ),
        transcript="ignored fallback transcript",
        exit_code=0,
    )

    report = stage_report_from_subagent(task, "implementing", result)

    # No CLI verdict → fail, regardless of text VERDICT: PASS in transcript
    assert report.verdict == "fail"
    assert any("litehive report" in w for w in report.warnings)


def test_stage_prompt_includes_shared_process_and_profile_overlay(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Profiled task", goal="Ship deterministic routing")
    prompt = stage_prompt(
        task,
        "testing",
        workspace_context="## Project\n- Purpose: validate overlays",
        process_profile="codehive",
    )

    assert "Stage owner: qa" in prompt
    assert "Task: T-" in prompt
    assert "Stage: testing" in prompt
    assert "Role focus:" in prompt
    assert "litehive report" in prompt


def test_stage_prompt_surfaces_acceptance_gate_for_large_task_without_inferable_criteria(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="First prerequisite")
    task = create_task(tmp_path, title="Profiled task", depends_on=[prerequisite.id])

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "Stage owner: planner" in prompt
    assert (
        "You are the planner, a PM-style role representing the user's and product's point of view."
        in prompt
    )
    assert "Acceptance gate:" in prompt
    assert (
        "Structured acceptance criteria are required before implementation for larger tasks."
        in prompt
    )
    assert (
        "As the planner for grooming, provide an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets before passing grooming."
        in prompt
    )
    assert "ACCEPTANCE_CRITERIA:" in prompt
    assert "If the context is still insufficient, return `VERDICT: BLOCKED`" in prompt


def test_stage_prompt_allows_grooming_to_pass_with_inferred_acceptance_criteria(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Profiled task", goal="Ship deterministic routing")
    task.plan = ["Inspect current flow", "Implement gate"]

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "Stage owner: planner" in prompt
    assert "Acceptance gate:" in prompt
    assert (
        "As the planner for grooming, either provide explicit `ACCEPTANCE_CRITERIA:` bullets or let the runner persist the inferred version by returning `VERDICT: PASS`."
        in prompt
    )
    assert (
        "If the current task context is not sufficient after all, return `VERDICT: BLOCKED` instead of passing grooming without criteria."
        in prompt
    )
    assert "you may add an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets" in prompt
    assert "the current task context is sufficient to infer them" in prompt
    assert "You may return `VERDICT: PASS` without restating them" in prompt
    assert (
        "Return `VERDICT: BLOCKED` only if the inferred criteria are incomplete or incorrect"
        in prompt
    )
    assert (
        "Structured acceptance criteria are required before implementation for larger tasks."
        not in prompt
    )


def test_stage_prompt_shows_inferred_acceptance_criteria_when_context_is_sufficient(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Profiled task", goal="Ship deterministic routing")
    task.plan = ["Inspect current flow", "Implement gate"]

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "Inferred acceptance criteria available from current task context:" in prompt
    assert "The delivered change achieves the stated goal: Ship deterministic routing." in prompt
    assert "Focused verification demonstrates the targeted behavior works as intended." in prompt


def test_stage_prompt_distinguishes_accepting_reviewer_role(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Review final outcome",
        acceptance_criteria=["The user-visible outcome works end to end."],
    )

    prompt = stage_prompt(task, "accepting", workspace_context="")

    assert "Stage owner: reviewer" in prompt
    assert (
        "You are the reviewer, a PM-style role representing the user's and product's point of view."
        in prompt
    )
    assert (
        "Validate the strict end-user outcome, look for regressions or missing evidence, and make a final done versus not-done judgment."
        in prompt
    )
    assert "accept the task to normal `done`" in prompt
    assert "Use `wont_do`, `duplicate`, or `deferred` only" in prompt


def test_stage_prompt_guides_swe_for_preimplemented_or_obsolete_work(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Existing workflow behavior",
        acceptance_criteria=["Existing behavior is verified and reported explicitly."],
    )

    prompt = stage_prompt(task, "implementing", workspace_context="")

    assert "If the requested behavior is already implemented" in prompt
    assert "submit `litehive report --verdict pass` with explicit evidence" in prompt
    assert "Never exit the stage without calling `litehive report`." in prompt
    assert "use `litehive update` to narrow scope or adjust the acceptance criteria" in prompt
    assert "use `litehive close --outcome wont_do` or `litehive close --outcome duplicate`" in prompt


def test_stage_prompt_lists_upcoming_runner_hooks_for_swe(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Hook-aware implementation",
        acceptance_criteria=["Lint and acceptance hooks pass."],
    )
    config = LitehiveConfig(
        runner_hooks={
            "before_pm_acceptance": [
                {
                    "command": "ruff check --select E402,F401",
                    "blocking": True,
                    "description": "ensures no unused imports or wrong import order",
                }
            ],
            "after_swe_implementation": [
                {
                    "command": "pytest -q tests/test_runner_workflow.py",
                    "blocking": True,
                    "description": "checks the workflow slice stays green",
                }
            ],
        }
    )

    prompt = stage_prompt(task, "implementing", workspace_context="", config=config)

    assert "Runner hooks:" in prompt
    assert (
        "After implementing, these checks will run: pytest -q tests/test_runner_workflow.py (checks the workflow slice stays green)"
        in prompt
    )
    assert (
        "Before acceptance, these checks will run: ruff check --select E402,F401 (ensures no unused imports or wrong import order)"
        in prompt
    )


def test_stage_report_from_subagent_marks_cli_verdict_source(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="CLI verdict source")
    _write_cli_verdict(
        tmp_path,
        task,
        "implementing",
        verdict="pass",
        message="Already implemented and verified with pytest.",
    )
    result = SubagentResult(
        ref=SubagentRef(
            id="SA-implementing",
            role="swe",
            engine="codex",
            status="completed",
            path="subagents/SA-implementing",
        ),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=tmp_path,
            exit_code=0,
            stdout="",
            stderr="",
        ),
        transcript="ignored",
        exit_code=0,
    )

    report = stage_report_from_subagent(task, "implementing", result, root=tmp_path)

    assert report.verdict == "pass"
    assert report.source == "agent"
    assert report.submitted_via_cli is True
    assert report.feedback == "Already implemented and verified with pytest."


def test_stage_prompt_uses_recovery_role_when_requested(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover final stage")
    task.status = "flagged"
    task.pipeline_status = "implementing"
    task.runtime.last_outcome.kind = "flagged"
    task.runtime.last_outcome.reason_code = "stage_exception"
    task.runtime.last_outcome.reason = "implementing failed with unhandled error: boom"
    save_task(tmp_path, task)

    prompt = stage_prompt(task, "implementing", workspace_context="", role_name="recovery")

    assert "Stage owner: recovery" in prompt
    assert (
        "You are the recovery agent responsible for diagnosing why this task stopped making progress and restoring a runnable path."
        in prompt
    )
    assert (
        "Make the smallest effective fix needed so the task can resume the current stage and finish cleanly."
        in prompt
    )
    assert "switch into the repo at `litehive_source_path` and repair Litehive there" in prompt
    assert "run `uv run pytest` in the Litehive repo before reporting success" in prompt


def test_stage_prompt_includes_project_startup_guidance_for_role(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery-heavy task")
    config = LitehiveConfig(
        agent_startup_guidance={
            "all": ["Start from the latest task-local artifacts before broad repo reads."],
            "qa": ["Check the latest implementing report and wrapper logs before rerunning tests."],
        }
    )

    prompt = stage_prompt(task, "testing", workspace_context="", config=config)

    assert "Project startup guidance:" in prompt
    assert "Start from the latest task-local artifacts before broad repo reads." in prompt
    assert "Check the latest implementing report and wrapper logs before rerunning tests." in prompt


def test_stage_prompt_includes_task_type_and_plan(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Review adapter update", task_type="review", mode="tasks")

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "Task type: review" in prompt
    assert "Plan:" in prompt
    assert "Inspect the relevant change or workflow surface." in prompt
    assert "Task template:" in prompt
    assert (
        "Prioritize correctness, regressions, and missing verification over style observations."
        in prompt
    )
    assert "Template sections to fill or verify:" in prompt
    assert "Findings: record actionable issues with severity and supporting evidence." in prompt


def test_stage_prompt_includes_pm_sizing_guidance_for_grooming(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path, title="Estimate task", pm_complexity="moderate", planned_effort="m"
    )

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "PM sizing:" in prompt
    assert "Current PM complexity: moderate" in prompt
    assert "Current planned effort: m" in prompt
    assert "Use `PM_COMPLEXITY: simple|moderate|complex`." in prompt
    assert "Use `PLANNED_EFFORT: xs|s|m|l|xl`." in prompt


def test_load_config_normalizes_agent_startup_guidance_keys(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_startup_guidance": {
                    "QA": [
                        "Check the latest report first.",
                        "  ",
                    ],
                    "all": ["Use task-local artifacts before broad repo reads."],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.agent_startup_guidance == {
        "qa": ["Check the latest report first."],
        "all": ["Use task-local artifacts before broad repo reads."],
    }


def test_agent_md_overrides_config_startup_guidance(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="MD override test")
    config = LitehiveConfig(agent_startup_guidance={"swe": ["Config SWE guidance."]})
    agents_dir = tmp_path / ".litehive" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "swe.md").write_text("MD SWE guidance line one.\nMD SWE guidance line two.")

    prompt = stage_prompt(task, "implementing", workspace_context="", config=config, root=tmp_path)

    assert "MD SWE guidance line one." in prompt
    assert "MD SWE guidance line two." in prompt
    assert "Config SWE guidance." not in prompt


def test_agent_md_absent_falls_back_to_config(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback test")
    config = LitehiveConfig(agent_startup_guidance={"swe": ["Config SWE fallback."]})

    prompt = stage_prompt(task, "implementing", workspace_context="", config=config, root=tmp_path)

    assert "Config SWE fallback." in prompt


def test_recovery_prompt_includes_default_startup_guidance(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery guidance test")

    prompt = stage_prompt(task, "implementing", workspace_context="", config=LitehiveConfig(), root=tmp_path, role_name="recovery")

    assert "fix Litehive infrastructure bugs" in prompt
    assert "Do not redo the failed stage's work" in prompt
    assert "stdout, stderr, transcript, session metadata, exit code" in prompt


def test_agent_md_all_and_role_combine(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Combine test")
    agents_dir = tmp_path / ".litehive" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "all.md").write_text("Global guidance from md.")
    (agents_dir / "qa.md").write_text("QA guidance from md.")

    prompt = stage_prompt(task, "testing", workspace_context="", root=tmp_path)

    assert "Global guidance from md." in prompt
    assert "QA guidance from md." in prompt


def test_agent_md_empty_file_produces_no_guidance(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Empty md test")
    agents_dir = tmp_path / ".litehive" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "swe.md").write_text("   \n  \n")

    prompt = stage_prompt(task, "implementing", workspace_context="", root=tmp_path)

    assert "Project startup guidance:" not in prompt


def test_stage_prompt_requires_real_lifecycle_verification_for_workflow_testing_tasks(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Enforce workflow verification",
        goal="Prove workflow/control-plane behavior through the real CLI lifecycle",
        acceptance_criteria=[
            "commit_to_git succeeds and records the final checkpoint commit",
            "An interrupted task resumes from the correct stage in a real workspace run",
            "daemon or pool behavior is proven through the real CLI execution path",
        ],
    )

    prompt = stage_prompt(task, "testing", workspace_context="")

    # Workflow verification overlay was removed — QA decides on its own
    assert "This task touches workflow or control-plane behavior" not in prompt


def test_stage_prompt_no_longer_injects_lifecycle_verification_overlay(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Enforce control plane completion reliability",
        goal="Prove workflow/control plane behavior through the real CLI lifecycle",
        acceptance_criteria=[
            "Completion reliability is only proven after commit to git records the final checkpoint commit",
        ],
    )

    prompt = stage_prompt(task, "testing", workspace_context="")

    # Workflow verification overlay was removed — QA decides on its own
    assert "This task touches workflow or control-plane behavior" not in prompt


def test_update_command_seeds_task_brief_when_switching_to_tasks_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Review queue behavior")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type="review",
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode="tasks",
            auto_commit=None,
        )
    )
    capsys.readouterr()

    assert exit_code == 0
    brief = (task_dir(tmp_path, task) / "brief.md").read_text(encoding="utf-8")
    assert "# T-0001 Review queue behavior" in brief
    assert "- Task type: review" in brief
    assert "## Template Guidance" in brief
    assert "## Intake Notes" in brief
    assert "### Findings" in brief
    assert "_TBD_" in brief


# ── Per-stage retry escalation tests ─────────────────────────────────────────


def test_runner_normal_retry_within_stage_limit(tmp_path: Path) -> None:
    """Testing rejects once (within stage limit=2), task requeues at implementing — not escalated."""
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=3, default_stage_retry_limit=2))
    task = create_task(
        tmp_path,
        title="Normal retry",
        acceptance_criteria=["Feature works."],
        auto_commit=False,
    )
    call_count: dict[str, int] = {}

    def executor(task, step):  # type: ignore[no-untyped-def]
        call_count[step] = call_count.get(step, 0) + 1
        if step == "testing" and call_count.get("testing", 0) == 1:
            return StageReport(task_id=task.id, step=step, verdict="fail", summary="test failed")
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=3, stage_retry_limit=2)

    # First run: grooming+implementing pass, testing fails → requeue at implementing
    result1 = runner.run(task)
    assert result1.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.pipeline_status == "implementing"
    assert task.runtime.stage_retry_counts.get("testing", 0) == 1

    # Second run: implementing pass, testing passes now → completes
    result2 = runner.run(task)
    assert result2.final_status == "done"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.stage_retry_counts.get("testing", 0) == 1


def test_runner_escalates_to_grooming_after_testing_stage_limit_exhausted(tmp_path: Path) -> None:
    """After testing rejects 3 times (stage limit=2), task routes to grooming for recovery."""
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=10, default_stage_retry_limit=2))
    task = create_task(tmp_path, title="Testing churn", acceptance_criteria=["Feature works."])
    testing_calls: list[int] = [0]

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            testing_calls[0] += 1
        if step == "testing":
            return StageReport(task_id=task.id, step=step, verdict="fail", summary="tests fail")
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    # Run 3 times to accumulate 3 testing rejections:
    # Run 1: implementing pass → testing fail (stage_count=1) → requeue at implementing
    runner = TaskExecutionRunner(tmp_path, executor, max_retries=10, stage_retry_limit=2)
    result1 = runner.run(task)
    assert result1.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.pipeline_status == "implementing"

    # Run 2: implementing pass → testing fail (stage_count=2) → requeue at implementing
    result2 = runner.run(task)
    assert result2.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.pipeline_status == "implementing"

    # Run 3: implementing pass → testing fail (stage_count=3 > limit=2) → escalate to grooming
    result3 = runner.run(task)
    assert result3.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "grooming"
    assert task.runtime.stage_retry_counts.get("testing", 0) == 3
    assert task.runtime.continuation_handoff is not None
    assert "testing" in task.runtime.continuation_handoff.reason
    assert "recovery escalation" in task.runtime.continuation_handoff.reason
    assert task.runtime.continuation_handoff.kind == "restart"

    # Verify report recorded the escalation reason code
    # Escalation report is the last one written by run 3 (ordinal may collide with run 2's ordinal)
    reports_dir = task_dir(tmp_path, task) / "reports"
    testing_reports = sorted(reports_dir.glob("testing-*.yaml"))
    all_testing_outcomes = [
        yaml.safe_load(r.read_text(encoding="utf-8")).get("outcome_reason_code")
        for r in testing_reports
    ]
    assert "stage_retry_limit_exhausted" in all_testing_outcomes
    escalation_report = next(
        yaml.safe_load(r.read_text(encoding="utf-8"))
        for r in testing_reports
        if yaml.safe_load(r.read_text(encoding="utf-8")).get("outcome_reason_code")
        == "stage_retry_limit_exhausted"
    )
    assert "recovery escalation" in escalation_report["outcome_reason"]
    assert escalation_report["retry_decision"] == "retry"


def test_runner_escalates_to_grooming_after_accepting_stage_limit_exhausted(tmp_path: Path) -> None:
    """After accepting rejects 3 times (stage limit=2), task routes to grooming for planner."""
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=10, default_stage_retry_limit=2))
    task = create_task(tmp_path, title="Accepting churn", acceptance_criteria=["Feature works."])

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "accepting":
            return StageReport(task_id=task.id, step=step, verdict="reject", summary="rejected")
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=10, stage_retry_limit=2)

    # Run 3 times to exhaust per-stage accepting limit
    for i in range(2):
        result = runner.run(task)
        assert result.final_status == "queued"
        task = get_task(tmp_path, task.id)
        assert task is not None
        assert task.pipeline_status == "implementing"

    # Third run: stage_count=3 > limit=2 → escalate to grooming
    result = runner.run(task)
    assert result.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "grooming"
    assert task.runtime.stage_retry_counts.get("accepting", 0) == 3
    assert task.runtime.continuation_handoff is not None
    assert "accepting" in task.runtime.continuation_handoff.reason
    assert "planner escalation" in task.runtime.continuation_handoff.reason

    # Stage counts should be preserved
    reports_dir = task_dir(tmp_path, task) / "reports"
    accepting_reports = sorted(reports_dir.glob("accepting-*.yaml"))
    all_outcomes = [
        yaml.safe_load(r.read_text(encoding="utf-8")).get("outcome_reason_code")
        for r in accepting_reports
    ]
    assert "stage_retry_limit_exhausted" in all_outcomes
    escalation_report = next(
        yaml.safe_load(r.read_text(encoding="utf-8"))
        for r in accepting_reports
        if yaml.safe_load(r.read_text(encoding="utf-8")).get("outcome_reason_code")
        == "stage_retry_limit_exhausted"
    )
    assert "planner escalation" in escalation_report["outcome_reason"]


def test_runner_task_level_stage_retry_limit_overrides_global(tmp_path: Path) -> None:
    """Task-level stage_retry_limit overrides the workspace default."""
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=10, default_stage_retry_limit=5))
    task = create_task(tmp_path, title="Low stage limit", acceptance_criteria=["Feature works."])
    task.retry_policy = task.retry_policy.model_copy(update={"stage_retry_limit": 1})
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            return StageReport(task_id=task.id, step=step, verdict="fail", summary="fail")
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=10, stage_retry_limit=5)

    # Run 1: testing rejects (stage_count=1, limit=1) → normal retry
    result1 = runner.run(task)
    assert result1.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.pipeline_status == "implementing"

    # Run 2: testing rejects (stage_count=2 > limit=1) → escalate to grooming
    result2 = runner.run(task)
    assert result2.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.pipeline_status == "grooming"
    assert task.runtime.stage_retry_counts.get("testing", 0) == 2


def test_runner_stage_counts_persist_across_requeued_runs(tmp_path: Path) -> None:
    """stage_retry_counts survive serialization and are loaded on the next run."""
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=10, default_stage_retry_limit=3))
    task = create_task(tmp_path, title="Count persistence", acceptance_criteria=["Feature works."])
    runner = TaskExecutionRunner(
        tmp_path,
        lambda t, s: StageReport(
            task_id=t.id, step=s, verdict="fail" if s == "testing" else "pass", summary=f"{s} done",
            files_changed=["app.txt"], tests={"added": 1, "passing": 1},
        ),
        max_retries=10,
        stage_retry_limit=3,
    )

    runner.run(task)
    # Re-load task from disk (simulating a fresh pool iteration)
    reloaded = get_task(tmp_path, task.id)
    assert reloaded is not None
    assert reloaded.runtime.stage_retry_counts.get("testing", 0) == 1


# ── Single pipeline mode tests ─────────────────────────────────────────────────


def test_single_mode_task_with_code_changes_goes_through_commit(tmp_path: Path) -> None:
    """Single-mode tasks skip grooming/testing/accepting; if files changed, commit_to_git runs."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Quick fix", pipeline_mode="single", auto_commit=False)
    assert task.pipeline_mode == "single"

    stages_executed: list[str] = []

    def executor(t, step):  # type: ignore[no-untyped-def]
        stages_executed.append(step)
        files = ["litehive/tasks.py"] if step == "implementing" else []
        return StageReport(task_id=t.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=files)

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    assert stages_executed == ["implementing", "commit_to_git"]
    reports = tmp_path / ".litehive" / "tasks" / "T-0001-quick-fix" / "reports"
    assert (reports / "implementing-001.yaml").exists()
    assert (reports / "commit_to_git-002.yaml").exists()
    assert not (reports / "grooming-001.yaml").exists()
    assert not (reports / "testing-003.yaml").exists()
    assert not (reports / "accepting-004.yaml").exists()


def test_single_mode_task_without_code_changes_goes_directly_to_done(tmp_path: Path) -> None:
    """Single-mode tasks with no files_changed skip commit_to_git and go directly to done."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Research task", pipeline_mode="single", auto_commit=False)

    stages_executed: list[str] = []

    def executor(t, step):  # type: ignore[no-untyped-def]
        stages_executed.append(step)
        return StageReport(task_id=t.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=[])

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    assert stages_executed == ["implementing"]
    task = require_task(tmp_path, task.id)
    assert task.status == "done"
    assert task.pipeline_status == "done"


def test_single_mode_task_fail_routes_to_flagged(tmp_path: Path) -> None:
    """Single-mode tasks that fail go to flagged, same as full mode."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Failing task", pipeline_mode="single", auto_commit=False)

    def executor(t, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=t.id, step=step, verdict="fail", summary="failed")

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=0)
    result = runner.run(task)

    assert result.final_status == "flagged"
    task = require_task(tmp_path, task.id)
    assert task.status == "flagged"


def test_single_mode_task_skips_normalization(tmp_path: Path) -> None:
    """Single-mode tasks are not rerouted to grooming even without acceptance criteria."""
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Simple task",
        pipeline_mode="single",
        goal="Do something",
        auto_commit=False,
    )
    # Simulate a previous flagged state that would normally trigger normalization
    task.pipeline_status = "implementing"  # type: ignore[assignment]
    task.status = "flagged"
    save_task(tmp_path, task)
    task = require_task(tmp_path, task.id)

    stages_executed: list[str] = []

    def executor(t, step):  # type: ignore[no-untyped-def]
        stages_executed.append(step)
        return StageReport(task_id=t.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    assert "grooming" not in stages_executed
    assert "implementing" in stages_executed


def test_create_task_pipeline_mode_field_persists(tmp_path: Path) -> None:
    """pipeline_mode is persisted and loaded correctly from task.yaml."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Single task", pipeline_mode="single")
    loaded = require_task(tmp_path, task.id)
    assert loaded.pipeline_mode == "single"

    task2 = create_task(tmp_path, title="Full task")
    loaded2 = require_task(tmp_path, task2.id)
    assert loaded2.pipeline_mode == "full"


def test_full_mode_is_default_pipeline_mode(tmp_path: Path) -> None:
    """Tasks created without pipeline_mode default to full mode."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Default task", auto_commit=False)
    assert task.pipeline_mode == "full"

    stages_executed: list[str] = []

    def executor(t, step):  # type: ignore[no-untyped-def]
        stages_executed.append(step)
        return StageReport(task_id=t.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    assert "grooming" in stages_executed
    assert "testing" in stages_executed
    assert "accepting" in stages_executed
