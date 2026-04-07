"""Tests for litehive self-heal recovery path (failure_owner == litehive)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from litehive.config import LitehiveConfig, ensure_workspace
from litehive.models import StageReport, SubagentRef, TaskThreadComment
from litehive.runtime._recovery import (
    _attempt_stage_recovery,
    _classify_recovery_failure_owner,
)
from litehive.subagents import SubagentResult
from litehive.tasks import append_thread_comment, create_task, save_task, save_task_runtime


def _make_litehive_traceback(source_path: str) -> str:
    return (
        f'Traceback (most recent call last):\n'
        f'  File "{source_path}/litehive/runtime/_execution.py", line 100, in run\n'
        f'    raise RuntimeError("bug in litehive")\n'
        f'RuntimeError: bug in litehive'
    )


def _make_project_traceback(project_path: str) -> str:
    return (
        f'Traceback (most recent call last):\n'
        f'  File "{project_path}/app/main.py", line 50, in handler\n'
        f'    raise ValueError("project bug")\n'
        f'ValueError: project bug'
    )


def _fake_subagent_ref() -> SubagentRef:
    return SubagentRef(
        id="SA-9999",
        role="recovery",
        engine="codex",
        status="completed",
        path="subagents/SA-9999-recovery",
        sandboxed=False,
        sandbox_summary="host",
    )


def _load_recovery_report(root: Path, task_id: str, slug: str) -> dict[str, object]:
    report_path = root / ".litehive" / "tasks" / f"{task_id}-{slug}" / "recovery" / "recovery-001.yaml"
    return yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}


class TestClassifyFailureOwner:
    def test_litehive_source_path_match(self, tmp_path: Path) -> None:
        source = tmp_path / "litehive-src"
        source.mkdir()
        config = LitehiveConfig(litehive_source_path=str(source))
        report = StageReport(
            task_id="T-0001",
            step="implementing",
            verdict="fail",
            summary="crash",
            failure_diagnostics={"traceback": _make_litehive_traceback(str(source))},
        )
        owner, tb, src_root = _classify_recovery_failure_owner(tmp_path, report, config=config)
        assert owner == "litehive"
        assert src_root == source.resolve()
        assert "RuntimeError" in tb

    def test_project_path_match(self, tmp_path: Path) -> None:
        config = LitehiveConfig(litehive_source_path="/some/other/path")
        report = StageReport(
            task_id="T-0001",
            step="implementing",
            verdict="fail",
            summary="crash",
            failure_diagnostics={"traceback": _make_project_traceback(str(tmp_path))},
        )
        owner, tb, _ = _classify_recovery_failure_owner(tmp_path, report, config=config)
        assert owner == "project"


class TestSelfHealRecovery:
    def _setup_task(self, tmp_path: Path) -> "tuple[Path, object]":
        ensure_workspace(tmp_path)
        task = create_task(tmp_path, title="External project task")
        task.status = "in_progress"
        task.pipeline_status = "implementing"
        save_task(tmp_path, task)
        save_task_runtime(tmp_path, task)
        return tmp_path, task

    def test_self_heal_launches_agent_against_litehive_source(self, tmp_path: Path) -> None:
        root, task = self._setup_task(tmp_path)
        source_root = tmp_path / "litehive-src"
        source_root.mkdir()
        config = LitehiveConfig(litehive_source_path=str(source_root))

        failed_report = StageReport(
            task_id=task.id,
            step="implementing",
            verdict="fail",
            summary="litehive bug",
            failure_diagnostics={"traceback": _make_litehive_traceback(str(source_root))},
        )

        captured_calls: list[dict] = []
        captured_execution_root: list[Path] = []

        def fake_run(t, *, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):
            captured_calls.append({"prompt": prompt, "role": role})
            # Simulate the agent submitting a verdict via litehive report
            append_thread_comment(root, t, TaskThreadComment(
                role="recovery", step="implementing", verdict="pass",
                message="Fixed litehive bug in _execution.py",
                files_changed=["litehive/runtime/_execution.py"],
            ))
            return SubagentResult(
                ref=_fake_subagent_ref(),
                execution=None,
                transcript="fixed the bug",
                exit_code=0,
                failure=None,
            )

        def fake_manager_init(self, r, *, execution_root=None):
            self.root = r.resolve()
            self.execution_root = (execution_root or r).resolve()
            self.config = config
            self.sandbox = MagicMock()
            self._stream_offsets = {}
            self.run = fake_run
            captured_execution_root.append(self.execution_root)

        mock_subagents = MagicMock()
        mock_subagents.run = fake_run

        with patch("litehive.runtime._recovery.SubagentManager.__init__", fake_manager_init):
            result = _attempt_stage_recovery(
                root,
                root,  # execution_root (project worktree)
                task,
                "implementing",
                failed_report,
                subagents=mock_subagents,
                config=config,
                engine_name="codex",
            )

        # Verify the self-heal agent was called
        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["role"] == "recovery"
        assert "SELF-HEAL" in call["prompt"]
        assert "litehive source tree" in call["prompt"]
        assert str(source_root) in call["prompt"]
        assert "uv run pytest -q" in call["prompt"]
        assert "RuntimeError: bug in litehive" in call["prompt"]

        # Verify SubagentManager was constructed with litehive source root
        assert len(captured_execution_root) == 1
        assert captured_execution_root[0] == source_root.resolve()

        # Check recovery report
        report = _load_recovery_report(root, task.id, task.slug)
        assert report["trigger"] == "litehive_self_heal"
        assert report["failure_classification"] == "litehive"
        assert report["actions"][0]["action"] == "litehive_self_heal"

        # Verify fingerprint was recorded
        assert len(task.runtime.self_heal_traceback_fingerprints) == 1

    def test_self_heal_skips_duplicate_fingerprint(self, tmp_path: Path) -> None:
        root, task = self._setup_task(tmp_path)
        source_root = tmp_path / "litehive-src"
        source_root.mkdir()
        config = LitehiveConfig(litehive_source_path=str(source_root))

        traceback = _make_litehive_traceback(str(source_root))
        failed_report = StageReport(
            task_id=task.id,
            step="implementing",
            verdict="fail",
            summary="litehive bug",
            failure_diagnostics={"traceback": traceback},
        )

        # Pre-populate the fingerprint
        from litehive.runtime._recovery import _traceback_fingerprint
        fp = _traceback_fingerprint(traceback, "litehive bug")
        task.runtime.self_heal_traceback_fingerprints.append(fp)
        save_task_runtime(root, task)

        mock_subagents = MagicMock()

        result = _attempt_stage_recovery(
            root,
            root,
            task,
            "implementing",
            failed_report,
            subagents=mock_subagents,
            config=config,
            engine_name="codex",
        )

        # Should return None (skipped) and NOT call subagents.run
        assert result is None
        mock_subagents.run.assert_not_called()

    def test_project_failure_uses_standard_recovery_path(self, tmp_path: Path) -> None:
        root, task = self._setup_task(tmp_path)
        source_root = tmp_path / "litehive-src"
        source_root.mkdir()
        config = LitehiveConfig(litehive_source_path=str(source_root))

        failed_report = StageReport(
            task_id=task.id,
            step="implementing",
            verdict="fail",
            summary="project bug",
            failure_diagnostics={"traceback": _make_project_traceback(str(root))},
        )

        captured_calls: list[dict] = []

        def fake_run(t, *, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):
            captured_calls.append({"prompt": prompt, "role": role})
            return SubagentResult(
                ref=_fake_subagent_ref(),
                execution=None,
                transcript="fixed project issue",
                exit_code=0,
                failure=None,
            )

        mock_subagents = MagicMock()
        mock_subagents.run = fake_run

        result = _attempt_stage_recovery(
            root,
            root,
            task,
            "implementing",
            failed_report,
            subagents=mock_subagents,
            config=config,
            engine_name="codex",
        )

        assert len(captured_calls) == 1
        call = captured_calls[0]
        # Standard path: no SELF-HEAL, uses project classification
        assert "SELF-HEAL" not in call["prompt"]
        assert "project" in call["prompt"].lower()
        # No self-heal fingerprints recorded
        assert len(task.runtime.self_heal_traceback_fingerprints) == 0

    def test_self_heal_failed_records_blocker_and_fingerprint(self, tmp_path: Path) -> None:
        root, task = self._setup_task(tmp_path)
        source_root = tmp_path / "litehive-src"
        source_root.mkdir()
        config = LitehiveConfig(litehive_source_path=str(source_root))

        failed_report = StageReport(
            task_id=task.id,
            step="implementing",
            verdict="fail",
            summary="litehive crash",
            failure_diagnostics={"traceback": _make_litehive_traceback(str(source_root))},
        )

        def fake_run(t, *, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):
            return SubagentResult(
                ref=_fake_subagent_ref(),
                execution=None,
                transcript="could not fix",
                exit_code=1,
                failure=None,
            )

        def fake_manager_init(self, r, *, execution_root=None):
            self.root = r.resolve()
            self.execution_root = (execution_root or r).resolve()
            self.config = config
            self.sandbox = MagicMock()
            self._stream_offsets = {}
            self.run = fake_run

        mock_subagents = MagicMock()
        mock_subagents.run = fake_run

        with patch("litehive.runtime._recovery.SubagentManager.__init__", fake_manager_init):
            result = _attempt_stage_recovery(
                root,
                root,
                task,
                "implementing",
                failed_report,
                subagents=mock_subagents,
                config=config,
                engine_name="codex",
            )

        # Failed self-heal returns None
        assert result is None

        # Fingerprint still recorded (prevents retry)
        assert len(task.runtime.self_heal_traceback_fingerprints) == 1

        # Recovery report records the failure
        report = _load_recovery_report(root, task.id, task.slug)
        assert report["trigger"] == "litehive_self_heal"
        assert report["runnable_state"] == "blocked"
        assert report["actions"][0]["action"] == "litehive_self_heal_failed"
