import argparse
import litehive.tasks as tasks_module
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from litehive.cli import (
    _cmd_add,
    _cmd_abandon_task,
    _cmd_close_task,
    _cmd_move,
    _cmd_promote,
    _cmd_queue,
    _cmd_recover,
    _cmd_requeue_task,
    _cmd_resume_task,
    _cmd_rollback,
    _cmd_run,
    _cmd_status,
    _cmd_update,
    build_parser,
)
from litehive.engines import classify_execution_limit, get_engine
from litehive.config import LitehiveConfig, ensure_workspace, load_config, resolve_process_profile
from litehive.external_cli import AdapterCapabilities, CLIExecutionResult, parse_stage_report_text
from litehive.git_ops import GitError
from litehive.models import RuntimeStageState, RuntimeSubagentState, StageReport, SubagentRef
from litehive.runtime import (
    EngineBudgetLedger,
    TaskPoolStopConditions,
    rollback_completed_task,
    resolve_engine_plan,
    recover_completed_task,
    resolve_engine_name,
    resolve_next_task,
    run_next_task,
    run_task,
    run_task_pool,
)
from litehive.runner import TaskExecutionRunner
from litehive.subagents import (
    EngineFailure,
    SubagentManager,
    SubagentResult,
    stage_prompt,
    stage_report_from_subagent,
)
from litehive.tasks import (
    WorkspaceConflictError,
    close_task,
    create_task,
    dequeue_next_task_selection,
    finish_task_run_transition,
    get_task,
    list_tasks,
    load_state,
    move_queued_task,
    peek_next_task_selection,
    requeue_task,
    require_task,
    save_state,
    save_task,
    save_task_runtime,
    set_active_task,
    restore_untouched_active_task,
    task_dir,
    update_task_metadata,
)


def test_ensure_workspace_creates_layout(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    assert (tmp_path / ".litehive" / "config.yaml").exists()
    assert (tmp_path / ".litehive" / "state.yaml").exists()
    assert (tmp_path / ".litehive" / "tasks").exists()


def test_ensure_workspace_scaffolds_profile_specific_context(tmp_path: Path) -> None:
    django_path = tmp_path / "django"
    django_path.mkdir()

    from litehive.config import LitehiveConfig

    ensure_workspace(django_path, LitehiveConfig(process_profile="django"))

    context = (django_path / ".litehive" / "context.md").read_text(encoding="utf-8")

    assert "Process profile: Django" in context
    assert "## Init scaffold" in context
    assert "## Prompt scaffold" in context
    assert "## Django specifics" in context
    assert "migrations" in context
    assert (
        "Shared stages: grooming -> implementing -> testing -> accepting -> commit_to_git."
        in context
    )


def test_resolve_process_profile_merges_shared_process_with_overlay() -> None:
    profile = resolve_process_profile("codehive")

    assert profile["label"] == "Codehive-style"
    assert profile["shared_stages"] == [
        "grooming",
        "implementing",
        "testing",
        "accepting",
        "commit_to_git",
    ]
    assert (
        profile["orchestrator_model"]
        == "the orchestrator is the manager; subagents execute but do not choose routing."
    )
    assert profile["routing_model"].startswith("manager-owned deterministic routing")
    assert any("generic base prompt" in line for line in profile["prompt_scaffold"])
    assert profile["stage_overlay"]["accepting"][0].startswith("- Acceptance is managerial review")


def test_create_task_persists_folder_and_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Fix login race")
    tasks = list_tasks(tmp_path)
    state = load_state(tmp_path)

    assert task.id == "T-0001"
    assert len(tasks) == 1
    assert state.queue == ["T-0001"]
    assert (tmp_path / ".litehive" / "tasks" / "T-0001-fix-login-race" / "task.yaml").exists()


def test_create_task_seeds_tasks_mode_template_defaults(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Investigate queue stalls", task_type="research", mode="tasks")
    brief = (task_dir(tmp_path, task) / "brief.md").read_text(encoding="utf-8")

    assert task.goal == "Answer the open question with concrete evidence and a recommendation for next action."
    assert task.acceptance_criteria == [
        "The research question, scope, and decision to inform are stated clearly.",
        "Findings are grounded in repository evidence, experiments, or direct inspection.",
        "The output includes a recommendation, tradeoffs, and any follow-up tasks.",
    ]
    assert task.constraints == [
        "Prefer evidence from the repository and local experiments over speculation.",
        "Keep conclusions explicit about confidence and remaining unknowns.",
    ]
    assert task.plan == [
        "Define the exact question and scope of the investigation.",
        "Gather evidence from code, configs, tests, or focused experiments.",
        "Summarize findings, recommendation, and concrete follow-up actions.",
    ]
    assert "## Template Guidance" in brief
    assert "Frame the question, scope, and decision this research should inform." in brief
    assert "## Intake Notes" in brief
    assert "Question and scope: define what is being investigated and what is out of scope." in brief


@pytest.mark.parametrize(
    ("task_type", "title"),
    [
        ("adapter", "Add Gemini adapter"),
        ("bugfix", "Fix queue retry regression"),
        ("research", "Investigate queue stalls"),
        ("review", "Review adapter update"),
        ("refactor", "Refactor queue routing"),
    ],
)
def test_create_task_seeds_requested_task_type_templates(tmp_path: Path, task_type: str, title: str) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title=title, task_type=task_type, mode="tasks")
    template = tasks_module.TASK_TEMPLATES[task_type]
    brief = (task_dir(tmp_path, task) / "brief.md").read_text(encoding="utf-8")
    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert task.goal == template["goal"]
    assert task.acceptance_criteria == template["acceptance_criteria"]
    assert task.constraints == template["constraints"]
    assert task.plan == template["plan"]
    assert f"- Task type: {task_type}" in brief
    assert "## Template Guidance" in brief
    assert "## Intake Notes" in brief
    assert f"Task type: {task_type}" in prompt
    assert "Task template:" in prompt
    assert "Template sections to fill or verify:" in prompt

    for item in template["prompt_guidance"]:
        assert item in brief
        assert item in prompt
    for item in template["brief_sections"]:
        assert item in brief
        assert item in prompt


def test_create_task_preserves_explicit_fields_when_seeding_template_defaults(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(
        tmp_path,
        title="Stabilize flaky queue retry",
        task_type="bugfix",
        mode="tasks",
        goal="Eliminate the duplicate retry path",
        acceptance_criteria=["Queue retries once for a limit error"],
    )

    assert task.goal == "Eliminate the duplicate retry path"
    assert task.acceptance_criteria == ["Queue retries once for a limit error"]
    assert task.constraints
    assert task.plan


def test_create_task_persists_dependencies(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")
    dependent = create_task(
        tmp_path,
        title="Dependent task",
        depends_on=[first.id, second.id],
    )

    persisted = get_task(tmp_path, dependent.id)

    assert persisted is not None
    assert persisted.depends_on == [first.id, second.id]


def test_subagent_artifacts_exist_while_engine_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Persist live subagent artifacts")
    manager = SubagentManager(tmp_path)

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(self, prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            assert base.exists()
            session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
            assert session["id"] == "SA-0001"
            assert session["role"] == "swe"
            assert session["engine"] == "codex"
            assert session["status"] == "running"
            assert session["created_at"]
            assert session["updated_at"]
            assert session["exit_code"] is None
            assert (base / "prompt.txt").read_text(encoding="utf-8") == prompt
            assert (base / "transcript.md").read_text(encoding="utf-8") == ""
            assert (base / "stdout.txt").read_text(encoding="utf-8") == ""
            assert (base / "stderr.txt").read_text(encoding="utf-8") == ""
            report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
            assert report["status"] == "running"
            assert report["summary"] == ""
            refreshed = get_task(tmp_path, task.id)
            assert refreshed is not None
            assert refreshed.runtime.active_subagent is not None
            assert refreshed.runtime.active_subagent.path == "subagents/SA-0001-swe"
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout=(
                    "VERDICT: PASS\n"
                    "SUMMARY: artifacts persisted live\n"
                    "FILES_CHANGED:\n"
                    "- litehive/subagents.py\n"
                    "TESTS_ADDED: 1\n"
                    "TESTS_PASSING: 1\n"
                    "WARNINGS:\n"
                    "- none\n"
                ),
                stderr="",
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert result.ref.status == "completed"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["id"] == "SA-0001"
    assert session["role"] == "swe"
    assert session["engine"] == "codex"
    assert session["status"] == "completed"
    assert session["created_at"]
    assert session["updated_at"]
    assert session["exit_code"] == 0
    assert (base / "transcript.md").read_text(encoding="utf-8") == (
        "VERDICT: PASS\n"
        "SUMMARY: artifacts persisted live\n"
        "FILES_CHANGED:\n"
        "- litehive/subagents.py\n"
        "TESTS_ADDED: 1\n"
        "TESTS_PASSING: 1\n"
        "WARNINGS:\n"
        "- none\n"
    )
    assert (base / "stdout.txt").read_text(encoding="utf-8") == (
        "VERDICT: PASS\n"
        "SUMMARY: artifacts persisted live\n"
        "FILES_CHANGED:\n"
        "- litehive/subagents.py\n"
        "TESTS_ADDED: 1\n"
        "TESTS_PASSING: 1\n"
        "WARNINGS:\n"
        "- none\n"
    )
    assert (base / "stderr.txt").read_text(encoding="utf-8") == ""
    assert report == {
        "status": "completed",
        "summary": "artifacts persisted live",
        "files_changed": ["litehive/subagents.py"],
        "tests": {"added": 1, "passing": 1},
        "warnings": ["none"],
    }


def test_subagent_artifacts_update_live_during_streaming_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Stream live subagent artifacts")
    manager = SubagentManager(tmp_path)

    class FakeStreamingEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run_live(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_update=None,
        ) -> CLIExecutionResult:
            first = CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="VERDICT: PASS\nSUMMARY: streaming",
                stderr="partial stderr",
            )
            assert on_update is not None
            on_update(first)

            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
            report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
            assert session["created_at"]
            assert session["updated_at"]
            assert session["status"] == "running"
            assert session["exit_code"] is None
            assert (base / "stdout.txt").read_text(encoding="utf-8") == "VERDICT: PASS\nSUMMARY: streaming"
            assert (base / "stderr.txt").read_text(encoding="utf-8") == "partial stderr"
            assert (base / "transcript.md").read_text(encoding="utf-8") == (
                "VERDICT: PASS\nSUMMARY: streaming\n\n[stderr]\npartial stderr"
            )
            assert report["status"] == "running"
            assert report["summary"] == "streaming"

            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout=(
                    "VERDICT: PASS\n"
                    "SUMMARY: streaming complete\n"
                    "FILES_CHANGED:\n"
                    "- litehive/external_cli.py\n"
                ),
                stderr="",
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: FakeStreamingEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="stream it")

    assert result.ref.status == "completed"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "completed"
    assert session["created_at"]
    assert session["updated_at"]
    assert session["exit_code"] == 0
    assert (base / "stdout.txt").read_text(encoding="utf-8") == (
        "VERDICT: PASS\n"
        "SUMMARY: streaming complete\n"
        "FILES_CHANGED:\n"
        "- litehive/external_cli.py\n"
    )
    assert report["summary"] == "streaming complete"
    assert report["files_changed"] == ["litehive/external_cli.py"]


def test_subagent_manager_prefers_run_over_inherited_run_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback usage-limit task")
    manager = SubagentManager(tmp_path)
    engine = get_engine("codex")

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: engine)
    monkeypatch.setattr(engine, "is_available", lambda: True)

    calls: list[str] = []

    def fake_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        calls.append("run")
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
        )

    def fail_run_live(*args, **kwargs) -> CLIExecutionResult:  # type: ignore[no-untyped-def]
        raise AssertionError("run_live should not be used for adapters that only inherit it")

    monkeypatch.setattr(engine, "run", fake_run)
    monkeypatch.setattr(engine, "run_live", fail_run_live)

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert calls == ["run"]
    assert result.failure == EngineFailure(kind="execution_limit", reason="usage limit reached")


