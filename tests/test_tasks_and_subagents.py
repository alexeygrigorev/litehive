from tests.workspace_helpers import *  # noqa: F401,F403


def test_create_task_persists_folder_and_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Fix login race")
    tasks = list_tasks(tmp_path)
    state = load_state(tmp_path)

    assert task.id == "T-0001"
    assert len(tasks) == 1
    assert state.queue == ["T-0001"]
    assert (tmp_path / ".litehive" / "tasks" / "T-0001-fix-login-race" / "task.yaml").exists()


def test_save_task_rolls_back_task_record_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Atomic save", auto_commit=False)
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        save_task(tmp_path, task)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.execution_status == "idle"


def test_workspace_transition_writes_preserve_task_added_after_state_snapshot(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    script = """
import json
import yaml
from pathlib import Path
import litehive.tasks as tasks_module
from litehive.config import ensure_workspace
from litehive.tasks import create_task, load_state

root = Path(__import__("sys").argv[1])
ensure_workspace(root)
active = create_task(root, title="Active task")
queued = create_task(root, title="Queued task")
stale_state = load_state(root)
added = create_task(root, title="Added later")
active.status = "done"
active.pipeline_status = "done"
stale_state.active_task_id = None
stale_state.queue = [queued.id]
writes = tasks_module._workspace_transition_writes(root, tasks=[active], state=stale_state)
serialized_state = yaml.safe_load(writes[tasks_module.state_path(root)])
print(json.dumps({
    "queue": serialized_state["queue"],
    "next_task_number": serialized_state["next_task_number"],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.stdout.strip() == '{"queue": ["T-0002", "T-0003"], "next_task_number": 3}'


def test_create_task_preserves_runner_queue_changes_after_state_snapshot(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    script = """
import json
from pathlib import Path
import litehive.tasks as tasks_module
from litehive.config import ensure_workspace
from litehive.tasks import create_task, load_state

root = Path(__import__("sys").argv[1])
ensure_workspace(root)
first = create_task(root, title="First queued task")
second = create_task(root, title="Second queued task")
original_merge = tasks_module._merged_state_for_runner_owned_write
injected = False

def inject_latest_state(root, *, state, protected_task_ids=()):
    global injected
    if not injected:
        injected = True
        latest = load_state(root)
        latest.queue = [second.id, first.id]
        tasks_module._save_state_without_runner_guard(root, latest)
    return original_merge(root, state=state, protected_task_ids=protected_task_ids)

tasks_module._merged_state_for_runner_owned_write = inject_latest_state
added = create_task(root, title="Added while runner updated queue")
print(json.dumps({"id": added.id, "queue": load_state(root).queue}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.stdout.strip() == '{"id": "T-0003", "queue": ["T-0002", "T-0001", "T-0003"]}'


def test_create_follow_up_tasks_preserves_runner_queue_changes_after_state_snapshot(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    script = """
import json
from pathlib import Path
import litehive.tasks as tasks_module
from litehive.config import ensure_workspace
from litehive.models import FollowUpTaskSpec
from litehive.tasks import create_follow_up_tasks, create_task, load_state

root = Path(__import__("sys").argv[1])
ensure_workspace(root)
parent = create_task(root, title="Parent task")
sibling = create_task(root, title="Sibling task")
original_merge = tasks_module._merged_state_for_runner_owned_write
injected = False

def inject_latest_state(root, *, state, protected_task_ids=()):
    global injected
    if not injected:
        injected = True
        latest = load_state(root)
        latest.queue = [sibling.id, parent.id]
        tasks_module._save_state_without_runner_guard(root, latest)
    return original_merge(root, state=state, protected_task_ids=protected_task_ids)

tasks_module._merged_state_for_runner_owned_write = inject_latest_state
created = create_follow_up_tasks(
    root,
    parent_task=parent,
    stage="grooming",
    follow_ups=[
        FollowUpTaskSpec(title="First follow-up", rationale="Needs separate work"),
        FollowUpTaskSpec(title="Second follow-up", rationale="Needs more detail"),
    ],
)
print(json.dumps({"ids": [task.id for task in created], "queue": load_state(root).queue}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        env=env,
    )

    assert (
        result.stdout.strip()
        == '{"ids": ["T-0003", "T-0004"], "queue": ["T-0002", "T-0001", "T-0003", "T-0004"]}'
    )


def test_create_task_seeds_next_task_number_from_existing_workspace_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Existing task")
    create_task(tmp_path, title="Second task")

    state_file = tmp_path / ".litehive" / "state.yaml"
    legacy_state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    legacy_state.pop("next_task_number", None)
    state_file.write_text(yaml.safe_dump(legacy_state, sort_keys=False), encoding="utf-8")

    created = create_task(tmp_path, title="Third task")

    assert created.id == "T-0003"
    assert load_state(tmp_path).next_task_number == 3


def test_create_task_uses_persisted_next_task_number_without_rescanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Existing task")

    def fail_scan(root: Path) -> int:
        raise AssertionError("task id allocation should not rescan task directories")

    monkeypatch.setattr(tasks_module, "_highest_task_number_on_disk", fail_scan)

    created = create_task(tmp_path, title="Second task")

    assert created.id == "T-0002"
    assert load_state(tmp_path).next_task_number == 2


def test_create_task_seeds_tasks_mode_template_defaults(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(
        tmp_path, title="Investigate queue stalls", task_type="research", mode="tasks"
    )
    brief = (task_dir(tmp_path, task) / "brief.md").read_text(encoding="utf-8")

    assert (
        task.goal
        == "Answer the open question with concrete evidence and a recommendation for next action."
    )
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
    assert "### Question and Scope" in brief
    assert "Define what is being investigated and what is out of scope." in brief
    assert "_TBD_" in brief


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
def test_create_task_seeds_requested_task_type_templates(
    tmp_path: Path, task_type: str, title: str
) -> None:
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
        assert item in prompt
    for stub in template["brief_section_stubs"]:
        assert f"### {stub['title']}" in brief
        assert stub["prompt"] in brief


def test_intake_prompt_uses_codehive_style_guidance() -> None:
    prompt = intake_prompt("Need a rough task from this brain dump.")

    assert "You are the planner for a local multi-agent coding workspace." in prompt
    assert "You are handling freeform task intake for a Codehive-style workflow." in prompt
    assert (
        "Preserve execution visibility through task reports, subagent transcripts, and recent progress."
        in prompt
    )
    assert (
        "Do not add acceptance criteria, implementation plans, decomposition, or detailed structure."
        in prompt
    )
    assert "Treat the original dump as the authoritative source of detail." in prompt
    assert "TITLE: <concise rough task title>" in prompt
    assert "GOAL: <1-3 sentence high-level goal statement>" in prompt


def test_intake_command_creates_linked_task_from_freeform_dump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)

    captured: dict[str, object] = {}

    class FakeEngine:
        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            captured["prompt"] = prompt
            captured["cwd"] = cwd
            captured["model"] = model
            captured["max_turns"] = max_turns
            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout="TITLE: Capture queue visibility gaps\nGOAL: Turn the raw notes into a queued task planner can groom later.\n",
                stderr="",
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.cli.tasks.get_engine", lambda _: FakeEngine())

    dump = "We need better queue visibility.\nShow stage, transcript, and last progress in the task view.\n"
    intake_file = tmp_path / "brain-dump.md"
    intake_file.write_text(dump, encoding="utf-8")

    exit_code = _cmd_intake(
        argparse.Namespace(
            file=intake_file,
            engine="opencode",
            model=None,
            workspace=tmp_path,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.title == "Capture queue visibility gaps"
    assert task.goal == (
        "Turn the raw notes into a queued task planner can groom later.\n\n"
        "(See intake.md for the original brain dump)"
    )
    assert task.mode == "tasks"
    assert task.task_type == "intake"
    assert task.status == "queued"
    assert task.pipeline_status == "backlog"
    assert captured["cwd"] == tmp_path
    assert captured["model"] == "zai-coding-plan/glm-5.1"
    assert captured["max_turns"] is None
    assert "Codehive-style specifics:" in str(captured["prompt"])

    base = task_dir(tmp_path, task)
    assert (base / "intake.md").read_text(encoding="utf-8") == dump
    brief = (base / "brief.md").read_text(encoding="utf-8")
    assert "- Original dump: [intake.md](intake.md)" in brief
    assert "Treat `intake.md` as the authoritative source for the raw specification." in brief
    assert dump.strip() not in brief
    assert "Created task T-0001: Capture queue visibility gaps" in output
    assert "Original dump preserved at:" in output


def test_intake_command_rolls_back_created_task_when_post_create_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)

    class FakeEngine:
        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout="TITLE: Capture queue visibility gaps\nGOAL: Turn the raw notes into a queued task PM can groom later.\n",
                stderr="",
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.cli.tasks.get_engine", lambda _: FakeEngine())
    monkeypatch.setattr(
        "litehive.cli.tasks._link_intake_brief_to_source",
        lambda _: (_ for _ in ()).throw(OSError("disk full")),
    )

    intake_file = tmp_path / "brain-dump.md"
    intake_file.write_text("We need better queue visibility.\n", encoding="utf-8")

    exit_code = _cmd_intake(
        argparse.Namespace(
            file=intake_file,
            engine="opencode",
            model=None,
            workspace=tmp_path,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Task creation failed: disk full" in output
    assert get_task(tmp_path, "T-0001") is None
    assert load_state(tmp_path).queue == []
    assert list((tmp_path / ".litehive" / "tasks").iterdir()) == []


def test_update_task_fills_only_unset_template_fields_for_typed_tasks(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Review queue behavior",
        task_type="review",
        mode="tasks",
        goal="Use explicit review framing",
        acceptance_criteria=["Call out the highest-risk regression first."],
    )

    updated = tasks_module.update_task(tmp_path, task.id, mode="tasks", task_type="review")

    assert updated.goal == "Use explicit review framing"
    assert updated.acceptance_criteria == ["Call out the highest-risk regression first."]
    assert updated.constraints == tasks_module.TASK_TEMPLATES["review"]["constraints"]
    assert updated.plan == tasks_module.TASK_TEMPLATES["review"]["plan"]


def test_create_task_preserves_explicit_fields_when_seeding_template_defaults(
    tmp_path: Path,
) -> None:
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

        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            assert max_turns is None
            assert on_started is not None
            on_started(4242)
            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            assert base.exists()
            session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
            assert session["id"] == "SA-0001"
            assert session["role"] == "swe"
            assert session["engine"] == "codex"
            assert session["status"] == "running"
            assert session["created_at"]
            assert session["updated_at"]
            assert session["pid"] == 4242
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
            assert refreshed.runtime.active_subagent.pid == 4242
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
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: FakeEngine())

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
    assert session["pid"] == 4242
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
    assert not (base / "stderr.txt.gz").exists()
    if (base / "stderr.txt").exists():
        assert (base / "stderr.txt").read_text(encoding="utf-8") == ""
    assert report["status"] == "completed"
    # Text-based verdict parsing removed; summary is now first line of transcript.
    assert report["summary"] == "VERDICT: PASS"
    assert report["files_changed"] == []
    assert report["resource_control"]["enabled"] is False
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.active_subagent is None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.pid == 4242
    monitoring = load_engine_monitoring(tmp_path)
    assert monitoring.engines["codex"].invocation_count == 1
    assert monitoring.engines["codex"].success_count == 1


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
            inactivity_timeout_seconds=None,
        ) -> CLIExecutionResult:
            first = CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="VERDICT: PASS\nSUMMARY: streaming",
                stderr="partial stderr",
                pid=5151,
            )
            assert on_update is not None
            on_update(first)

            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
            report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
            assert session["created_at"]
            assert session["updated_at"]
            assert session["status"] == "running"
            assert session["pid"] == 5151
            assert session["exit_code"] is None
            assert (base / "stdout.txt").read_text(
                encoding="utf-8"
            ) == "VERDICT: PASS\nSUMMARY: streaming"
            assert (base / "stderr.txt").read_text(encoding="utf-8") == "partial stderr"
            assert (base / "transcript.md").read_text(encoding="utf-8") == (
                "VERDICT: PASS\nSUMMARY: streaming\n\n[stderr]\npartial stderr"
            )
            assert report["status"] == "running"
            assert report["summary"] == "VERDICT: PASS"

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
                pid=5151,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: FakeStreamingEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="stream it")

    assert result.ref.status == "completed"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "completed"
    assert session["created_at"]
    assert session["updated_at"]
    assert session["pid"] == 5151
    assert session["exit_code"] == 0
    assert (base / "stdout.txt").read_text(encoding="utf-8") == (
        "VERDICT: PASS\nSUMMARY: streaming complete\nFILES_CHANGED:\n- litehive/external_cli.py\n"
    )
    assert report["summary"] == "VERDICT: PASS"
    assert report["files_changed"] == []
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.pid == 5151


def test_subagent_manager_records_copilot_quota_monitoring_during_live_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Stream Copilot quota usage")
    manager = SubagentManager(tmp_path)
    adapter = get_engine("copilot")

    def fake_run_live(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
        on_update=None,
        inactivity_timeout_seconds=None,
    ) -> CLIExecutionResult:
        del prompt, model, max_turns
        if on_started is not None:
            on_started(6262)
        update = CLIExecutionResult(
            adapter="copilot",
            argv=("copilot", "-p"),
            cwd=cwd,
            exit_code=0,
            stdout=(
                '{"type":"assistant.usage","data":{"model":"gpt-5",'
                '"inputTokens":120,"outputTokens":30,"cost":2,'
                '"quotaSnapshots":{"premium_interactions":{"isUnlimitedEntitlement":false,'
                '"entitlementRequests":100,"usedRequests":60,'
                '"usageAllowedWithExhaustedQuota":false,"overage":0,'
                '"overageAllowedWithExhaustedQuota":false,'
                '"remainingPercentage":0.4,'
                '"resetDate":"2026-04-30T00:00:00Z"}}}}\n'
            ),
            stderr="",
            pid=6262,
        )
        assert on_update is not None
        on_update(update)

        monitoring = load_engine_monitoring(tmp_path)
        record = monitoring.engines["copilot"]
        assert record.source == "provider"
        assert record.provider == "github"
        assert record.invocation_count == 0
        assert record.success_count == 0
        assert record.failure_count == 0
        assert record.usage is not None
        assert record.usage.used == 60
        assert record.usage.remaining == 40
        assert record.usage.reset_at == "2026-04-30T00:00:00Z"
        assert record.metadata["quota_snapshot"] == "premium_interactions"

        return update

    monkeypatch.setattr(adapter, "run_live", fake_run_live)
    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: adapter)

    result = manager.run(task, role="swe", engine_name="copilot", prompt="monitor quota")

    assert result.ref.status == "completed"
    monitoring = load_engine_monitoring(tmp_path)
    record = monitoring.engines["copilot"]
    assert record.invocation_count == 1
    assert record.success_count == 1
    assert record.failure_count == 0
    assert record.usage is not None
    assert record.usage.used == 60
    assert record.usage.remaining == 40


def test_subagent_artifacts_stream_to_disk_while_process_is_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tail live subagent artifacts")
    manager = SubagentManager(tmp_path)

    class TestAdapter(ExternalCLIAdapter):
        def __init__(self) -> None:
            super().__init__(
                name="codex",
                binary="bash",
                capabilities=AdapterCapabilities(available=True),
            )

        def is_available(self) -> bool:
            return True

        def build_command(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            resume_session_id: str | None = None,
        ) -> list[str]:
            del prompt, cwd, model, max_turns, resume_session_id
            return [
                "bash",
                "-lc",
                "printf 'VERDICT: PASS\\nSUMMARY: live start\\n'; "
                "printf 'live stderr\\n' >&2; "
                "sleep 1.2; "
                "printf 'FILES_CHANGED:\\n- litehive/external_cli.py\\nTESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n';",
            ]

    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: TestAdapter())

    result_holder: dict[str, SubagentResult] = {}
    error_holder: list[BaseException] = []

    def run_manager() -> None:
        try:
            result_holder["result"] = manager.run(
                task, role="swe", engine_name="codex", prompt="stream it"
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            error_holder.append(exc)

    worker = threading.Thread(target=run_manager)
    worker.start()

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    deadline = time.time() + 5
    while time.time() < deadline:
        if base.exists() and (base / "stdout.txt").exists() and (base / "stderr.txt").exists():
            stdout_text = (base / "stdout.txt").read_text(encoding="utf-8")
            stderr_text = (base / "stderr.txt").read_text(encoding="utf-8")
            if "SUMMARY: live start" in stdout_text and "live stderr" in stderr_text:
                break
        time.sleep(0.05)
    else:
        worker.join(timeout=0)
        raise AssertionError("live stdout was not persisted before the process exited")

    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    stdout_text = (base / "stdout.txt").read_text(encoding="utf-8")
    stderr_text = (base / "stderr.txt").read_text(encoding="utf-8")
    transcript_text = (base / "transcript.md").read_text(encoding="utf-8")

    assert worker.is_alive()
    assert session["status"] == "running"
    assert session["pid"] is not None
    assert session["exit_code"] is None
    assert "SUMMARY: live start" in stdout_text
    assert "live stderr" in stderr_text
    assert "SUMMARY: live start" in transcript_text
    assert report["status"] == "running"
    assert report["summary"] == "VERDICT: PASS"

    worker.join(timeout=5)
    assert not error_holder
    assert "result" in result_holder
    assert result_holder["result"].ref.status == "completed"

    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "completed"
    assert session["exit_code"] == 0
    assert report["summary"] == "VERDICT: PASS"
    assert report["files_changed"] == []
    assert (
        (base / "stdout.txt")
        .read_text(encoding="utf-8")
        .startswith("VERDICT: PASS\nSUMMARY: live start\n")
    )
    assert (base / "stderr.txt").read_text(encoding="utf-8") == "live stderr\n"


def test_subagent_manager_kills_stale_live_process_using_stdout_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(subagent_inactivity_timeout_seconds=0.1))
    task = create_task(tmp_path, title="Kill stale live subagent")
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
            on_started=None,
            on_update=None,
            inactivity_timeout_seconds=None,
        ) -> CLIExecutionResult:
            del prompt, model, max_turns
            assert on_started is not None
            assert on_update is not None
            on_started(6161)
            first = CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="VERDICT: PASS\nSUMMARY: partial",
                stderr="",
                pid=6161,
            )
            on_update(first)
            stdout_path = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe" / "stdout.txt"
            stale_at = time.time() - 5
            os.utime(stdout_path, (stale_at, stale_at))
            on_update(
                CLIExecutionResult(
                    adapter="codex",
                    argv=("codex", "exec"),
                    cwd=cwd,
                    exit_code=0,
                    stdout="VERDICT: PASS\nSUMMARY: partial",
                    stderr="heartbeat only",
                    pid=6161,
                )
            )
            raise AssertionError("expected stale timeout to interrupt live execution")

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    killed_pids: list[int] = []

    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: FakeStreamingEngine())
    monkeypatch.setattr(
        "litehive.subagents._execution.os.kill", lambda pid, sig: killed_pids.append(pid)
    )

    result = manager.run(task, role="swe", engine_name="codex", prompt="stream it")

    assert killed_pids == [6161]
    assert result.ref.status == "failed"
    assert result.exit_code == 124
    assert result.failure == EngineFailure(
        kind="retryable_execution_error",
        reason="transient timeout",
        classification="timeout",
    )
    assert "litehive killed stale subagent after 0.1s without new stdout" in result.transcript
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "failed"
    assert session["exit_code"] == 124


def test_subagent_manager_avoids_existing_folder_collisions_for_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Avoid subagent folder collisions")
    manager = SubagentManager(tmp_path)

    stale_base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    stale_base.mkdir(parents=True, exist_ok=False)
    task.subagents.append(
        SubagentRef(
            id="SA-0001",
            role="swe",
            engine="codex",
            status="failed",
            path="subagents/SA-0001-swe",
        )
    )
    save_task(tmp_path, task)

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
            base = task_dir(tmp_path, task) / "subagents" / "SA-0002-swe"
            assert base.exists()
            assert not (task_dir(tmp_path, task) / "subagents" / "SA-0003-swe").exists()
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="VERDICT: PASS\nSUMMARY: retry folder allocated safely\nFILES_CHANGED:\nTESTS_ADDED: 0\nTESTS_PASSING: 0\nWARNINGS:\n",
                stderr="",
                pid=7171,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="retry safely")

    assert result.ref.id == "SA-0002"
    assert result.ref.path == "subagents/SA-0002-swe"
    assert (task_dir(tmp_path, task) / "subagents" / "SA-0002-swe").exists()


def test_subagent_streaming_pid_persists_before_first_live_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Persist silent streaming pid")
    manager = SubagentManager(tmp_path)

    class SilentStreamingEngine:
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
            on_started=None,
            on_update=None,
            inactivity_timeout_seconds=None,
        ) -> CLIExecutionResult:
            assert max_turns is None
            assert on_started is not None
            on_started(6161)

            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
            assert session["status"] == "running"
            assert session["pid"] == 6161
            assert session["exit_code"] is None
            assert (base / "transcript.md").read_text(encoding="utf-8") == ""
            assert (base / "stdout.txt").read_text(encoding="utf-8") == ""
            assert (base / "stderr.txt").read_text(encoding="utf-8") == ""

            refreshed = get_task(tmp_path, task.id)
            assert refreshed is not None
            assert refreshed.runtime.active_subagent is not None
            assert refreshed.runtime.active_subagent.pid == 6161

            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="VERDICT: PASS\nSUMMARY: silent streaming complete\n",
                stderr="",
                pid=6161,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr(
        "litehive.subagents._execution.get_engine", lambda _: SilentStreamingEngine()
    )

    result = manager.run(task, role="swe", engine_name="codex", prompt="stream it silently")

    assert result.ref.status == "completed"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "completed"
    assert session["pid"] == 6161
    assert session["exit_code"] == 0

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.active_subagent is None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.pid == 6161


def test_subagent_artifacts_capture_sandbox_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
                    )
                },
            )
        ),
    )
    task = create_task(tmp_path, title="Persist sandbox metadata")
    manager = SubagentManager(tmp_path)
    calls: dict[str, object] = {}

    class FakePopen:
        def __init__(self, cmd, cwd, env, stdout, stderr, text, stdin=None):  # type: ignore[no-untyped-def]
            calls["cmd"] = cmd
            calls["cwd"] = cwd
            calls["env"] = env
            self.pid = 7272
            self.returncode = 0
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            os.write(
                stdout_write,
                (
                    "VERDICT: PASS\nSUMMARY: sandboxed execution\nFILES_CHANGED:\n"
                    "- litehive/sandbox.py\nTESTS_ADDED: 1\nTESTS_PASSING: 1\nWARNINGS:\n"
                ).encode("utf-8"),
            )
            os.close(stdout_write)
            os.close(stderr_write)
            self.stdout = os.fdopen(stdout_read, "rb")
            self.stderr = os.fdopen(stderr_read, "rb")

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def wait(self):  # type: ignore[no-untyped-def]
            return self.returncode

    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "litehive.subagents._execution._supports_live_on_started", lambda engine: False
    )
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert result.execution is not None
    assert result.execution.sandboxed is True
    assert "sandbox[" in result.execution.sandbox_summary
    assert "--env OPENAI_API_KEY=secret" in " ".join(calls["cmd"])
    assert "ANTHROPIC_API_KEY" not in " ".join(calls["cmd"])

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    assert session["sandboxed"] is True
    assert session["sandbox"].startswith("sandbox[")

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.sandboxed is True
    assert refreshed.runtime.last_subagent.sandbox_summary.startswith("sandbox[")


def test_subagent_artifacts_capture_structured_resource_limit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            process_profile="rust",
            subagent_resource_limits=SubagentResourceLimitsConfig(
                enabled=True,
                memory_mb=4096,
                cpu_count=2.0,
                process_limit=256,
            ),
        ),
    )
    task = create_task(tmp_path, title="Persist resource limit event")
    manager = SubagentManager(tmp_path)

    class FakePopen:
        def __init__(self, cmd, cwd, env, stdout, stderr, text, stdin=None):  # type: ignore[no-untyped-def]
            self.pid = 8181
            self.returncode = 137
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            os.write(stdout_write, b"native build aborted")
            os.write(stderr_write, b"OOMKilled: container exceeded memory limit")
            os.close(stdout_write)
            os.close(stderr_write)
            self.stdout = os.fdopen(stdout_read, "rb")
            self.stderr = os.fdopen(stderr_read, "rb")

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def wait(self):  # type: ignore[no-untyped-def]
            return self.returncode

    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "litehive.subagents._execution._supports_live_on_started", lambda engine: False
    )

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert result.failure is not None
    assert result.failure.kind == "resource_limit"
    assert result.failure.resource_limit_event is not None
    assert result.failure.resource_limit_event.resource == "memory"
    assert result.failure.resource_limit_event.memory_mb == 4096

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["resource_control"]["memory_mb"] == 4096
    assert session["resource_control"]["cpu_count"] == 2.0
    assert session["resource_control"]["process_limit"] == 256
    assert session["resource_limit_event"]["resource"] == "memory"
    assert report["resource_control"]["enabled"] is True
    assert report["resource_control"]["runtime"] == "docker"
    assert report["resource_limit_event"]["reason"] == "memory limit exceeded (OOM)"

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.resource_limit_event is not None
    assert (
        refreshed.runtime.last_subagent.resource_limit_event.reason == "memory limit exceeded (OOM)"
    )


def test_subagent_manager_marks_signal_terminated_execution_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Halted subagent execution")
    manager = SubagentManager(tmp_path)

    class InterruptedEngine:
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
            assert max_turns is None
            if on_started is not None:
                on_started(7171)
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=130,
                stdout="Execution interrupted by user",
                stderr="received SIGINT",
                pid=7171,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: InterruptedEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="resume safely")

    assert result.ref.status == "interrupted"
    assert result.failure is not None
    assert result.failure.kind == "execution_interrupted"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "interrupted"
    assert session["pid"] == 7171
    assert session["interruption_reason"] == "execution interrupted"
    assert report["status"] == "interrupted"
    assert report["interruption_reason"] == "execution interrupted"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.status == "interrupted"
    assert refreshed.runtime.last_subagent.pid == 7171
    assert refreshed.runtime.last_subagent.interruption_reason == "execution interrupted"


def test_subagent_manager_uses_inherited_run_live_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback usage-limit task")
    manager = SubagentManager(tmp_path)
    engine = get_engine("codex")

    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: engine)
    monkeypatch.setattr(engine, "is_available", lambda: True)

    calls: list[str] = []

    def fail_run(*args, **kwargs) -> CLIExecutionResult:  # type: ignore[no-untyped-def]
        raise AssertionError("run should not be used when run_live is available")

    def fake_run_live(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
        on_update=None,
        inactivity_timeout_seconds=None,
    ) -> CLIExecutionResult:
        assert max_turns is None
        calls.append("run_live")
        assert on_started is not None
        on_started(4242)
        assert on_update is not None
        on_update(
            CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later.",
                pid=4242,
            )
        )
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
            pid=4242,
        )

    monkeypatch.setattr("litehive.engines.base.ExternalCLIAdapter.run", fail_run)
    monkeypatch.setattr("litehive.engines.base.ExternalCLIAdapter.run_live", fake_run_live)

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert calls == ["run_live"]
    assert result.failure == EngineFailure(kind="execution_limit", reason="usage limit reached")


def test_subagent_manager_prefers_instance_run_override_over_inherited_run_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback usage-limit task")
    manager = SubagentManager(tmp_path)
    engine = get_engine("codex")

    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: engine)
    monkeypatch.setattr(engine, "is_available", lambda: True)

    calls: list[str] = []

    def fake_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        calls.append("run")
        assert on_started is not None
        on_started(4242)
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
            pid=4242,
        )

    def fail_run_live(*args, **kwargs) -> CLIExecutionResult:  # type: ignore[no-untyped-def]
        raise AssertionError("run_live should not be used when only run is overridden")

    monkeypatch.setattr(engine, "run", fake_run)
    monkeypatch.setattr("litehive.engines.base.ExternalCLIAdapter.run_live", fail_run_live)

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert calls == ["run"]
    assert result.failure == EngineFailure(kind="execution_limit", reason="usage limit reached")


def test_subagent_manager_prefers_class_run_override_over_inherited_run_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback usage-limit task")
    manager = SubagentManager(tmp_path)

    calls: list[str] = []

    class RunOnlyEngine(ExternalCLIAdapter):
        def __init__(self) -> None:
            super().__init__(
                name="codex",
                binary="codex",
                capabilities=AdapterCapabilities(available=True),
            )

        def is_available(self) -> bool:
            return True

        def build_command(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            resume_session_id: str | None = None,
        ) -> list[str]:
            raise AssertionError("build_command should not be used in this regression test")

        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            resume_session_id: str | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            del prompt, model, max_turns, resume_session_id
            calls.append("run")
            assert on_started is not None
            on_started(4343)
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later.",
                pid=4343,
            )

    engine = RunOnlyEngine()

    def fail_run_live(*args, **kwargs) -> CLIExecutionResult:  # type: ignore[no-untyped-def]
        raise AssertionError("run_live should not be used when only run is overridden")

    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: engine)
    monkeypatch.setattr("litehive.engines.base.ExternalCLIAdapter.run_live", fail_run_live)

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


def test_subagent_prunes_superseded_raw_artifacts_and_compresses_latest_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Prune superseded artifacts")
    manager = SubagentManager(tmp_path)
    monkeypatch.setattr("litehive.subagents._execution._COMPRESS_STREAM_ARTIFACT_MIN_BYTES", 1)
    monkeypatch.setattr("litehive.subagents._execution._COMPRESS_TEXT_ARTIFACT_MIN_BYTES", 1)

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def __init__(self) -> None:
            self.calls = 0

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
            del prompt, model, max_turns
            self.calls += 1
            if on_started is not None:
                on_started(5000 + self.calls)
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout=f"VERDICT: PASS\nSUMMARY: artifact pass {self.calls}\n",
                stderr=f"stderr {self.calls}\n",
                pid=5000 + self.calls,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    engine = FakeEngine()
    monkeypatch.setattr("litehive.subagents._execution.get_engine", lambda _: engine)

    manager.run(task, role="swe", engine_name="codex", prompt="first")
    manager.run(task, role="qa", engine_name="codex", prompt="second")

    first_base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    second_base = task_dir(tmp_path, task) / "subagents" / "SA-0002-qa"

    assert (first_base / "session.yaml").exists()
    assert (first_base / "report.yaml").exists()
    assert not (first_base / "prompt.txt").exists()
    assert not (first_base / "transcript.md").exists()
    assert not (first_base / "transcript.md.gz").exists()
    assert not (first_base / "stdout.txt").exists()
    assert not (first_base / "stdout.txt.gz").exists()
    assert not (first_base / "stderr.txt").exists()
    assert not (first_base / "stderr.txt.gz").exists()

    assert (second_base / "transcript.md.gz").exists()
    assert (second_base / "stdout.txt.gz").exists()
    assert (second_base / "stderr.txt.gz").exists()
