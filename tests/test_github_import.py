"""Tests for GitHub issue import commands."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litehive.config import ensure_workspace
from litehive.models import GitHubOrigin
from litehive.tasks import create_task, list_tasks
from litehive.cli.github_import import (
    GhAuthError,
    GhNotFoundError,
    cmd_import_issue,
    cmd_import_issues,
    check_gh_auth,
    detect_repo_from_remote,
    find_existing_task_for_issue,
    import_single_issue,
    map_labels,
    parse_issue_ref,
    run_gh,
)
from tests.workspace_helpers import _init_git_repo


@pytest.fixture
def workspace(tmp_path):
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    return tmp_path


# ── parse_issue_ref ──────────────────────────────────────────────────────


def test_parse_issue_ref_url():
    repo, num = parse_issue_ref("https://github.com/owner/repo/issues/42")
    assert repo == "owner/repo"
    assert num == 42


def test_parse_issue_ref_number():
    repo, num = parse_issue_ref("7")
    assert repo is None
    assert num == 7


def test_parse_issue_ref_invalid():
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_issue_ref("not-a-ref")


# ── map_labels ───────────────────────────────────────────────────────────


def test_map_labels_bug():
    task_type, priority = map_labels(["bug"])
    assert task_type == "bugfix"
    assert priority is None


def test_map_labels_documentation():
    task_type, priority = map_labels(["documentation"])
    assert task_type == "docs"


def test_map_labels_enhancement():
    task_type, priority = map_labels(["enhancement"])
    assert task_type == "refactor"


def test_map_labels_question():
    task_type, priority = map_labels(["question"])
    assert task_type == "research"


def test_map_labels_priority_high():
    task_type, priority = map_labels(["priority: high"])
    assert task_type is None
    assert priority == "high"


def test_map_labels_priority_critical():
    _, priority = map_labels(["priority: critical"])
    assert priority == "critical"


def test_map_labels_priority_low():
    _, priority = map_labels(["priority: low"])
    assert priority == "low"


def test_map_labels_combined():
    task_type, priority = map_labels(["bug", "priority: high"])
    assert task_type == "bugfix"
    assert priority == "high"


def test_map_labels_combined_first_wins():
    task_type, priority = map_labels(["documentation", "priority: critical", "enhancement"])
    assert task_type == "docs"
    assert priority == "critical"


def test_map_labels_empty():
    task_type, priority = map_labels([])
    assert task_type is None
    assert priority is None


def test_map_labels_unknown():
    task_type, priority = map_labels(["wontfix", "good first issue"])
    assert task_type is None
    assert priority is None


# ── detect_repo_from_remote ──────────────────────────────────────────────


def test_detect_repo_ssh():
    mock_result = MagicMock(returncode=0, stdout="git@github.com:owner/repo.git\n")
    with patch("litehive.cli.github_import.subprocess.run", return_value=mock_result):
        assert detect_repo_from_remote() == "owner/repo"


def test_detect_repo_https():
    mock_result = MagicMock(returncode=0, stdout="https://github.com/owner/repo.git\n")
    with patch("litehive.cli.github_import.subprocess.run", return_value=mock_result):
        assert detect_repo_from_remote() == "owner/repo"


def test_detect_repo_https_no_git_suffix():
    mock_result = MagicMock(returncode=0, stdout="https://github.com/owner/repo\n")
    with patch("litehive.cli.github_import.subprocess.run", return_value=mock_result):
        assert detect_repo_from_remote() == "owner/repo"


def test_detect_repo_no_remote():
    mock_result = MagicMock(returncode=1, stdout="", stderr="fatal: No such remote")
    with patch("litehive.cli.github_import.subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="No git remote"):
            detect_repo_from_remote()


# ── run_gh / check_gh_auth ──────────────────────────────────────────────


def test_run_gh_missing():
    with patch(
        "litehive.cli.github_import.subprocess.run",
        side_effect=FileNotFoundError("No such file"),
    ):
        with pytest.raises(GhNotFoundError, match="gh CLI not found"):
            run_gh(["auth", "status"])


def test_run_gh_failure():
    mock_result = MagicMock(returncode=1, stderr="some error")
    with patch("litehive.cli.github_import.subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="gh command failed"):
            run_gh(["issue", "view", "1"])


def test_check_gh_auth_failure():
    mock_result = MagicMock(returncode=1, stderr="not logged in")
    with patch("litehive.cli.github_import.subprocess.run", return_value=mock_result):
        with pytest.raises(GhAuthError, match="not authenticated"):
            check_gh_auth()


# ── duplicate detection ──────────────────────────────────────────────────


def test_find_existing_task_none(workspace):
    assert find_existing_task_for_issue(workspace, "owner/repo", 1) is None


def test_find_existing_task_found(workspace):
    task = create_task(
        workspace,
        title="Test task",
        github_origin=GitHubOrigin(
            repo="owner/repo",
            issue_number=42,
            issue_url="https://github.com/owner/repo/issues/42",
        ),
        auto_commit=False,
    )
    assert find_existing_task_for_issue(workspace, "owner/repo", 42) == task.id


def test_find_existing_task_different_repo(workspace):
    create_task(
        workspace,
        title="Test task",
        github_origin=GitHubOrigin(
            repo="owner/repo",
            issue_number=42,
            issue_url="https://github.com/owner/repo/issues/42",
        ),
        auto_commit=False,
    )
    assert find_existing_task_for_issue(workspace, "other/repo", 42) is None


def test_github_origin_persisted_in_yaml(workspace):
    task = create_task(
        workspace,
        title="Test persistence",
        github_origin=GitHubOrigin(
            repo="owner/repo",
            issue_number=99,
            issue_url="https://github.com/owner/repo/issues/99",
        ),
        auto_commit=False,
    )
    task_dir = workspace / ".litehive" / "tasks" / f"{task.id}-{task.slug}"
    data = yaml.safe_load((task_dir / "task.yaml").read_text())
    assert data["github_origin"]["repo"] == "owner/repo"
    assert data["github_origin"]["issue_number"] == 99


# ── import_single_issue ─────────────────────────────────────────────────


def _mock_gh_issue_view(issue_data):
    """Create a mock for subprocess.run that returns issue JSON for gh issue view."""
    auth_result = MagicMock(returncode=0, stdout="Logged in\n")
    view_result = MagicMock(returncode=0, stdout=json.dumps(issue_data))

    def side_effect(cmd, **kwargs):
        if cmd[0] == "gh":
            if "auth" in cmd:
                return auth_result
            return view_result
        # git commands pass through
        raise FileNotFoundError("not mocked")

    return side_effect


def test_import_single_issue_creates_task(workspace):
    issue_data = {
        "number": 10,
        "title": "Fix the login page",
        "body": "The login page is broken when...",
        "labels": [{"name": "bug"}],
        "url": "https://github.com/owner/repo/issues/10",
    }
    with patch("litehive.cli.github_import.run_gh", return_value=json.dumps(issue_data)):
        with patch("litehive.cli.github_import.fetch_issue", return_value=issue_data):
            task_id, status = import_single_issue(workspace, "owner/repo", 10)

    assert status == "created"
    tasks = list_tasks(workspace, include_runtime=False)
    task = next(t for t in tasks if t.id == task_id)
    assert task.title == "Fix the login page"
    assert task.goal == "The login page is broken when..."
    assert task.task_type == "bugfix"
    assert task.github_origin is not None
    assert task.github_origin.repo == "owner/repo"
    assert task.github_origin.issue_number == 10


def test_import_duplicate_skipped(workspace):
    create_task(
        workspace,
        title="Existing task",
        github_origin=GitHubOrigin(
            repo="owner/repo",
            issue_number=10,
            issue_url="https://github.com/owner/repo/issues/10",
        ),
        auto_commit=False,
    )
    task_id, status = import_single_issue(workspace, "owner/repo", 10)
    assert status == "skipped"


def test_cmd_import_issue_from_url(workspace, capsys):
    issue_data = {
        "number": 5,
        "title": "Add dark mode",
        "body": "We need dark mode support",
        "labels": [{"name": "enhancement"}],
        "url": "https://github.com/owner/repo/issues/5",
    }
    with patch("litehive.cli.github_import.check_gh_auth"):
        with patch("litehive.cli.github_import.fetch_issue", return_value=issue_data):
            ret = cmd_import_issue(
                workspace, "https://github.com/owner/repo/issues/5"
            )

    assert ret == 0
    out = capsys.readouterr().out
    assert "Created task" in out
    assert "owner/repo#5" in out


def test_cmd_import_issue_from_number(workspace, capsys):
    issue_data = {
        "number": 3,
        "title": "Fix typo",
        "body": "There is a typo in the README",
        "labels": [],
        "url": "https://github.com/owner/repo/issues/3",
    }
    with patch("litehive.cli.github_import.check_gh_auth"):
        with patch("litehive.cli.github_import.fetch_issue", return_value=issue_data):
            ret = cmd_import_issue(workspace, "3", repo="owner/repo")

    assert ret == 0
    out = capsys.readouterr().out
    assert "Created task" in out


def test_cmd_import_issue_duplicate(workspace, capsys):
    create_task(
        workspace,
        title="Already here",
        github_origin=GitHubOrigin(
            repo="owner/repo",
            issue_number=5,
            issue_url="https://github.com/owner/repo/issues/5",
        ),
        auto_commit=False,
    )
    with patch("litehive.cli.github_import.check_gh_auth"):
        with patch("litehive.cli.github_import.fetch_issue") as mock_fetch:
            ret = cmd_import_issue(
                workspace, "https://github.com/owner/repo/issues/5"
            )

    assert ret == 0
    out = capsys.readouterr().out
    assert "already imported" in out
    assert "skipping" in out
    mock_fetch.assert_not_called()  # should skip before fetching


def test_cmd_import_issue_gh_missing(workspace, capsys):
    with patch(
        "litehive.cli.github_import.check_gh_auth",
        side_effect=GhNotFoundError("gh CLI not found. Install it from https://cli.github.com/"),
    ):
        ret = cmd_import_issue(workspace, "1", repo="owner/repo")

    assert ret == 1
    out = capsys.readouterr().out
    assert "gh CLI not found" in out


def test_cmd_import_issues_bulk(workspace, capsys):
    issues = [
        {
            "number": 1,
            "title": "Bug report",
            "body": "Something is wrong",
            "labels": [{"name": "bug"}],
            "url": "https://github.com/owner/repo/issues/1",
        },
        {
            "number": 2,
            "title": "Feature request",
            "body": "Add this feature",
            "labels": [{"name": "enhancement"}, {"name": "priority: high"}],
            "url": "https://github.com/owner/repo/issues/2",
        },
        {
            "number": 3,
            "title": "Docs update",
            "body": "Update the docs",
            "labels": [{"name": "documentation"}],
            "url": "https://github.com/owner/repo/issues/3",
        },
    ]
    with patch("litehive.cli.github_import.check_gh_auth"):
        with patch("litehive.cli.github_import.fetch_open_issues", return_value=issues):
            ret = cmd_import_issues(workspace, repo="owner/repo")

    assert ret == 0
    out = capsys.readouterr().out
    assert "Imported 3 issue(s)" in out

    tasks = list_tasks(workspace, include_runtime=False)
    assert len(tasks) == 3
    bug_task = next(t for t in tasks if t.title == "Bug report")
    assert bug_task.task_type == "bugfix"
    feature_task = next(t for t in tasks if t.title == "Feature request")
    assert feature_task.task_type == "refactor"
    assert feature_task.priority == "high"
    docs_task = next(t for t in tasks if t.title == "Docs update")
    assert docs_task.task_type == "docs"


def test_cmd_import_issues_skips_existing(workspace, capsys):
    create_task(
        workspace,
        title="Existing",
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
            "title": "Bug report",
            "body": "Something is wrong",
            "labels": [{"name": "bug"}],
            "url": "https://github.com/owner/repo/issues/1",
        },
        {
            "number": 2,
            "title": "New issue",
            "body": "New",
            "labels": [],
            "url": "https://github.com/owner/repo/issues/2",
        },
    ]
    with patch("litehive.cli.github_import.check_gh_auth"):
        with patch("litehive.cli.github_import.fetch_open_issues", return_value=issues):
            ret = cmd_import_issues(workspace, repo="owner/repo")

    assert ret == 0
    out = capsys.readouterr().out
    assert "Imported 1 issue(s)" in out
    assert "skipped 1" in out


def test_cmd_import_issues_gh_not_authenticated(workspace, capsys):
    with patch(
        "litehive.cli.github_import.check_gh_auth",
        side_effect=GhAuthError("gh CLI is not authenticated. Run 'gh auth login' first."),
    ):
        ret = cmd_import_issues(workspace, repo="owner/repo")

    assert ret == 1
    out = capsys.readouterr().out
    assert "not authenticated" in out