def test_create_task_rejects_missing_dependency(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with pytest.raises(ValueError, match="Task T-9999 not found"):
        create_task(tmp_path, title="Dependent task", depends_on=["T-9999"])


def test_create_task_rejects_dependency_cycle(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    first.depends_on = [second.id]
    save_task(tmp_path, first)

    with pytest.raises(
        ValueError, match=rf"Task {second.id} dependency cycle detected via {first.id}"
    ):
        update_task_metadata(tmp_path, second.id, depends_on=[first.id])


def test_runner_advances_task_to_done(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Implement feature")

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    reports = tmp_path / ".litehive" / "tasks" / "T-0001-implement-feature" / "reports"
    assert (reports / "grooming-001.yaml").exists()
    assert (reports / "implementing-002.yaml").exists()
    assert (reports / "testing-003.yaml").exists()
    assert (reports / "accepting-004.yaml").exists()
    assert (reports / "commit_to_git-005.yaml").exists()


def test_runner_keeps_review_rejections_looping_until_acceptance(tmp_path: Path) -> None:
    # max_retries=3 allows the 3 total rejections (2 testing + 1 accepting) to
    # loop all the way to done without hitting the retry-limit-exhausted guard.
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=3))
    task = create_task(tmp_path, title="Review loop")
    attempts = {"testing": 0, "accepting": 0}

    def executor(task, step):  # type: ignore[no-untyped-def]
        verdict = "pass"
        if step == "testing":
            attempts["testing"] += 1
            verdict = "fail" if attempts["testing"] <= 2 else "pass"
        elif step == "accepting":
            attempts["accepting"] += 1
            verdict = "reject" if attempts["accepting"] == 1 else "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}")

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=3)
    result = runner.run(task)

    assert result.final_status == "done"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.runtime.retry_count == 3
    assert task.runtime.retry_limit == 3
    assert task.runtime.retry_source == "global"
    assert task.runtime.last_outcome.kind is None

    first_testing_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-review-loop"
            / "reports"
            / "testing-003.yaml"
        ).read_text(encoding="utf-8")
    )
    assert first_testing_report["retry_count"] == 1
    assert first_testing_report["retry_limit"] == 3
    assert first_testing_report["retry_decision"] == "retry"

    second_testing_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-review-loop"
            / "reports"
            / "testing-005.yaml"
        ).read_text(encoding="utf-8")
    )
    assert second_testing_report["retry_count"] == 2
    assert second_testing_report["retry_limit"] == 3
    assert second_testing_report["retry_decision"] == "retry"

    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-review-loop"
            / "reports"
            / "accepting-008.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["retry_count"] == 3
    assert accepting_report["retry_limit"] == 3
    assert accepting_report["retry_decision"] == "retry"


