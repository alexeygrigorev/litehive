"""Tests for the litehive logs command."""

import gzip

from tests.workspace_helpers import (
    Path,
    RuntimeSubagentState,
    SubagentRef,
    _cmd_logs,
    argparse,
    create_task,
    ensure_workspace,
    pytest,
    save_task,
    save_task_runtime,
    task_dir,
    threading,
    time,
)


def _ns(
    workspace: Path,
    task_id: str | None = None,
    *,
    daemon: bool = False,
    agent: bool = False,
    all_flag: bool = False,
    follow: bool = False,
):
    return argparse.Namespace(
        workspace=workspace,
        task_id=task_id,
        daemon=daemon,
        agent=agent,
        all=all_flag,
        follow=follow,
    )


def _make_task_with_subagent(tmp_path: Path, *, active: bool = False):
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Logs test task", auto_commit=False)
    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running" if active else "completed",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    if active:
        task.runtime.active_subagent = RuntimeSubagentState(
            id="SA-0001",
            role="swe",
            engine="codex",
            status="running",
            path="subagents/SA-0001-swe",
            pid=42,
            started_at="2026-04-09T10:00:00Z",
            updated_at="2026-04-09T10:00:01Z",
        )
    else:
        task.runtime.last_subagent = RuntimeSubagentState(
            id="SA-0001",
            role="swe",
            engine="codex",
            status="completed",
            path="subagents/SA-0001-swe",
            started_at="2026-04-09T10:00:00Z",
            updated_at="2026-04-09T10:00:05Z",
            completed_at="2026-04-09T10:00:05Z",
            exit_code=0,
        )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    base.mkdir(parents=True, exist_ok=True)
    return task, base


def test_logs_defaults_to_latest_daemon_run_tail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    from litehive.config.paths import workspace_logs_dir

    log_dir = workspace_logs_dir(tmp_path) / "run-all" / "20260409T120000Z"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "0001-run.log").write_text("line one\nline two\n", encoding="utf-8")

    exit_code = _cmd_logs(_ns(tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"daemon log: {log_dir / '0001-run.log'}" in output
    assert "line one" in output
    assert "line two" in output



def test_logs_daemon_lists_latest_sessions_with_outcomes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    from litehive.config.paths import workspace_logs_dir

    logs_root = workspace_logs_dir(tmp_path) / "run-all"
    logs_root.mkdir(parents=True, exist_ok=True)
    for index in range(6):
        name = f"20260409T12000{index}Z"
        directory = logs_root / name
        directory.mkdir()
        (directory / "0001-post-status.log").write_text(
            f"active_task_id: None\nqueued_tasks: 0\npool_stop_reason: reason-{index}\n",
            encoding="utf-8",
        )

    exit_code = _cmd_logs(_ns(tmp_path, daemon=True))
    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if line.strip()]

    assert exit_code == 0
    assert len(lines) == 5
    assert lines[0].startswith("20260409T120005Z")
    assert "outcome=reason-5" in lines[0]
    assert "20260409T120000Z" not in output


def test_logs_task_journal_prints_journal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Journal task", auto_commit=False)
    journal = task_dir(tmp_path, task) / "journal.md"
    journal.write_text("# journal\nentry\n", encoding="utf-8")

    exit_code = _cmd_logs(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "# journal" in output
    assert "entry" in output


def test_logs_agent_prefers_live_stdout_for_active_subagent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task, base = _make_task_with_subagent(tmp_path, active=True)
    (base / "transcript.md").write_text("live transcript\n", encoding="utf-8")
    (base / "stdout.log").write_text("live stdout\n", encoding="utf-8")
    (base / "stdout.txt").write_text("stale snapshot\n", encoding="utf-8")

    exit_code = _cmd_logs(_ns(tmp_path, task.id, agent=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "transcript:" in output
    assert "live transcript" in output
    assert "stdout:" in output
    assert "live stdout" in output
    assert "stale snapshot" not in output


def test_logs_agent_reads_compressed_completed_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task, base = _make_task_with_subagent(tmp_path, active=False)
    with gzip.open(base / "transcript.md.gz", "wt", encoding="utf-8") as handle:
        handle.write("final transcript\n")
    with gzip.open(base / "stdout.txt.gz", "wt", encoding="utf-8") as handle:
        handle.write("final stdout\n")

    exit_code = _cmd_logs(_ns(tmp_path, task.id, agent=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "final transcript" in output
    assert "final stdout" in output


def test_logs_agent_all_lists_all_subagents_with_duration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="All subagents", auto_commit=False)
    task.subagents = [
        SubagentRef(
            id="SA-0001",
            role="planner",
            engine="gemini",
            status="completed",
            path="subagents/SA-0001-planner",
        ),
        SubagentRef(
            id="SA-0002",
            role="swe",
            engine="codex",
            status="failed",
            path="subagents/SA-0002-swe",
        ),
    ]
    save_task(tmp_path, task)

    planner_dir = task_dir(tmp_path, task) / "subagents" / "SA-0001-planner"
    planner_dir.mkdir(parents=True, exist_ok=True)
    (planner_dir / "session.yaml").write_text(
        "exit_code: 0\ncreated_at: 2026-04-09T10:00:00Z\nupdated_at: 2026-04-09T10:00:03Z\n",
        encoding="utf-8",
    )
    swe_dir = task_dir(tmp_path, task) / "subagents" / "SA-0002-swe"
    swe_dir.mkdir(parents=True, exist_ok=True)
    (swe_dir / "session.yaml").write_text(
        "exit_code: 1\ncreated_at: 2026-04-09T10:00:10Z\nupdated_at: 2026-04-09T10:00:15Z\n",
        encoding="utf-8",
    )

    exit_code = _cmd_logs(_ns(tmp_path, task.id, agent=True, all_flag=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    lines = [line for line in output.splitlines() if line.strip()]
    assert lines[0].startswith("SA-0002")
    assert "role=swe" in lines[0]
    assert "engine=codex" in lines[0]
    assert "exit_code=1" in lines[0]
    assert "duration=5s" in lines[0]
    assert "duration=3s" in lines[1]


def test_logs_follow_streams_active_stdout_until_subagent_finishes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("litehive.cli.task_logs_support._FOLLOW_POLL_SECONDS", 0.01)
    task, base = _make_task_with_subagent(tmp_path, active=True)
    stdout_path = base / "stdout.log"
    stdout_path.write_text("", encoding="utf-8")

    def writer() -> None:
        time.sleep(0.02)
        stdout_path.write_text("chunk one\n", encoding="utf-8")
        time.sleep(0.02)
        stdout_path.write_text("chunk one\nchunk two\n", encoding="utf-8")
        task.runtime.active_subagent = None
        save_task_runtime(tmp_path, task)

    worker = threading.Thread(target=writer)
    worker.start()
    try:
        exit_code = _cmd_logs(_ns(tmp_path, follow=True))
    finally:
        worker.join(timeout=1)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "following: .litehive/tasks/" in output
    assert "chunk one" in output
    assert "chunk two" in output


def test_logs_follow_falls_back_to_latest_stdout_when_subagent_just_finished(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task, base = _make_task_with_subagent(tmp_path, active=False)
    (base / "stdout.log").write_text("final live output\n", encoding="utf-8")

    exit_code = _cmd_logs(_ns(tmp_path, follow=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "following: .litehive/tasks/" in output
    assert "final live output" in output
