import pytest
from tests.workspace_helpers import (
    AdapterCapabilities,
    CLIExecutionResult,
    EngineUsageWindow,
    LitehiveConfig,
    LiveEvent,
    LiveTimeline,
    Path,
    StageReport,
    SubagentManager,
    SubagentRef,
    _cmd_update,
    _completed_subagent_result,
    argparse,
    create_task,
    ensure_workspace,
    extract_engine_timeline,
    get_engine,
    get_task,
    load_config,
    mark_subagent_started,
    pytest,
    render_task_summary,
    resolve_engine_name,
    run_next_task,
    save_task,
    task_dir,
    tasks_module,
    yaml,
)

def test_claude_live_progress_report_uses_adapter_summary_for_restart_snippet(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    task = create_task(tmp_path, title="Claude live restart summary", auto_commit=False)
    manager = SubagentManager(tmp_path)

    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="claude",
        status="running",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    mark_subagent_started(tmp_path, task, ref)
    save_task(tmp_path, task)

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    base.mkdir(parents=True, exist_ok=False)
    manager._write_session_start(task, base, ref, "stream partial Claude output")

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"VERDICT: PASS\\n"}}',
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"SUMMARY: partial Claude output\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"FILES_CHANGED:\\n- litehive/engines.py\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"TESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n"}}',
            ]
        ),
        stderr="",
        pid=4242,
    )

    manager._write_session_progress(task, base, ref, "stream partial Claude output", execution)

    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert report["status"] == "running"
    # The session snapshot summary is now the reject reason from stage_report_from_subagent
    # because no CLI verdict was submitted — adapter-level STAGE_RESULT parsing is gone.
    assert "did not submit verdict" in report["summary"]
    assert report["files_changed"] == []

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    interrupted = tasks_module.mark_interrupted_subagent(
        tmp_path,
        refreshed,
        reason="runner interrupted before subagent completion",
        stage="implementing",
    )

    assert interrupted is not None
    # transcript_snippet now reflects the reject reason from stage_report_from_subagent
    # (no CLI verdict submitted) — STAGE_RESULT parsing is gone.
    assert "did not submit verdict" in interrupted.transcript_snippet

    resumed_report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert resumed_report["status"] == "interrupted"
    assert resumed_report["resume_stage"] == "implementing"


def test_resolve_engine_name_rejects_claude_when_not_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    task = create_task(tmp_path, title="Claude task")
    config = load_config(tmp_path)
    assert resolve_engine_name(task, config) == "claude"


def test_resolve_engine_name_rejects_default_claude_when_not_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    config = load_config(tmp_path)
    assert config.default_engine == "claude"

    task = create_task(tmp_path, title="Claude default task")
    assert resolve_engine_name(task, config) == "claude"


def test_resolve_engine_name_allows_claude_when_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    task = create_task(tmp_path, title="Claude task")
    config = load_config(tmp_path)
    assert resolve_engine_name(task, config) == "claude"


def test_claude_is_not_default_engine() -> None:
    config = LitehiveConfig()
    assert config.default_engine != "claude"


def test_claude_config_defaults_to_sonnet() -> None:
    config = LitehiveConfig()
    assert config.claude_model == "claude-sonnet-4-20250514"
    assert config.claude_max_turns == 100


def test_claude_not_in_engine_preference() -> None:
    config = LitehiveConfig()
    assert "claude" not in config.engine_preference, "claude should not be in default engine_preference"


def test_update_command_rejects_removed_claude_engine_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    ensure_workspace(tmp_path, LitehiveConfig())
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

    assert exit_code == 1
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert "update failed: no changes requested" in output


def test_update_command_rejects_removed_goz_engine_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Goz task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="goz",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert "update failed: no changes requested" in output


def test_configure_persists_claude_settings(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw["claude_model"] = "claude-sonnet-4-20250514"
    raw["claude_max_turns"] = 20
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.claude_model == "claude-sonnet-4-20250514"
    assert config.claude_max_turns == 20


def test_configure_updates_existing_workspace_process_profile(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(process_profile="generic"))
    raw = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw["process_profile"] = "python"
    raw["claude_max_turns"] = 20
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )

    config = load_config(tmp_path)
    assert config.process_profile == "python"
    assert config.claude_max_turns == 20