def test_runner_blocks_large_task_without_acceptance_criteria_during_grooming(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Implement feature", goal="Ship deterministic routing")

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "flagged"
    finish_task_run_transition(tmp_path, task, result.final_status)
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    assert flagged.status == "flagged"
    assert flagged.runtime.last_outcome.reason_code == "missing_acceptance_criteria"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-implement-feature"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert "Structured acceptance criteria are required before implementation for larger tasks." in report["summary"]


def test_runner_blocks_direct_implementing_stage_without_acceptance_criteria(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume feature", goal="Ship deterministic routing")
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        raise AssertionError(f"executor should not run for {step}")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "flagged"
    finish_task_run_transition(tmp_path, task, result.final_status)
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    assert flagged.status == "flagged"
    assert flagged.runtime.last_outcome.stage == "implementing"
    assert flagged.runtime.last_outcome.reason_code == "missing_acceptance_criteria"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-resume-feature"
            / "reports"
            / "implementing-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert "Structured acceptance criteria are required before implementation for larger tasks." in report["summary"]


def test_runner_cancels_task_with_explicit_reason(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Cancelled run")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            raise KeyboardInterrupt()
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "cancelled"
    finish_task_run_transition(tmp_path, task, result.final_status)
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "cancelled"
    assert task.runtime.last_outcome.kind == "cancelled"
    assert task.runtime.last_outcome.stage == "testing"
    assert task.runtime.last_outcome.reason_code == "execution_cancelled"
    assert task.runtime.last_outcome.reason == "Execution cancelled during testing"
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
    assert report["outcome"] == "cancelled"
    assert report["outcome_reason_code"] == "execution_cancelled"
    assert report["outcome_reason"] == "Execution cancelled during testing"


def test_runner_fails_task_when_stage_executor_crashes(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Executor crash")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            raise RuntimeError("boom")
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "failed"
    finish_task_run_transition(tmp_path, task, result.final_status)
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "failed"
    assert task.runtime.last_outcome.kind == "failed"
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
    assert report["outcome"] == "failed"
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

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.pipeline_status == "testing":
            attempts["testing"] += 1
            if attempts["testing"] == 1:
                transcript = "\n".join(
                    [
                        "VERDICT: FAIL",
                        "SUMMARY: tests failed once",
                        "FILES_CHANGED:",
                        "TESTS_ADDED: 0",
                        "TESTS_PASSING: 0",
                        "WARNINGS:",
                    ]
                )
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-testing-codex",
                        role=role,
                        engine=engine_name,
                        status="completed",
                        path="subagents/testing-codex",
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
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, task.id)
    assert task is not None
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


def test_run_next_task_keeps_qa_and_pm_rejections_on_same_active_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    task = create_task(tmp_path, title="Iterate until accepted", auto_commit=False)
    task.retry_policy.max_retries = 3  # allow 2 testing fails + 1 accepting reject
    save_task(tmp_path, task)
    attempts = {"testing": 0, "accepting": 0}

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        transcript = "\n".join(
            [
                "VERDICT: PASS",
                f"SUMMARY: {task.pipeline_status} passed",
                "FILES_CHANGED:",
                "- app.txt",
                "TESTS_ADDED: 0",
                "TESTS_PASSING: 0",
                "WARNINGS:",
            ]
        )
        if task.pipeline_status == "testing":
            attempts["testing"] += 1
            if attempts["testing"] <= 2:
                transcript = "\n".join(
                    [
                        "VERDICT: FAIL",
                        f"SUMMARY: testing fail {attempts['testing']}",
                        "FILES_CHANGED:",
                        "TESTS_ADDED: 0",
                        "TESTS_PASSING: 0",
                        "WARNINGS:",
                    ]
                )
        elif task.pipeline_status == "accepting":
            attempts["accepting"] += 1
            if attempts["accepting"] == 1:
                transcript = "\n".join(
                    [
                        "VERDICT: REJECT",
                        "SUMMARY: pm wants another pass",
                        "FILES_CHANGED:",
                        "TESTS_ADDED: 0",
                        "TESTS_PASSING: 0",
                        "WARNINGS:",
                    ]
                )
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{task.pipeline_status}-codex",
                role=role,
                engine=engine_name,
                status="completed",
                path=f"subagents/{task.pipeline_status}-codex",
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

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.task.id == task.id
    assert summary.result is not None
    assert summary.result.final_status == "done"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.runtime.retry_count == 3
    assert refreshed.runtime.retry_limit == 3
    assert refreshed.runtime.retry_source == "task"
    assert refreshed.runtime.last_outcome.kind is None

    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-iterate-until-accepted"
            / "reports"
            / "accepting-008.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["verdict"] == "reject"
    assert accepting_report["retry_count"] == 3
    assert accepting_report["retry_limit"] == 3
    assert accepting_report["retry_source"] == "task"
    assert accepting_report["retry_decision"] == "retry"


def test_opencode_strips_provider_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {}

    def fake_run(cmd, cwd, capture_output, text, env, check):  # type: ignore[no-untyped-def]
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/fake")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("OPENCODE_API_KEY", "secret2")

    engine = get_engine("opencode")
    result = engine.run("hello", tmp_path)

    assert result.returncode == 0
    assert calls["cwd"] == str(tmp_path)
    assert list(calls["cmd"]) == ["opencode", "run", "--dir", str(tmp_path), "hello"]
    assert "OPENAI_API_KEY" not in calls["env"]
    assert "OPENCODE_API_KEY" not in calls["env"]


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
    assert codex.transcript_format == "text"
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
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(tmp_path),
        "--skip-git-repo-check",
        "ship it",
    ]


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
        "--dir",
        str(tmp_path),
        "--model",
        "zai-coding-plan/glm-5.1",
        "ship it",
    ]


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

    assert report.verdict == "pass"
    assert report.summary == "implemented Gemini adapter"
    assert report.files_changed == ["litehive/engines.py"]
    assert report.tests == {"added": 4, "passing": 4}


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
    assert report.verdict == "blocked"


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

    assert report.verdict == "pass"
    assert report.summary == "implemented Copilot adapter"
    assert report.files_changed == ["litehive/engines.py"]
    assert report.tests == {"added": 2, "passing": 2}


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
    assert report.verdict == "blocked"


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
    assert report.verdict == "blocked"


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


def test_parse_stage_report_text_extracts_shared_report_fields() -> None:
    report = parse_stage_report_text(
        task_id="T-0003",
        step="implementing",
        transcript=(
            "VERDICT: PASS\n"
            "SUMMARY: adapter contract added\n"
            "FILES_CHANGED:\n"
            "- litehive/engines.py\n"
            "- litehive/external_cli.py\n"
            "TESTS_ADDED: 3\n"
            "TESTS_PASSING: 8\n"
            "WARNINGS:\n"
            "- kept claude deferred\n"
        ),
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.summary == "adapter contract added"
    assert report.files_changed == ["litehive/engines.py", "litehive/external_cli.py"]
    assert report.tests == {"added": 3, "passing": 8}
    assert report.warnings == ["kept claude deferred"]


def test_stage_report_from_subagent_uses_adapter_execution_transcript(tmp_path: Path) -> None:
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

    assert report.summary == "execution transcript parsed"
    assert report.verdict == "pass"


def test_stage_prompt_includes_shared_process_and_profile_overlay(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Profiled task", goal="Ship deterministic routing")
    prompt = stage_prompt(
        task,
        "testing",
        workspace_context="## Project\n- Purpose: validate overlays",
        process_profile="codehive",
    )

    assert "Process profile: Codehive-style" in prompt
    assert (
        "Shared stages: grooming -> implementing -> testing -> accepting -> commit_to_git."
        in prompt
    )
    assert (
        "Routing model: manager-owned deterministic routing, retries, and escalation stay in local code rather than prompts."
        in prompt
    )
    assert "the orchestrator is the manager; subagents execute but do not choose routing." in prompt
    assert (
        "Combine the generic base prompt with the selected project overlay instead of replacing the base."
        in prompt
    )
    assert "Verification should be independent enough to catch behavioral regressions" in prompt
    assert "default to regression-first or test-first implementation" in prompt
    assert "accepted tasks commit by default at commit_to_git" in prompt


def test_stage_prompt_surfaces_acceptance_gate_for_large_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Profiled task", goal="Ship deterministic routing")

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "Acceptance gate:" in prompt
    assert "Structured acceptance criteria are required before implementation for larger tasks." in prompt


def test_stage_prompt_includes_task_type_and_plan(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Review adapter update", task_type="review", mode="tasks")

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "Task type: review" in prompt
    assert "Plan:" in prompt
    assert "Inspect the relevant change or workflow surface." in prompt
    assert "Task template:" in prompt
    assert "Prioritize correctness, regressions, and missing verification over style observations." in prompt
    assert "Template sections to fill or verify:" in prompt
    assert "Findings: record actionable issues with severity and supporting evidence." in prompt


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


def test_run_next_task_no_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    summary = run_next_task(tmp_path)

    assert summary.task is None
    assert summary.result is None


def test_run_task_pool_no_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    summary = run_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "queue_exhausted"


def test_run_task_pool_drains_dynamic_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id == first.id and get_task(tmp_path, "T-0002") is None:
            create_task(tmp_path, title="Second task", auto_commit=False)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path)

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

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
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

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
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


def test_run_task_pool_stops_by_default_when_limit_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
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

    summary = run_task_pool(tmp_path)

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


def test_run_task_pool_rereads_queue_order_between_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)
    third = create_task(tmp_path, title="Third task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id == first.id:
            move_queued_task(tmp_path, third.id, 1)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path)

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


def test_run_task_pool_picks_up_requeued_task_between_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    requeued = create_task(tmp_path, title="Requeued task", auto_commit=False)
    requeued.status = "flagged"
    requeued.pipeline_status = "testing"
    save_task(tmp_path, requeued)

    state = load_state(tmp_path)
    state.queue = [first.id]
    save_state(tmp_path, state)
    requeued_once = False

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        nonlocal requeued_once
        if task.id == first.id and not requeued_once:
            requeue_task(tmp_path, requeued.id)
            requeued_once = True
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path)

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


def test_run_task_pool_honors_stop_condition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_task_pool(tmp_path, stop_when=lambda executions: len(executions) >= 1)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "stop_condition_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_run_task_pool_restores_preselected_active_task_when_stop_condition_hits(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == first.id
    assert load_state(tmp_path).queue == [second.id]

    summary = run_task_pool(tmp_path, stop_when=lambda executions: True)

    assert summary.executions == []
    assert summary.stop_reason == "stop_condition_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [first.id, second.id]


def test_run_task_pool_pauses_for_human_checkpoint_before_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    checkpointed = create_task(
        tmp_path,
        title="Needs review before acceptance",
        human_checkpoints=["before_acceptance"],
        auto_commit=False,
    )
    queued = create_task(tmp_path, title="Queued behind checkpoint", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_task_pool(tmp_path)

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

    resume_summary = run_task_pool(tmp_path)

    assert [
        execution.task.id for execution in resume_summary.executions if execution.task is not None
    ] == [checkpointed.id, queued.id]
    assert resume_summary.stop_reason == "queue_exhausted"
    refreshed = get_task(tmp_path, checkpointed.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"


def test_run_task_pool_pauses_for_human_checkpoint_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    checkpointed = create_task(
        tmp_path,
        title="Needs review before commit",
        human_checkpoints=["before_commit"],
        auto_commit=False,
    )
    queued = create_task(tmp_path, title="Queued behind commit review", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_task_pool(tmp_path)

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

    resume_summary = run_task_pool(tmp_path)

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


def test_dequeue_next_task_selection_rolls_back_claim_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        dequeue_next_task_selection(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id is None
    assert restored.queue == [first.id, second.id]
    refreshed = get_task(tmp_path, first.id)
    assert refreshed is not None
    assert refreshed.status == "queued"


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


def test_run_task_pool_stops_after_max_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(max_tasks=1))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "max_tasks_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_run_task_pool_stops_on_first_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Failing task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
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

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_failure=True))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "failure_detected"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_run_task_pool_stops_on_execution_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
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

    summary = run_task_pool(
        tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_execution_limit=True)
    )

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "execution_limit_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_run_task_pool_stops_on_quota_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Third task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
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

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(quota_threshold=2))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001", "T-0002"]
    assert summary.stop_reason == "quota_threshold_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0003"]


def test_run_task_pool_stops_on_budget_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Budget task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
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

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(budget_threshold=1))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "budget_threshold_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_run_task_pool_stops_on_dirty_git_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Queued task", auto_commit=False)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    summary = run_task_pool(
        tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_dirty_git=True)
    )

    assert summary.executions == []
    assert summary.stop_reason == "dirty_git_state"


def test_run_task_pool_stops_on_pool_usage_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(pool_usage_cap=4))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "pool_usage_cap_reached"
    state = load_state(tmp_path)
    assert state.queue == ["T-0002"]


def test_run_task_pool_stops_on_pool_cost_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True))
    create_task(tmp_path, title="First task", engine="claude", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_task_pool(
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

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
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
    queued = create_task(tmp_path, title="Queued task")
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
    queued = create_task(tmp_path, title="Real queued task")
    completed = create_task(tmp_path, title="Completed queued task")

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
    assert task.id == queued.id
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id]


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
    assert task.id == prerequisite.id


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
    interrupted = create_task(tmp_path, title="Interrupted task")
    queued = create_task(tmp_path, title="New high priority task")

    queued.priority = "high"
    save_task(tmp_path, queued)
    set_active_task(tmp_path, interrupted.id)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == interrupted.id


@pytest.mark.parametrize("policy", ["fifo", "priority_first", "dependency_aware"])
def test_resolve_next_task_prefers_interrupted_queued_task_before_new_work(
    tmp_path: Path, policy: str
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy=policy))
    new_task = create_task(tmp_path, title="New task")
    interrupted = create_task(tmp_path, title="Interrupted task")

    new_task.priority = "high"
    interrupted.priority = "low"
    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    save_task(tmp_path, new_task)
    save_task(tmp_path, interrupted)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == interrupted.id


def test_resolve_next_task_dependency_aware_prefers_task_with_more_downstream_dependents(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="dependency_aware"))
    unrelated = create_task(tmp_path, title="Unrelated ready task")
    root = create_task(tmp_path, title="Dependency root")
    mid = create_task(tmp_path, title="Mid dependency")
    leaf = create_task(tmp_path, title="Leaf dependency")

    mid.depends_on = [root.id]
    leaf.depends_on = [mid.id]
    save_task(tmp_path, mid)
    save_task(tmp_path, leaf)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == root.id


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


