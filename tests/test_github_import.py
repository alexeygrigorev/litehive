"""Tests for GitHub issue import commands."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litehive.config import ensure_workspace
from litehive.models import GitHubOrigin
from litehive.tasks import create_task, list_tasks, get_task

from litehive.cli.github_import import (
    GhAuthError,
    GhNotFoundError,
    _check_gh_auth,
    _cmd_import_issue,
    _cmd_import_issues,
    _detect_repo,
    _fetch_issue,
    _fetch_open_issues,
    _find_existing_task_for_issue,
    _import_single_issue,
    _map_labels,
    _parse_issue_ref,
    _run_gh,
)


# ── Unit tests for helpers ───────────────────────────────────────────


def test_parse_issue_ref_url():
    repo, num = _parse_issue_ref("https://github.com/owner/repo/issues/42")
    assert repo == "owner/repo"
    assert num == 42


def test_parse_issue_ref_number():
    repo, num = _parse_issue_ref("7")
    assert repo is None
    assert num == 7


def test_parse_issue_ref_invalid():
    with pytest.raises(ValueError, match="Cannot parse issue reference"):
        _parse_issue_ref("not-a-ref")


def test_map_labels_bug():
    task_type, priority = _map_labels([{"name": "bug"}])
    assert task_type == "bugfix"
    assert priority is None


def test_map_labels_priority():
    task_type, priority = _map_labels([{"name": "priority: high"}])
    assert task_type is None
    assert priority == "high"


def test_map_labels_combined():
    task_type, priority = _map_labels([
        {"name": "documentation"},
        {"name": "priority: critical"},
        {"name": "enhancement"},  # should not override docs
    ])
    assert task_type == "docs"
    assert priority == "critical"


def test_map_labels_empty():
    task_type, priority = _map_labels([])
    assert task_type is None
    assert priority is None


# ── gh CLI wrapper tests ─────────────────────────────────────────────


def test_run_gh_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GhNotFoundError, match="gh CLI not found"):
        _run_gh("auth", "status")


def test_check_gh_auth_failure(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="not logged in")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GhAuthError, match="not authenticated"):
        _check_gh_auth()


def test_detect_repo_ssh(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="git@github.com:alexeygrigorev/litehive.git\n", stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_repo(Path("/tmp")) == "alexeygrigorev/litehive"


def test_detect_repo_https(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="https://github.com/owner/repo.git\n", stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_repo(Path("/tmp")) == "owner/repo"


def test_detect_repo_no_remote(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="fatal: no remote",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="Could not detect"):
        _detect_repo(Path("/tmp"))


# ── Integration tests with workspace ─────────────────────────────────


@pytest.fixture
def ws(tmp_path):
    """Create an initialized litehive workspace."""
    ensure_workspace(tmp_path)
    return tmp_path


SAMPLE_ISSUE = {
    "number": 42,
    "title": "Fix login timeout",
    "body": "The login page times out after 10 seconds.",
    "labels": [{"name": "bug"}, {"name": "priority: high"}],
    "url": "https://github.com/owner/repo/issues/42",
}


def test_import_single_issue_creates_task(ws):
    action, task_id = _import_single_issue(ws, "owner/repo", SAMPLE_ISSUE)
    assert action == "created"
    assert task_id is not None

    task = get_task(ws, task_id)
    assert task.title == "Fix login timeout"
    assert "login page times out" in task.goal
    assert task.task_type == "bugfix"
    assert task.priority == "high"
    assert task.github_origin is not None
    assert task.github_origin.repo == "owner/repo"
    assert task.github_origin.issue_number == 42
    assert task.github_origin.issue_url == "https://github.com/owner/repo/issues/42"


def test_import_duplicate_skipped(ws):
    action1, tid1 = _import_single_issue(ws, "owner/repo", SAMPLE_ISSUE)
    assert action1 == "created"

    action2, tid2 = _import_single_issue(ws, "owner/repo", SAMPLE_ISSUE)
    assert action2 == "skipped"
    assert tid2 == tid1


def test_find_existing_task_for_issue(ws):
    assert _find_existing_task_for_issue(ws, "owner/repo", 42) is None

    create_task(
        ws,
        title="Test task",
        github_origin=GitHubOrigin(
            repo="owner/repo",
            issue_number=42,
            issue_url="https://github.com/owner/repo/issues/42",
        ),
        auto_commit=False,
    )
    result = _find_existing_task_for_issue(ws, "owner/repo", 42)
    assert result is not None
    assert result.startswith("T-")


def test_find_existing_task_different_repo(ws):
    create_task(
        ws,
        title="Test task",
        github_origin=GitHubOrigin(
            repo="other/repo",
            issue_number=42,
            issue_url="https://github.com/other/repo/issues/42",
        ),
        auto_commit=False,
    )
    assert _find_existing_task_for_issue(ws, "owner/repo", 42) is None


def test_github_origin_persisted_in_yaml(ws):
    task = create_task(
        ws,
        title="Test persistence",
        github_origin=GitHubOrigin(
            repo="owner/repo",
            issue_number=99,
            issue_url="https://github.com/owner/repo/issues/99",
        ),
        auto_commit=False,
    )
    task_dir = ws / ".litehive" / "tasks" / f"{task.id}-{task.slug}"
    data = yaml.safe_load((task_dir / "task.yaml").read_text())
    assert data["github_origin"]["repo"] == "owner/repo"
    assert data["github_origin"]["issue_number"] == 99


# ── CLI command tests ────────────────────────────────────────────────


def _make_gh_mock(monkeypatch, issues=None, single_issue=None, auth_ok=True):
    """Set up subprocess.run mock for gh CLI calls."""
    def fake_run(cmd, **kwargs):
        if not isinstance(cmd, list):
            cmd = list(cmd)
        prog = cmd[0] if cmd else ""
        if prog == "gh":
            subcmd = cmd[1] if len(cmd) > 1 else ""
            if subcmd == "auth":
                rc = 0 if auth_ok else 1
                return subprocess.CompletedProcess(
                    args=cmd, returncode=rc, stdout="", stderr="" if auth_ok else "not logged in",
                )
            if subcmd == "issue":
                action = cmd[2] if len(cmd) > 2 else ""
                if action == "view" and single_issue is not None:
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0, stdout=json.dumps(single_issue), stderr="",
                    )
                if action == "list" and issues is not None:
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0, stdout=json.dumps(issues), stderr="",
                    )
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="unknown gh command",
            )
        if prog == "git":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="https://github.com/owner/repo.git\n", stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_cmd_import_issue_from_url(ws, monkeypatch):
    _make_gh_mock(monkeypatch, single_issue=SAMPLE_ISSUE)
    args = argparse.Namespace(
        workspace=ws,
        issue_ref="https://github.com/owner/repo/issues/42",
        repo=None,
    )
    rc = _cmd_import_issue(args)
    assert rc == 0

    tasks = list_tasks(ws, include_runtime=False)
    assert len(tasks) == 1
    assert tasks[0].title == "Fix login timeout"
    assert tasks[0].github_origin.issue_number == 42


def test_cmd_import_issue_from_number(ws, monkeypatch):
    _make_gh_mock(monkeypatch, single_issue=SAMPLE_ISSUE)
    args = argparse.Namespace(
        workspace=ws,
        issue_ref="42",
        repo="owner/repo",
    )
    rc = _cmd_import_issue(args)
    assert rc == 0


def test_cmd_import_issue_duplicate(ws, monkeypatch):
    _make_gh_mock(monkeypatch, single_issue=SAMPLE_ISSUE)
    args = argparse.Namespace(
        workspace=ws,
        issue_ref="https://github.com/owner/repo/issues/42",
        repo=None,
    )
    assert _cmd_import_issue(args) == 0
    assert _cmd_import_issue(args) == 0  # duplicate is skipped, still rc=0

    tasks = list_tasks(ws, include_runtime=False)
    assert len(tasks) == 1


def test_cmd_import_issue_gh_missing(ws, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", fake_run)
    args = argparse.Namespace(
        workspace=ws,
        issue_ref="42",
        repo="owner/repo",
    )
    rc = _cmd_import_issue(args)
    assert rc == 1


def test_cmd_import_issues_bulk(ws, monkeypatch):
    issues = [
        {
            "number": 1,
            "title": "First issue",
            "body": "Body one",
            "labels": [{"name": "documentation"}],
            "url": "https://github.com/owner/repo/issues/1",
        },
        {
            "number": 2,
            "title": "Second issue",
            "body": "Body two",
            "labels": [{"name": "enhancement"}, {"name": "priority: low"}],
            "url": "https://github.com/owner/repo/issues/2",
        },
        {
            "number": 3,
            "title": "Third issue",
            "body": "",
            "labels": [],
            "url": "https://github.com/owner/repo/issues/3",
        },
    ]
    _make_gh_mock(monkeypatch, issues=issues)
    args = argparse.Namespace(workspace=ws, repo="owner/repo")
    rc = _cmd_import_issues(args)
    assert rc == 0

    tasks = list_tasks(ws, include_runtime=False)
    assert len(tasks) == 3

    by_title = {t.title: t for t in tasks}
    assert by_title["First issue"].task_type == "docs"
    assert by_title["Second issue"].task_type == "refactor"
    assert by_title["Second issue"].priority == "low"
    assert by_title["Third issue"].task_type is None
    assert by_title["Third issue"].goal == ""


def test_cmd_import_issues_skips_existing(ws, monkeypatch):
    # Pre-import one issue
    create_task(
        ws,
        title="Already here",
        github_origin=GitHubOrigin(
            repo="owner/repo",
            issue_number=1,
            issue_url="https://github.com/owner/repo/issues/1",
        ),
        auto_commit=False,
    )
    issues = [
        {
            "number": 1,
            "title": "Already here",
            "body": "Body",
            "labels": [],
            "url": "https://github.com/owner/repo/issues/1",
        },
        {
            "number": 2,
            "title": "New issue",
            "body": "New body",
            "labels": [],
            "url": "https://github.com/owner/repo/issues/2",
        },
    ]
    _make_gh_mock(monkeypatch, issues=issues)
    args = argparse.Namespace(workspace=ws, repo="owner/repo")
    rc = _cmd_import_issues(args)
    assert rc == 0

    tasks = list_tasks(ws, include_runtime=False)
    assert len(tasks) == 2  # 1 pre-existing + 1 new


def test_cmd_import_issues_gh_not_authenticated(ws, monkeypatch):
    _make_gh_mock(monkeypatch, auth_ok=False)
    args = argparse.Namespace(workspace=ws, repo="owner/repo")
    rc = _cmd_import_issues(args)
    assert rc == 1
