"""Tests for the `litehive pipeline journal` CLI command.

Uses Typer's CliRunner against the full app since the command is a
thin wrapper around SqliteJournal + SqlitePersistence.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.domain.recovery import (
    FailureFingerprint,
    RecoveryDisposition,
    RecoveryOutcome,
    RecoveryTrigger,
    TriggerEventKind,
)
from litehive.domain.lifecycle_deltas import StateDelta
from litehive.lifecycle.events import CleanState, Reject
from litehive.lifecycle.journal import SqliteJournal
from litehive.workspace import Workspace
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.lifecycle.types import PipelineMode
from litehive.config.workspace import ensure_workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ensure_workspace(tmp_path)
    return tmp_path


def _seed_journal(workspace: Path, task_id: str) -> None:
    store = SqlitePersistence(Workspace.from_path(workspace))
    state = store.initialize(task_id, pipeline_mode=PipelineMode.FULL)
    state.active_recovery_trigger = RecoveryTrigger(
        origin_stage="grooming",
        trigger_event_kind=TriggerEventKind.REJECT,
        failure_fingerprint=FailureFingerprint(
            fingerprint="agent:blank task",
            classification="agent_reject",
        ),
        source="agent",
        message="blank task",
    )
    state.recovery_history.append(
        RecoveryOutcome(
            trigger=RecoveryTrigger(
                origin_stage="implementing",
                trigger_event_kind=TriggerEventKind.CRASH,
                failure_fingerprint=FailureFingerprint(
                    fingerprint="RuntimeError:boom",
                    classification="RuntimeError",
                ),
                message="boom",
            ),
            recovery_verdict="resume",
            disposition=RecoveryDisposition.RESUMED,
        )
    )
    state.stage = "recovering"
    store.save(state)

    journal = SqliteJournal(Workspace.from_path(workspace))
    journal.task_started(task_id, "ready")
    journal.transition(
        task_id=task_id,
        from_stage="ready",
        event=CleanState(),
        to_stage="worktree_sync",
        rule_description="ready → worktree_sync",
        delta=StateDelta(),
    )
    journal.transition(
        task_id=task_id,
        from_stage="grooming",
        event=Reject(source="agent", reason="blank task"),
        to_stage="recovering",
        rule_description="grooming reject → recovering",
        delta=StateDelta(set_active_recovery_trigger=state.active_recovery_trigger),
    )


def test_pipeline_journal_prints_state_and_transitions(workspace: Path) -> None:
    _seed_journal(workspace, "T-0099")

    result = CliRunner().invoke(app, ["pipeline", "journal", "T-0099", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "task: T-0099" in result.output
    assert "stage: recovering" in result.output
    assert "active_recovery_trigger:" in result.output
    assert "origin_stage=grooming" in result.output
    assert "recovery_history:" in result.output
    assert "ready" in result.output
    assert "worktree_sync" in result.output
    assert "grooming reject → recovering" in result.output


def test_pipeline_journal_unknown_task_returns_error(workspace: Path) -> None:
    result = CliRunner().invoke(app, ["pipeline", "journal", "T-9999", "--workspace", str(workspace)])
    assert result.exit_code == 1
    assert "no pipeline state row for task T-9999" in result.output