def test_run_task_pool_skips_stale_queue_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Real task", auto_commit=False)
    state = load_state(tmp_path)
    state.queue = ["T-9999", task.id]
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None, max_turns=None: (
            _completed_subagent_result(tmp_path, current_task.pipeline_status)
        ),
    )

    summary = run_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [task.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_run_task_pool_skips_ineligible_active_and_queue_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Flagged active task", auto_commit=False)
    queued = create_task(tmp_path, title="Real task", auto_commit=False)
    completed = create_task(tmp_path, title="Completed queued task", auto_commit=False)

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
        lambda self, current_task, role, engine_name, prompt, model=None, max_turns=None: (
            _completed_subagent_result(tmp_path, current_task.pipeline_status)
        ),
    )

    summary = run_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [queued.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_run_task_pool_reports_blocked_tasks_remaining(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked task", auto_commit=False)
    missing = "T-9999"

    blocked.depends_on = [missing]
    save_task(tmp_path, blocked)

    summary = run_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "blocked_tasks_remaining"
    assert [entry.task_id for entry in summary.blocked] == [blocked.id]
    assert summary.blocked[0].blocked_by == [f"{missing} (missing)"]
    assert load_state(tmp_path).queue == [blocked.id]


def test_run_task_pool_reports_and_requeues_blocked_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked active task", auto_commit=False)
    blocked.depends_on = ["T-9999"]
    save_task(tmp_path, blocked)

    state = load_state(tmp_path)
    state.active_task_id = blocked.id
    state.queue = []
    save_state(tmp_path, state)

    summary = run_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "blocked_tasks_remaining"
    assert [entry.task_id for entry in summary.blocked] == [blocked.id]
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [blocked.id]


def test_run_task_pool_drains_active_task_without_queued_entries(
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
        lambda self, current_task, role, engine_name, prompt, model=None, max_turns=None: (
            _completed_subagent_result(tmp_path, current_task.pipeline_status)
        ),
    )

    summary = run_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [task.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_run_task_pool_keeps_active_review_loop_on_same_task_until_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    active = create_task(tmp_path, title="Active review loop", auto_commit=False)
    queued = create_task(tmp_path, title="Queued behind active", auto_commit=False)
    active.status = "in_progress"
    active.pipeline_status = "testing"
    active.retry_policy.max_retries = 2  # allow 1 testing fail + 1 accepting reject
    save_task(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    attempts = {"testing": 0, "accepting": 0}

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
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
            attempts["testing"] += 1
            if attempts["testing"] == 1:
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
        elif task.id == active.id and task.pipeline_status == "accepting":
            attempts["accepting"] += 1
            if attempts["accepting"] == 1:
                transcript = "\n".join(
                    [
                        "VERDICT: REJECT",
                        "SUMMARY: pm wants one more pass",
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

    summary = run_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [active.id, queued.id]
    assert summary.stop_reason == "queue_exhausted"
    refreshed_active = get_task(tmp_path, active.id)
    assert refreshed_active is not None
    assert refreshed_active.status == "done"
    assert refreshed_active.pipeline_status == "done"
    assert refreshed_active.runtime.retry_count == 2
    assert refreshed_active.runtime.retry_limit == 2
    assert refreshed_active.runtime.retry_source == "task"
    refreshed_queued = get_task(tmp_path, queued.id)
    assert refreshed_queued is not None
    assert refreshed_queued.status == "done"
    assert refreshed_queued.pipeline_status == "done"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_configure_persists_gemini_model(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="gemini",
        process_profile="generic",
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model="gemini-2.5-pro",
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.default_engine == "gemini"
    assert config.gemini_model == "gemini-2.5-pro"


def test_configure_persists_copilot_model(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="copilot",
        process_profile="generic",
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model="gpt-5",
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.default_engine == "copilot"
    assert config.copilot_model == "gpt-5"


def test_configure_persists_process_profile(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="rust",
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    context = (tmp_path / ".litehive" / "context.md").read_text(encoding="utf-8")

    assert config.process_profile == "rust"
    assert "Process profile: Rust" in context
    assert "## Init scaffold" in context
    assert "## Rust specifics" in context


def test_configure_persists_pool_stop_defaults(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        pool_stop_on_failure=True,
        pool_max_tasks=2,
        pool_stop_on_limit=True,
        pool_quota_threshold=3,
        pool_budget_threshold=1,
        pool_stop_on_dirty_git=True,
        pool_selection_policy="priority_first",
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.pool_stop_on_failure is True
    assert config.pool_max_tasks == 2
    assert config.pool_stop_on_execution_limit is True
    assert config.pool_quota_threshold == 3
    assert config.pool_budget_threshold == 1
    assert config.pool_stop_on_dirty_git is True
    assert config.pool_selection_policy == "priority_first"


def test_resolve_engine_name_prefers_run_override_then_task_then_workspace_default(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Queued task", engine="opencode")

    assert resolve_engine_name(task, config, engine_override="gemini") == "gemini"
    assert resolve_engine_name(task, config) == "opencode"

    task.engine = None
    assert resolve_engine_name(task, config) == config.default_engine


def test_litehive_config_merges_partial_task_engine_routing_override() -> None:
    config = LitehiveConfig(task_engine_routing={"research": ["opencode", "gemini", "codex"]})

    assert config.task_engine_routing["research"] == ["opencode", "gemini", "codex"]
    assert config.task_engine_routing["review"] == ["copilot", "codex", "opencode", "gemini"]


def test_resolve_engine_name_uses_task_routing_rule_before_workspace_default(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "gemini"
    assert resolve_engine_plan(task, config)[:3] == ["gemini", "codex", "opencode"]


def test_resolve_engine_name_prefers_explicit_task_type_over_keyword_inference(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior", task_type="review")

    assert resolve_engine_name(task, config) == "copilot"
    assert resolve_engine_plan(task, config)[:3] == ["copilot", "codex", "opencode"]


def test_resolve_engine_name_uses_configured_task_routing_override(
    tmp_path: Path,
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            task_engine_routing={"research": ["opencode", "gemini", "codex"]},
        ),
    )
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "opencode"
    assert resolve_engine_plan(task, config) == ["opencode", "gemini", "codex"]


def test_resolve_engine_name_skips_claude_in_routing_when_not_enabled(tmp_path: Path) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            task_engine_routing={"research": ["claude", "gemini", "codex"]},
        ),
    )
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "gemini"
    assert resolve_engine_plan(task, config) == ["gemini", "codex"]


def test_configure_persists_task_engine_routing_overrides(tmp_path: Path) -> None:
    from litehive.cli import _cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_enabled=False,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=30,
        pool_usage_cap=None,
        pool_cost_cap=None,
        engine_usage_cap=None,
        engine_budget_cap=None,
        engine_cost=None,
        task_engine_route=[
            "research=gemini,claude,codex",
            "bugfix=codex,opencode,copilot",
        ],
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
    )

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.task_engine_routing["research"] == ["gemini", "claude", "codex"]
    assert config.task_engine_routing["bugfix"] == ["codex", "opencode", "copilot"]
    assert config.task_engine_routing["review"] == ["copilot", "codex", "opencode", "gemini"]


def test_configure_rejects_invalid_task_engine_route(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from litehive.cli import _cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_enabled=False,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=30,
        pool_usage_cap=None,
        pool_cost_cap=None,
        engine_usage_cap=None,
        engine_budget_cap=None,
        engine_cost=None,
        task_engine_route=["research=gemini,unknown"],
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
    )

    assert _cmd_configure(parser) == 1
    assert "--task-engine-route engine must be one of:" in capsys.readouterr().out


def test_build_parser_accepts_run_dry_run_flag(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(["run", "--workspace", str(tmp_path), "--dry-run"])

    assert args.command == "run"
    assert args.workspace == tmp_path
    assert args.dry_run is True
    assert args.engine is None


def test_build_parser_accepts_acceptance_criteria_flags(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        [
            "add",
            "Ship task",
            "--workspace",
            str(tmp_path),
            "--acceptance-criteria",
            "first criterion",
            "--acceptance-criteria",
            "second criterion",
        ]
    )
    update_args = parser.parse_args(
        [
            "update",
            "T-0001",
            "--workspace",
            str(tmp_path),
            "--acceptance-criteria",
            "none",
        ]
    )

    assert add_args.acceptance_criteria == ["first criterion", "second criterion"]
    assert update_args.acceptance_criteria == ["none"]


def test_build_parser_accepts_human_checkpoint_flags(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        [
            "add",
            "Ship task",
            "--workspace",
            str(tmp_path),
            "--human-checkpoint",
            "before_acceptance",
            "--human-checkpoint",
            "before_commit",
        ]
    )
    update_args = parser.parse_args(
        [
            "update",
            "T-0001",
            "--workspace",
            str(tmp_path),
            "--human-checkpoint",
            "none",
        ]
    )

    assert add_args.human_checkpoint == ["before_acceptance", "before_commit"]
    assert update_args.human_checkpoint == ["none"]


def test_cmd_run_dry_run_shows_planned_tasks_and_stop_conditions_without_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued task", engine="opencode")

    def fail_run_task_pool(root: Path) -> None:
        raise AssertionError(f"run_task_pool should not be called for dry-run: {root}")

    monkeypatch.setattr("litehive.cli.run_task_pool", fail_run_task_pool)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "dry_run: true" in output
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 Queued task" in output
    assert "engine=opencode" in output
    assert "engine_attempts=opencode, codex, gemini, copilot" in output
    assert "human_checkpoints=-" in output
    assert "predicted_stop_reason: queue_exhausted" in output
    assert "stop_on_failure: False" in output
    assert load_state(tmp_path).queue == ["T-0001"]


def test_cmd_run_dry_run_prefers_run_engine_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued task", engine="opencode")

    def fail_run_task_pool(root: Path, engine_override: str | None = None) -> None:
        raise AssertionError(
            f"run_task_pool should not be called for dry-run: {root} {engine_override}"
        )

    monkeypatch.setattr("litehive.cli.run_task_pool", fail_run_task_pool)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, engine="gemini"))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 Queued task" in output
    assert "engine=gemini" in output
    assert "engine_attempts=gemini, codex, opencode, copilot" in output
    assert "human_checkpoints=-" in output
    assert load_state(tmp_path).queue == ["T-0001"]


def test_cmd_run_dry_run_plans_dependency_aware_pool_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked dependent", auto_commit=False)
    prerequisite = create_task(tmp_path, title="Prerequisite", engine="opencode", auto_commit=False)

    blocked.depends_on = [prerequisite.id]
    save_task(tmp_path, blocked)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 2" in output
    assert "would_run: 1. T-0002 Prerequisite" in output
    assert "would_run: 2. T-0001 Blocked dependent" in output
    assert "blocked_tasks: 0" in output


def test_cmd_run_dry_run_reports_max_tasks_predicted_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, max_tasks=1))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 First task" in output
    assert "would_run: 2." not in output
    assert "predicted_stop_reason: max_tasks_reached" in output


def test_cmd_run_dry_run_predicts_pool_usage_cap_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, pool_usage_cap=1))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 First task" in output
    assert "would_run: 2." not in output
    assert "predicted_stop_reason: pool_usage_cap_reached" in output


def test_cmd_run_dry_run_predicts_pool_cost_cap_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            pool_cost_cap=3,
            engine_cost=["codex=2"],
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 2" in output
    assert "would_run: 1. T-0001 First task" in output
    assert "would_run: 2. T-0002 Second task" in output
    assert "engine=opencode" in output
    assert "predicted_stop_reason: pool_cost_cap_reached" in output


def test_cmd_run_dry_run_uses_budget_allowed_fallback_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Research engine quota behavior", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            engine_usage_cap=["gemini=0"],
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "would_run: 1. T-0001 Research engine quota behavior" in output
    assert "engine=codex" in output
    assert "engine_attempts=gemini, codex, opencode, copilot" in output


def test_run_task_pool_uses_run_engine_override_for_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued task", engine="codex", auto_commit=False)
    seen_engines: list[str] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        seen_engines.append(engine_name)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path, engine_override="opencode")

    assert summary.stop_reason == "queue_exhausted"
    assert seen_engines == ["opencode", "opencode", "opencode", "opencode"]


def test_run_task_rejects_starting_a_second_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task", auto_commit=False)
    pending = create_task(tmp_path, title="Pending task", auto_commit=False)

    active.runtime.execution_status = "running"
    save_task_runtime(tmp_path, active)
    state = load_state(tmp_path)
    state.active_task_id = active.id
    save_state(tmp_path, state)

    with pytest.raises(
        WorkspaceConflictError,
        match=f"task {pending.id} cannot start because task {active.id} is already active",
    ):
        run_task(tmp_path, pending)


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


def test_cmd_run_drains_task_pool_and_reports_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 First task" in output
    assert "task: T-0002 Second task" in output
    assert (
        "stage_outcomes: grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in output
    )
    assert "completed_tasks: 2" in output
    assert (
        "completed: T-0001 First task status=done pipeline_status=done "
        "stage_outcomes=grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in output
    )
    assert (
        "completed: T-0002 Second task status=done pipeline_status=done "
        "stage_outcomes=grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in output
    )
    assert "failed_tasks: 0" in output
    assert "skipped_tasks: 0" in output
    assert "remaining_tasks: 0" in output
    assert "tasks_run: 2" in output
    assert "stop_reason: queue_exhausted" in output
    summary_report = (tmp_path / ".litehive" / "pool-summary.txt").read_text(encoding="utf-8")
    assert "completed_tasks: 2" in summary_report
    assert (
        "completed: T-0001 First task status=done pipeline_status=done "
        "stage_outcomes=grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in summary_report
    )
    assert "stop_reason: queue_exhausted" in summary_report
    assert load_state(tmp_path).queue == []


def test_cmd_run_reports_runner_conflict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued task", auto_commit=False)

    from litehive import tasks as tasks_module

    real_flock = tasks_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & tasks_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.tasks.fcntl.flock", fake_flock)

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
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "run failed: workspace is already being mutated by another runner" in output


def test_save_task_rejects_runner_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Queued task", auto_commit=False)

    from litehive import tasks as tasks_module

    real_flock = tasks_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & tasks_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.tasks.fcntl.flock", fake_flock)

    task.title = "Updated title"
    with pytest.raises(
        WorkspaceConflictError,
        match="workspace is already being mutated by another runner",
    ):
        save_task(tmp_path, task)


def test_save_state_rejects_runner_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    state = load_state(tmp_path)

    from litehive import tasks as tasks_module

    real_flock = tasks_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & tasks_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.tasks.fcntl.flock", fake_flock)

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

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No runnable task." in output
    assert f"blocked: {blocked.id} Blocked task blocked_by=T-9999 (missing)" in output
    assert "completed_tasks: 0" in output
    assert "failed_tasks: 0" in output
    assert "skipped_tasks: 1" in output
    assert "remaining_tasks: 1" in output
    assert (
        f"remaining: {blocked.id} Blocked task status=queued pipeline_status=backlog" in output
    )
    assert "tasks_run: 0" in output
    assert "stop_reason: blocked_tasks_remaining" in output


def test_cmd_run_reports_pre_execution_stop_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Queued task", auto_commit=False)
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
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No task executed." in output
    assert "completed_tasks: 0" in output
    assert "failed_tasks: 0" in output
    assert "skipped_tasks: 1" in output
    assert "remaining_tasks: 1" in output
    assert "remaining: T-0001 Queued task status=queued pipeline_status=backlog" in output
    assert "tasks_run: 0" in output
    assert "stop_condition: dirty git state" in output
    assert "stop_reason: dirty_git_state" in output


def test_cmd_run_reports_remaining_tasks_when_pool_stops_early(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
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
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "completed_tasks: 1" in output
    assert "failed_tasks: 0" in output
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
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: paused" in output
    assert "completed_tasks: 0" in output
    assert "failed_tasks: 0" in output
    assert "skipped_tasks: 2" in output
    assert "tasks_run: 1" in output
    assert "stop_condition: human checkpoint before acceptance" in output
    assert "stop_reason: human_checkpoint_before_acceptance" in output


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
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
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
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert (
        "skipped: T-0002 Second task status=queued pipeline_status=backlog "
        "stage_outcomes=grooming=pass"
        in output
    )
    assert (
        "remaining: T-0002 Second task status=queued pipeline_status=backlog "
        "stage_outcomes=grooming=pass"
        in output
    )


def test_cmd_run_reports_failed_task_summary_with_stage_outcomes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Needs acceptance criteria", auto_commit=False)
    task.pipeline_status = "implementing"
    task.priority = "high"
    save_task(tmp_path, task)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "failed_tasks: 1" in output
    assert (
        "failed: T-0001 Needs acceptance criteria status=flagged pipeline_status=implementing "
        "stage_outcomes=implementing=blocked"
        in output
    )
    assert "skipped_tasks: 0" in output
    assert "remaining_tasks: 0" in output
    assert "tasks_run: 1" in output
    assert "stop_reason: queue_exhausted" in output


def test_cmd_run_uses_configured_pool_stop_defaults(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_stop_on_dirty_git=True))
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Queued task", auto_commit=False)
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
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No queued task." in output
    assert "completed_tasks: 0" in output
    assert "failed_tasks: 0" in output
    assert "skipped_tasks: 0" in output
    assert "remaining_tasks: 0" in output
    assert "tasks_run: 0" in output
    assert "stop_condition: queue exhausted" in output
    assert "stop_reason: queue_exhausted" in output
    summary_report = (tmp_path / ".litehive" / "pool-summary.txt").read_text(encoding="utf-8")
    assert "completed_tasks: 0" in summary_report
    assert "failed_tasks: 0" in summary_report
    assert "stop_condition: queue exhausted" in summary_report


def test_status_output_includes_runtime_observability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_retry_limit=2,
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

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "default_retry_limit: 2" in output
    assert "pool_stop_on_failure: True" in output
    assert "pool_max_tasks: 4" in output
    assert "pool_stop_on_execution_limit: True" in output
    assert "pool_quota_threshold: 2" in output
    assert "pool_budget_threshold: 1" in output
    assert "pool_stop_on_dirty_git: True" in output
    assert "pool_selection_policy: priority_first" in output
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
        "last_subagent=SA-0001 swe/codex completed snippet=implemented live observability" in output
    )
    assert "last_report=grooming/pass duration=1m00s summary=plan confirmed" in output
    assert (
        "outcome=blocked stage=testing reason_code=verdict_blocked recorded_at=2026-03-31T10:02:30+00:00 retry_state=1/1 retry_source=task reason=waiting on fixture update"
        in output
    )


def test_queue_command_shows_active_and_queued_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task", engine="opencode")
    second.depends_on = [first.id]
    save_task(tmp_path, second)

    set_active_task(tmp_path, first.id)

    exit_code = _cmd_queue(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"active_task_id: {first.id}" in output
    assert (
        f"active: {first.id} [in_progress/backlog] priority=medium engine=codex (default) "
        "title=First task depends_on=-"
    ) in output
    assert (
        f"1. {second.id} [queued/backlog] priority=medium engine=opencode "
        f"title=Second task depends_on={first.id}"
    ) in output


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


def test_add_command_persists_task_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Review queue behavior",
            goal="",
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type="review",
            mode=None,
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    brief = (task_dir(tmp_path, task) / "brief.md").read_text(encoding="utf-8")
    assert task.mode == "tasks"
    assert task.task_type == "review"
    assert task.goal == "Review the target change critically and produce an actionable decision with supporting evidence."
    assert "## Template Guidance" in brief
    assert "mode: tasks" in output
    assert "task_type: review" in output


def test_add_command_can_force_implementation_mode_for_typed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Review queue behavior",
            goal="",
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type="review",
            mode="implementation",
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.mode == "implementation"
    assert task.task_type == "review"
    assert task.goal == ""
    assert not (task_dir(tmp_path, task) / "brief.md").exists()
    assert "mode: implementation" in output


def test_add_command_warns_when_large_task_lacks_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Large task",
            goal="Ship deterministic routing",
            acceptance_criteria=None,
            depends_on=None,
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "acceptance_criteria: 0" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output


def test_update_command_replaces_and_clears_dependencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")
    task = create_task(tmp_path, title="Dependent task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=[first.id, f"{second.id},{first.id}"],
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.depends_on == [first.id, second.id]
    assert f"depends_on: {first.id}, {second.id}" in output

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=["none"],
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    cleared = get_task(tmp_path, task.id)
    assert cleared is not None
    assert cleared.depends_on == []
    assert "depends_on: -" in output


def test_update_command_replaces_and_clears_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Tune task",
        goal="Ship queue CLI",
        acceptance_criteria=["Old criterion"],
    )

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=["First criterion", "Second criterion"],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.acceptance_criteria == ["First criterion", "Second criterion"]
    assert "acceptance_criteria: 2" in output

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=["none"],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    cleared = get_task(tmp_path, task.id)
    assert cleared is not None
    assert cleared.acceptance_criteria == []
    assert "acceptance_criteria: 0" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output


def test_add_command_rejects_missing_dependency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Blocked task",
            goal="",
            acceptance_criteria=None,
            depends_on=["T-9999"],
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "add failed: Task T-9999 not found" in output


def test_update_command_rejects_dependency_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    first.depends_on = [second.id]
    save_task(tmp_path, first)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=second.id,
            depends_on=[first.id],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"update failed: Task {second.id} dependency cycle detected via {first.id}" in output


def test_move_command_reorders_queue(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    third = create_task(tmp_path, title="Third task")

    exit_code = _cmd_move(argparse.Namespace(workspace=tmp_path, task_id=third.id, position=1))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "position: 1" in output
    assert load_state(tmp_path).queue == [third.id, first.id, second.id]


def test_promote_command_moves_queued_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=second.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "position: 1" in output
    assert load_state(tmp_path).queue == [second.id, first.id]


def test_promote_command_resumes_flagged_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Resume me first")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: testing" in output
    assert load_state(tmp_path).queue == [flagged.id, first.id]
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "testing"
    assert resumed.runtime.execution_status == "idle"


def test_requeue_command_requeues_flagged_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Needs another pass")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    from litehive.tasks import save_task

    save_task(tmp_path, task)

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=True)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: implementing" in output
    assert load_state(tmp_path).queue == [flagged.id, first.id]
    requeued = get_task(tmp_path, flagged.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "implementing"


def test_requeue_command_reroutes_large_task_without_acceptance_criteria_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Needs criteria", goal="Ship queue CLI")
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    flagged.status = "flagged"
    flagged.pipeline_status = "testing"
    save_task(tmp_path, flagged)

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output
    requeued = get_task(tmp_path, flagged.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "grooming"


def test_resume_command_preserves_flagged_task_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Resume later")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "accepting"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: accepting" in output
    assert load_state(tmp_path).queue == [first.id, flagged.id]
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "accepting"
    assert resumed.runtime.execution_status == "idle"


def test_resume_command_reroutes_large_task_missing_criteria_from_implementing_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume later", goal="Ship queue CLI")
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    flagged.status = "flagged"
    flagged.pipeline_status = "implementing"
    flagged.runtime.execution_status = "flagged"
    save_task(tmp_path, flagged)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "grooming"
    assert resumed.runtime.execution_status == "idle"


def test_requeue_command_requires_flagged_or_cancelled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Still queued")

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=task.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "is not flagged, failed, or cancelled" in output


def test_requeue_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Retry me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        requeue_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "testing"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task requeued for another implementation pass." not in journal


def test_abandon_command_cancels_task_and_removes_it_from_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Stop this task")
    queued = create_task(tmp_path, title="Keep this task")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = flagged.id
    save_state(tmp_path, state)

    exit_code = _cmd_abandon_task(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: cancelled" in output
    assert "pipeline_status: testing" in output
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id]
    abandoned = get_task(tmp_path, flagged.id)
    assert abandoned is not None
    assert abandoned.status == "cancelled"
    assert abandoned.runtime.execution_status == "cancelled"
    journal = (
        tmp_path / ".litehive" / "tasks" / f"{abandoned.id}-{abandoned.slug}" / "journal.md"
    ).read_text(encoding="utf-8")
    assert "Task abandoned via CLI at stage `testing`." in journal


def test_abandon_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Stop this task")
    queued = create_task(tmp_path, title="Keep this task")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.active_task_id = flagged.id
    save_state(tmp_path, state)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        _cmd_abandon_task(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.runtime.execution_status != "cancelled"
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id == flagged.id
    assert restored_state.queue == [flagged.id, queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task abandoned via CLI at stage `testing`." not in journal


def test_runner_flags_task_when_retry_limit_exhausted(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Exhausted task")
    attempts = {"testing": 0}

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            attempts["testing"] += 1
            verdict = "fail"
        else:
            verdict = "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}")

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=1)
    result = runner.run(task)

    # RunResult reflects the flagged terminal outcome.
    assert result.final_status == "flagged"
    # In-memory task status is set by the runner before returning.
    assert task.status == "flagged"
    # Persist the final status (mirrors what run_task() in runtime.py does).
    finish_task_run_transition(tmp_path, task, result.final_status)
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.runtime.last_outcome.kind == "flagged"
    assert refreshed.runtime.last_outcome.reason_code == "retry_limit_exhausted"


@pytest.mark.parametrize("outcome", ["wont_do", "deferred", "duplicate"])
def test_close_task_non_implementation_outcomes(tmp_path: Path, outcome: str) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close me")
    state = load_state(tmp_path)
    assert task.id in state.queue

    closed = close_task(tmp_path, task.id, outcome=outcome, reason="Test reason")

    assert closed.status == "cancelled"
    assert closed.runtime.last_outcome.kind == "cancelled"
    assert closed.runtime.last_outcome.reason_code == outcome
    assert closed.runtime.last_outcome.reason == "Test reason"
    state = load_state(tmp_path)
    assert task.id not in state.queue
    journal = (task_dir(tmp_path, closed) / "journal.md").read_text(encoding="utf-8")
    assert f"Task closed: {outcome}." in journal


def test_cmd_close_task_wont_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Will not implement")

    exit_code = _cmd_close_task(
        argparse.Namespace(workspace=tmp_path, task_id=task.id, outcome="wont_do", reason=None)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: cancelled" in output
    assert "outcome: wont_do" in output
    closed = get_task(tmp_path, task.id)
    assert closed is not None
    assert closed.status == "cancelled"
    assert closed.runtime.last_outcome.reason_code == "wont_do"


def test_update_command_updates_task_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="opencode",
            retry_limit="2",
            priority="high",
            goal="Ship queue CLI",
            acceptance_criteria=["Task is visible in queue"],
            human_checkpoint=["before_acceptance"],
            task_type="research",
            mode="tasks",
            auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "opencode"
    assert updated.retry_policy.max_retries == 2
    assert updated.priority == "high"
    assert updated.goal == "Ship queue CLI"
    assert updated.acceptance_criteria == ["Task is visible in queue"]
    assert updated.human_checkpoints == ["before_acceptance"]
    assert updated.task_type == "research"
    assert updated.mode == "tasks"
    assert updated.git.auto_commit is False
    assert "engine: opencode" in output
    assert "retry_limit: 2" in output
    assert "priority: high" in output
    assert "acceptance_criteria: 1" in output
    assert "human_checkpoints: before_acceptance" in output
    assert "task_type: research" in output


def test_update_command_seeds_template_defaults_when_switching_to_tasks_mode(
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
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.mode == "tasks"
    assert updated.task_type == "review"
    assert updated.goal == "Review the target change critically and produce an actionable decision with supporting evidence."
    assert updated.acceptance_criteria
    assert updated.constraints
    assert updated.plan
    assert "acceptance_criteria: 3" in output


def test_update_command_can_clear_task_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task", task_type="review")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type="default",
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.task_type is None
    assert "task_type: -" in output


def test_update_command_clears_task_retry_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune retry policy", retry_limit=2)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine=None,
            retry_limit="default",
            acceptance_criteria=None,
            human_checkpoint=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "retry_limit: default" in output
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.retry_policy.max_retries is None


def test_update_command_accepts_gemini_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Gemini task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="gemini",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "gemini"
    assert "engine: gemini" in output


def test_update_command_accepts_copilot_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Copilot task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="copilot",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "copilot"
    assert "engine: copilot" in output


def test_run_all_stops_before_run_when_pre_status_has_explicit_pool_stop_reason(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".litehive").mkdir()
    counts_dir = tmp_path / "counts"
    counts_dir.mkdir()
    run_count_file = counts_dir / "run-count"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "status" ]]; then
  echo "workspace: $5"
  echo "active_task_id: T-0001"
  echo "queued_tasks: 1"
  echo "pool_stop_reason: max_tasks_reached"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  count="$(cat "{run_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{run_count_file}"
  echo "unexpected run"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )

    result = subprocess.run(
        ["bash", str(_repo_root() / "scripts" / "run-all.sh"), str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=_with_fake_uv(fake_uv),
        check=False,
    )

    assert result.returncode == 0
    assert "Pool already stopped: max_tasks_reached" in result.stdout
    assert not run_count_file.exists()

    log_dirs = list((workspace / ".litehive" / "logs" / "run-all").iterdir())
    assert len(log_dirs) == 1
    assert (log_dirs[0] / "0001-pre-status.log").exists()
    assert not (log_dirs[0] / "0001-run.log").exists()


def test_run_all_restarts_litehive_until_queue_is_empty(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".litehive").mkdir()
    counts_dir = tmp_path / "counts"
    counts_dir.mkdir()
    status_count_file = counts_dir / "status-count"
    run_count_file = counts_dir / "run-count"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "status" ]]; then
  count="$(cat "{status_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{status_count_file}"

  queued_tasks=1
  stop_reason=None
  if [[ "$count" -eq 4 ]]; then
    queued_tasks=0
    stop_reason=queue_exhausted
  fi

  echo "workspace: $5"
  echo "active_task_id: None"
  echo "queued_tasks: $queued_tasks"
  echo "pool_stop_reason: $stop_reason"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  count="$(cat "{run_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{run_count_file}"
  echo "tasks_run: 1"
  echo "stop_reason: queue_exhausted"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )

    result = subprocess.run(
        ["bash", str(_repo_root() / "scripts" / "run-all.sh"), str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=_with_fake_uv(fake_uv),
        check=False,
    )

    assert result.returncode == 0
    assert "== iteration 1 ==" in result.stdout
    assert "== iteration 2 ==" in result.stdout
    assert "No active or queued tasks remain. Stopping." in result.stdout
    assert run_count_file.read_text(encoding="utf-8").strip() == "2"
    assert status_count_file.read_text(encoding="utf-8").strip() == "4"

    log_dirs = list((workspace / ".litehive" / "logs" / "run-all").iterdir())
    assert len(log_dirs) == 1
    log_dir = log_dirs[0]
    assert (log_dir / "0001-pre-status.log").exists()
    assert (log_dir / "0001-run.log").exists()
    assert (log_dir / "0001-post-status.log").exists()
    assert (log_dir / "0002-pre-status.log").exists()
    assert (log_dir / "0002-run.log").exists()
    assert (log_dir / "0002-post-status.log").exists()


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _with_fake_uv(fake_uv: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_uv.parent}:{env['PATH']}"
    return env


def _write_fake_uv(tmp_path: Path, script: str) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(script, encoding="utf-8")
    fake_uv.chmod(0o755)
    return fake_uv


def _init_git_repo(tmp_path: Path) -> str:
    _run(["git", "init"], tmp_path)
    _run(["git", "config", "user.name", "Litehive Tests"], tmp_path)
    _run(["git", "config", "user.email", "tests@example.com"], tmp_path)
    (tmp_path / "app.txt").write_text("base\n", encoding="utf-8")
    _run(["git", "add", "app.txt"], tmp_path)
    _run(["git", "commit", "-m", "initial"], tmp_path)
    return _run(["git", "rev-parse", "HEAD"], tmp_path)


def _completed_subagent_result(tmp_path: Path, step: str) -> SubagentResult:
    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}",
            role="swe",
            engine="codex",
            status="completed",
            path=f"subagents/{step}",
        ),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=tmp_path,
            exit_code=0,
            stdout=(
                "VERDICT: PASS\n"
                f"SUMMARY: {step} complete\n"
                "FILES_CHANGED:\n"
                "- app.txt\n"
                "TESTS_ADDED: 1\n"
                "TESTS_PASSING: 1\n"
                "WARNINGS:\n"
            ),
            stderr="",
        ),
        transcript="",
        exit_code=0,
    )


def _successful_stage_execution(tmp_path: Path, adapter: str, step: str) -> CLIExecutionResult:
    return CLIExecutionResult(
        adapter=adapter,
        argv=(adapter, "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout=(
            "VERDICT: PASS\n"
            f"SUMMARY: {step} complete via {adapter}\n"
            "FILES_CHANGED:\n"
            "- app.txt\n"
            "TESTS_ADDED: 1\n"
            "TESTS_PASSING: 1\n"
            "WARNINGS:\n"
        ),
        stderr="",
    )


def test_classify_execution_limit_matches_codex_usage_limit_transcript() -> None:
    transcript = (
        "[stderr]\n"
        "ERROR: You've hit your usage limit. Upgrade to Pro "
        "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at 5:26 PM."
    )

    assert classify_execution_limit(transcript) == "usage limit reached"


def test_run_next_task_uses_routing_plan_before_global_fallbacks_when_budget_blocks_first_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Research engine quota behavior", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        assert engine_name == "codex"
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task(
        tmp_path,
        require_task(tmp_path, "T-0001"),
        budget_ledger=EngineBudgetLedger(engine_usage_caps={"gemini": 0}),
    )

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "gemini"
    assert task.runtime.last_engine_switch.to_engine == "codex"
    assert "engine usage cap reached for `gemini`" in task.runtime.last_engine_switch.reason


def test_run_next_task_falls_back_from_codex_to_opencode_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fallback usage-limit task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)

    def fake_codex_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr=(
                    "ERROR: You've hit your usage limit. Upgrade to Pro "
                    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
                    "to purchase more credits or try again at 5:26 PM."
                ),
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_opencode_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        return _successful_stage_execution(tmp_path, "opencode", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    assert task.runtime.last_engine_switch.reason == "usage limit reached"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-fallback-usage-limit-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached."
        in report["warnings"]
    )
    assert report["feedback"].startswith(
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached."
    )
    assert "SUMMARY: grooming complete via opencode" in report["feedback"]
    _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out
    assert "engine_switch=grooming codex->opencode reason=usage limit reached" in output


def test_run_next_task_falls_back_from_codex_to_gemini_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={
                "codex": ["gemini"],
                "opencode": ["codex", "gemini", "copilot"],
                "gemini": ["codex", "opencode", "copilot"],
                "copilot": ["codex", "opencode", "gemini"],
            }
        ),
    )
    create_task(tmp_path, title="Gemini fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    gemini = get_engine("gemini")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: True)

    def fake_codex_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later or purchase more credits.",
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_gemini_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        transcript = (
            '{"type":"message","role":"assistant","content":"VERDICT: PASS\\n","delta":true}\n'
            f'{{"type":"message","role":"assistant","content":"SUMMARY: {step} complete via gemini\\nFILES_CHANGED:\\n- app.txt\\nTESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n","delta":true}}'
        )
        return CLIExecutionResult(
            adapter="gemini",
            argv=("gemini", "-p"),
            cwd=cwd,
            exit_code=0,
            stdout=transcript,
            stderr="",
        )

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(gemini, "run", fake_gemini_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "gemini"
    assert task.runtime.last_engine_switch.reason == "usage limit reached"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-gemini-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `gemini` after usage limit reached."
        in report["warnings"]
    )


def test_run_next_task_keeps_using_fallback_engine_after_implementing_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback usage-limit task", engine="codex", auto_commit=False)
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    codex = get_engine("codex")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)

    attempted_stages: list[tuple[str, str]] = []

    def fake_codex_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        attempted_stages.append(("codex", step))
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
        )

    def fake_opencode_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        attempted_stages.append(("opencode", step))
        return _successful_stage_execution(tmp_path, "opencode", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempted_stages == [
        ("codex", "implementing"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.step == "implementing"
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"


def test_run_next_task_walks_same_stage_fallback_graph_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={
                "codex": ["opencode"],
                "opencode": ["gemini"],
                "gemini": ["copilot"],
                "copilot": [],
            }
        ),
    )
    create_task(tmp_path, title="Chained fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    opencode = get_engine("opencode")
    gemini = get_engine("gemini")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: True)

    def fake_codex_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later.",
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_opencode_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=1,
                stdout="rate limit exceeded",
                stderr="",
            )
        return _successful_stage_execution(tmp_path, "opencode", "non-grooming")

    def fake_gemini_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        transcript = (
            '{"type":"message","role":"assistant","content":"VERDICT: PASS\\n","delta":true}\n'
            f'{{"type":"message","role":"assistant","content":"SUMMARY: {step} complete via gemini\\nFILES_CHANGED:\\n- app.txt\\nTESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n","delta":true}}'
        )
        return CLIExecutionResult(
            adapter="gemini",
            argv=("gemini", "-p"),
            cwd=cwd,
            exit_code=0,
            stdout=transcript,
            stderr="",
        )

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)
    monkeypatch.setattr(gemini, "run", fake_gemini_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "opencode"
    assert task.runtime.last_engine_switch.to_engine == "gemini"
    assert task.runtime.last_engine_switch.reason == "rate limit reached"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-chained-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"][:2] == [
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached.",
        "Stage `grooming` switched from `opencode` to `gemini` after rate limit reached.",
    ]
    assert report["feedback"].startswith(report["warnings"][0])
    assert "SUMMARY: grooming complete via gemini" in report["feedback"]