def test_claude_model_resolved_from_workspace_defaults() -> None:
    from litehive.config.engine_models import workspace_model_for_engine

    config = LitehiveConfig(claude_model="claude-sonnet-4-20250514")
    assert workspace_model_for_engine(config, "claude") == "claude-sonnet-4-20250514"

    config_default = LitehiveConfig()
    assert workspace_model_for_engine(config_default, "claude") == "claude-sonnet-4-20250514"


def test_subagent_writes_timeline_on_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Timeline finish test")
    manager = SubagentManager(tmp_path)

    stdout = '{"type":"text","part":{"text":"VERDICT: PASS\\nSUMMARY: done"}}\n'

    class FakeEngine:
        name = "opencode"
        binary = "opencode"

        def is_available(self) -> bool:
            return True

        def run(self, prompt, cwd, model=None, *, max_turns=None, on_started=None):
            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout=stdout,
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution):
            return execution.stdout

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="opencode", prompt="do it")

    assert result.ref.status == "completed"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    timeline_path = base / "timeline.yaml"
    assert timeline_path.exists()
    timeline_data = yaml.safe_load(timeline_path.read_text(encoding="utf-8"))
    assert timeline_data["engine"] == "opencode"
    assert timeline_data["task_id"] == task.id
    assert timeline_data["subagent_id"] == "SA-0001"
    assert len(timeline_data["events"]) == 1
    assert timeline_data["events"][0]["kind"] == "message"


def test_subagent_writes_timeline_during_live_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Timeline live progress test")
    manager = SubagentManager(tmp_path)

    partial_stdout = '{"type":"text","part":{"text":"partial output"}}\n'

    class FakeStreamingEngine:
        name = "opencode"
        binary = "opencode"

        def is_available(self) -> bool:
            return True

        def run_live(
            self,
            prompt,
            cwd,
            model=None,
            *,
            max_turns=None,
            on_update=None,
            inactivity_timeout_seconds=0,
            **kwargs,
        ):
            first = CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout=partial_stdout,
                stderr="",
                pid=5151,
            )
            if on_update is not None:
                on_update(first)

            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            timeline_path = base / "timeline.yaml"
            assert timeline_path.exists()
            timeline_data = yaml.safe_load(timeline_path.read_text(encoding="utf-8"))
            assert timeline_data["engine"] == "opencode"
            assert timeline_data["task_id"] == task.id
            assert len(timeline_data["events"]) == 1

            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout=partial_stdout + '{"type":"step_finish","part":{"tokens":{"total":50}}}\n',
                stderr="",
                pid=5151,
            )

        def render_transcript(self, execution):
            return execution.transcript

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeStreamingEngine())

    result = manager.run(task, role="swe", engine_name="opencode", prompt="stream it")
    assert result.ref.status == "completed"

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    timeline_path = base / "timeline.yaml"
    assert timeline_path.exists()
    timeline_data = yaml.safe_load(timeline_path.read_text(encoding="utf-8"))
    assert len(timeline_data["events"]) == 2
    assert timeline_data["event_counts"] == {"message": 1, "usage": 1}


def test_subagent_skips_timeline_when_no_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="No timeline test")
    manager = SubagentManager(tmp_path)

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(self, prompt, cwd, model=None, *, max_turns=None, on_started=None):
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="plain text output with no jsonl",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution):
            return execution.stdout

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="no events")
    assert result.ref.status == "completed"

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    assert not (base / "timeline.yaml").exists()


@pytest.mark.skip(reason="v1 TaskExecutionRunner deleted; test needs rewrite for v2 pipeline")
def test_runner_persists_duration_seconds_in_report_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Duration tracking task")

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

        pytest.skip("v1 executor deleted")
    runner = TaskExecutionRunner(tmp_path, executor)
    runner.run(task)

    reports_dir = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    for report_path in reports_dir.glob("*.yaml"):
        data = yaml.safe_load(report_path.read_text(encoding="utf-8"))
        assert "duration_seconds" in data, f"duration_seconds missing in {report_path.name}"
        assert isinstance(data["duration_seconds"], int)
        assert data["duration_seconds"] >= 0


def test_render_task_summary_includes_estimate_velocity_and_eta(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Estimate demo task")

    # Manually create a report with a known duration to seed the velocity estimate.
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
            }
        ),
        encoding="utf-8",
    )

    task.pipeline_status = "implementing"
    lines = render_task_summary(task, active=True, root=tmp_path)
    combined = "\n".join(lines)
    assert "stage_estimate=" in combined
    assert "velocity=" in combined
    assert "eta=" in combined