def test_run_next_task_skips_unavailable_fallback_engine_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={
                "codex": ["gemini", "opencode"],
                "opencode": ["codex", "gemini", "copilot"],
                "gemini": ["codex", "opencode", "copilot"],
                "copilot": ["codex", "opencode", "gemini"],
            }
        ),
    )
    create_task(tmp_path, title="Unavailable fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    gemini = get_engine("gemini")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: False)
    monkeypatch.setattr(opencode, "is_available", lambda: True)

    def fake_codex_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later.",
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_opencode_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        return _successful_stage_execution(tmp_path, "opencode", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "gemini"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-unavailable-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `gemini` after usage limit reached."
        in report["warnings"]
    )
    assert (
        "Stage `grooming` switched from `gemini` to `opencode` after Engine 'gemini' is unavailable: missing binary 'gemini'."
        in report["warnings"]
    )


def test_run_next_task_creates_checkpoint_commit_and_persists_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
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


def test_run_next_task_appends_attempt_suffix_after_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
    (tmp_path / "app.txt").write_text("updated-once\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    first = run_next_task(tmp_path)
    assert first.result is not None
    assert first.result.final_status == "done"

    rollback_completed_task(tmp_path, "T-0001")
    assert _run(["git", "status", "--short"], tmp_path) == ""

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
    assert task.git.rolled_back_checkpoint_attempt is None


def test_run_next_task_flags_task_when_commit_stage_prerequisite_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Needs git repo")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    assert task.runtime.last_outcome.kind == "flagged"
    assert task.runtime.last_outcome.reason_code == "verdict_fail"
    assert task.runtime.last_outcome.retry_limit == 3
    assert task.pipeline_status == "commit_to_git"
    assert task.git.commit_sha is None


def test_run_next_task_records_blocked_reason_code_when_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
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

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_outcome.kind == "blocked"
    assert task.runtime.last_outcome.reason_code == "verdict_blocked"
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
    assert report["outcome_reason_code"] == "verdict_blocked"


def test_run_next_task_skips_commit_stage_when_auto_commit_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Skip commit", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
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


def test_run_next_task_flags_task_when_repo_has_unrelated_dirty_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Dirty repo should block commit")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("unrelated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert summary.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    assert task.pipeline_status == "commit_to_git"
    assert task.git.commit_sha is None


def test_run_next_task_flags_task_when_other_task_state_is_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="Ship first task")
    create_task(tmp_path, title="Unrelated queued task")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.task.id == first.id
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert summary.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"
    task = get_task(tmp_path, first.id)
    assert task is not None
    assert task.status == "flagged"
    assert task.pipeline_status == "commit_to_git"
    assert task.git.commit_sha is None


def test_rollback_command_requeues_checkpointed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fix after done")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)

    exit_code = _cmd_rollback(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    rollback_output = capsys.readouterr().out

    assert exit_code == 0
    assert "rollback_commit:" in rollback_output
    assert "recovery_policy: rollback reverted the checkpoint and requeued the task" in rollback_output
    assert "next_commit_message: litehive: complete T-0001 fix-after-done" in rollback_output
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
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    recover_output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 Recover without revert" in recover_output
    assert "pipeline_status: implementing" in recover_output
    assert "recovery_policy: recover requeued the task without reverting workspace code" in recover_output
    assert "next_commit_message: litehive: complete T-0001 recover-without-revert" in recover_output
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "ship-again\n"
    assert load_state(tmp_path).queue == ["T-0001"]


def test_recover_completed_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover without revert")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        recover_completed_task(tmp_path, task.id)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == [task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task recovered for another implementation pass." not in journal


def test_rollback_completed_task_restores_state_when_rollback_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on commit failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)

    def fail_rollback_commit(root: Path, message: str):  # type: ignore[no-untyped-def]
        if message.startswith("litehive: rollback "):
            raise GitError("git rollback commit failed")
        return None

    monkeypatch.setattr("litehive.runtime.commit_task", fail_rollback_commit)

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
    assert _run(["git", "status", "--short"], tmp_path) == ""
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_recover_command_reroutes_large_task_without_acceptance_criteria_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(
        tmp_path,
        title="Recover with missing criteria",
        goal="Ship queue CLI",
        acceptance_criteria=["Task completes"],
    )
    (tmp_path / "app.txt").write_text("ship-again\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)
    update_task_metadata(tmp_path, "T-0001", acceptance_criteria=[])

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    recover_output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in recover_output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in recover_output
    recovered = get_task(tmp_path, "T-0001")
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


def test_claude_build_invocation_includes_model_and_max_turns(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter, get_engine

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
        max_turns=15,
    )
    invocation = adapter.build_invocation(
        "ship it",
        tmp_path,
        model="claude-sonnet-4-20250514",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "claude",
        "-p",
        "ship it",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-sonnet-4-20250514",
        "--max-turns",
        "15",
    ]


def test_claude_default_max_turns_is_30(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )
    invocation = adapter.build_invocation("hello", tmp_path)

    assert "--max-turns" in invocation.argv
    idx = list(invocation.argv).index("--max-turns")
    assert list(invocation.argv)[idx + 1] == "30"


def test_claude_build_invocation_allows_max_turn_override(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
        max_turns=30,
    )
    invocation = adapter.build_invocation("hello", tmp_path, max_turns=7)

    assert "--max-turns" in invocation.argv
    idx = list(invocation.argv).index("--max-turns")
    assert list(invocation.argv)[idx + 1] == "7"


def test_run_next_task_passes_configured_claude_max_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True, claude_max_turns=7))
    create_task(tmp_path, title="Claude max turns task", engine="claude", auto_commit=False)
    calls: list[int | None] = []

    def fake_run(self, prompt, cwd, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        calls.append(max_turns)
        return CLIExecutionResult(
            adapter="claude",
            argv=("claude", "-p"),
            cwd=cwd,
            exit_code=0,
            stdout="\n".join(
                [
                    "VERDICT: PASS",
                    "SUMMARY: ok",
                    "FILES_CHANGED:",
                    "TESTS_ADDED: 0",
                    "TESTS_PASSING: 0",
                    "WARNINGS:",
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr("litehive.engines.ClaudeCLIAdapter.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert calls
    assert calls[0] == 7


def test_claude_renders_jsonl_transcript_and_stage_report(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"system","subtype":"init"}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"VERDICT: PASS\\n"}]}}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"SUMMARY: implemented Claude adapter\\n"}]}}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"FILES_CHANGED:\\n- litehive/engines.py\\n"}]}}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"TESTS_ADDED: 3\\nTESTS_PASSING: 3\\nWARNINGS:\\n"}]}}',
                '{"type":"result","result":"done"}',
            ]
        ),
        stderr="",
    )

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )

    assert adapter.render_transcript(execution).splitlines()[0] == "VERDICT: PASS"
    report = adapter.parse_stage_report(
        task_id="T-0006",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.summary == "implemented Claude adapter"
    assert report.files_changed == ["litehive/engines.py"]
    assert report.tests == {"added": 3, "passing": 3}


def test_claude_stage_report_uses_error_when_no_assistant_message(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"error","data":{"message":"authentication required"}}',
        stderr="",
    )

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )
    report = adapter.parse_stage_report(
        task_id="T-0006",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "authentication required"
    assert report.verdict == "blocked"


def test_resolve_engine_name_rejects_claude_when_not_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    config = load_config(tmp_path)
    assert config.claude_enabled is False

    from litehive.engines import EngineError

    task = create_task(tmp_path, title="Claude task", engine="claude")
    with pytest.raises(EngineError, match="opt-in"):
        resolve_engine_name(task, config)


def test_resolve_engine_name_rejects_default_claude_when_not_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    config = load_config(tmp_path)
    assert config.default_engine == "claude"
    assert config.claude_enabled is False

    from litehive.engines import EngineError

    task = create_task(tmp_path, title="Claude default task")
    with pytest.raises(EngineError, match="opt-in"):
        resolve_engine_name(task, config)


def test_resolve_engine_name_allows_claude_when_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True))
    config = load_config(tmp_path)
    assert config.claude_enabled is True

    task = create_task(tmp_path, title="Claude task", engine="claude")
    assert resolve_engine_name(task, config) == "claude"


def test_claude_is_not_default_engine() -> None:
    config = LitehiveConfig()
    assert config.default_engine != "claude"
    assert config.claude_enabled is False


def test_claude_config_defaults_to_sonnet() -> None:
    config = LitehiveConfig(claude_enabled=True)
    assert config.claude_model == "claude-sonnet-4-20250514"
    assert config.claude_max_turns == 30


def test_claude_not_in_engine_fallbacks() -> None:
    config = LitehiveConfig()
    for engine, fallbacks in config.engine_fallbacks.items():
        assert "claude" not in fallbacks, f"claude should not be a fallback for {engine}"


def test_claude_engine_in_registry() -> None:
    engine = get_engine("claude")
    assert engine.name == "claude"
    assert engine.capabilities.supports_model_override is True
    assert engine.capabilities.transcript_format == "jsonl"


def test_update_command_accepts_claude_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True))
    task = create_task(tmp_path, title="Tune Claude task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="claude",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "claude"
    assert "engine: claude" in output


def test_configure_persists_claude_settings(tmp_path: Path) -> None:
    from litehive.cli import _cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_enabled=True,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=20,
        pool_usage_cap=12,
        pool_cost_cap=30,
        engine_usage_cap=["claude=2", "codex=5"],
        engine_budget_cap=["claude=6"],
        engine_cost=["claude=3", "codex=1"],
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
    )

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.claude_enabled is True
    assert config.claude_model == "claude-sonnet-4-20250514"
    assert config.claude_max_turns == 20
    assert config.pool_usage_cap == 12
    assert config.pool_cost_cap == 30
    assert config.engine_usage_caps == {"claude": 2, "codex": 5}
    assert config.engine_budget_caps == {"claude": 6}
    assert config.engine_costs["claude"] == 3
    assert config.task_engine_routing["research"][0] == "gemini"


def test_claude_model_resolved_in_model_for_engine() -> None:
    from litehive.runtime import _model_for_engine

    config = LitehiveConfig(claude_model="claude-sonnet-4-20250514")
    assert _model_for_engine(config, "claude") == "claude-sonnet-4-20250514"

    config_default = LitehiveConfig()
    assert _model_for_engine(config_default, "claude") == "claude-sonnet-4-20250514"


def test_cmd_run_dry_run_rejects_default_claude_when_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litehive.engines import EngineError

    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Claude default task")

    def fail_run_task_pool(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_task_pool should not be called for dry-run")

    monkeypatch.setattr("litehive.cli.run_task_pool", fail_run_task_pool)

    with pytest.raises(EngineError, match="opt-in"):
        _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, engine=None))
