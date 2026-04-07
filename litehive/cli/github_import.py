"""Import GitHub issues as litehive tasks."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from litehive.config import ensure_workspace
from litehive.models import GitHubOrigin
from litehive.tasks import (
    WorkspaceConflictError,
    create_task,
    list_tasks,
)

# Label → task_type mapping
LABEL_TASK_TYPE_MAP: dict[str, str] = {
    "bug": "bugfix",
    "documentation": "docs",
    "enhancement": "refactor",
    "question": "research",
}

# Label → priority mapping
LABEL_PRIORITY_MAP: dict[str, str] = {
    "priority: critical": "critical",
    "priority: high": "high",
    "priority: low": "low",
    "critical": "critical",
    "high priority": "high",
    "low priority": "low",
}


def _run_gh(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command and return the result."""
    try:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise GhNotFoundError(
            "gh CLI not found. Install it from https://cli.github.com/ and run 'gh auth login'."
        )


class GhNotFoundError(RuntimeError):
    pass


class GhAuthError(RuntimeError):
    pass


def _check_gh_auth() -> None:
    """Verify gh CLI is installed and authenticated."""
    result = _run_gh("auth", "status")
    if result.returncode != 0:
        raise GhAuthError(
            "gh CLI is not authenticated. Run 'gh auth login' first.\n"
            + result.stderr.strip()
        )


def _detect_repo(workspace: Path) -> str:
    """Infer owner/repo from git remote origin."""
    result = subprocess.run(
        ["git", "-C", str(workspace), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            "Could not detect GitHub repo from git remote. Use --repo owner/repo."
        )
    url = result.stdout.strip()
    # Handle SSH: git@github.com:owner/repo.git
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    # Handle HTTPS: https://github.com/owner/repo.git
    m = re.match(r"https?://github\.com/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    raise ValueError(f"Could not parse GitHub repo from remote URL: {url}")


def _parse_issue_ref(ref: str) -> tuple[str | None, int]:
    """Parse a GitHub issue URL or number into (repo_or_none, issue_number)."""
    # Full URL: https://github.com/owner/repo/issues/42
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/issues/(\d+)", ref)
    if m:
        return m.group(1), int(m.group(2))
    # Just a number
    if ref.isdigit():
        return None, int(ref)
    raise ValueError(
        f"Cannot parse issue reference '{ref}'. "
        "Use a GitHub URL (https://github.com/owner/repo/issues/42) or an issue number."
    )


def _fetch_issue(repo: str, number: int) -> dict:
    """Fetch a single issue via gh CLI."""
    result = _run_gh(
        "issue", "view", str(number),
        "--repo", repo,
        "--json", "number,title,body,labels,url",
    )
    if result.returncode != 0:
        raise ValueError(f"Failed to fetch issue #{number} from {repo}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _fetch_open_issues(repo: str) -> list[dict]:
    """Fetch all open issues via gh CLI."""
    result = _run_gh(
        "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,body,labels,url",
        "--limit", "500",
    )
    if result.returncode != 0:
        raise ValueError(f"Failed to list issues from {repo}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _map_labels(labels: list[dict]) -> tuple[str | None, str | None]:
    """Map GitHub labels to task_type and priority."""
    task_type = None
    priority = None
    for label in labels:
        name = label.get("name", "").lower()
        if name in LABEL_TASK_TYPE_MAP and task_type is None:
            task_type = LABEL_TASK_TYPE_MAP[name]
        if name in LABEL_PRIORITY_MAP and priority is None:
            priority = LABEL_PRIORITY_MAP[name]
    return task_type, priority


def _find_existing_task_for_issue(
    workspace: Path, repo: str, issue_number: int
) -> str | None:
    """Return existing task ID if this issue was already imported, else None."""
    for task in list_tasks(workspace, include_runtime=False):
        origin = task.github_origin
        if origin is not None and origin.repo == repo and origin.issue_number == issue_number:
            return task.id
    return None


def _import_single_issue(
    workspace: Path, repo: str, issue: dict
) -> tuple[str, str | None]:
    """Import one issue. Returns (action, task_id_or_none).

    action is "created", "skipped", or "error".
    """
    number = issue["number"]
    existing = _find_existing_task_for_issue(workspace, repo, number)
    if existing is not None:
        return "skipped", existing

    labels = issue.get("labels", [])
    task_type, priority = _map_labels(labels)
    body = issue.get("body") or ""

    origin = GitHubOrigin(
        repo=repo,
        issue_number=number,
        issue_url=issue.get("url", f"https://github.com/{repo}/issues/{number}"),
    )

    try:
        task = create_task(
            workspace,
            title=issue["title"],
            goal=body,
            task_type=task_type,
            priority=priority,
            github_origin=origin,
            auto_commit=True,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"  error importing #{number}: {exc}")
        return "error", None

    return "created", task.id


# ── CLI command handlers ─────────────────────────────────────────────


def _cmd_import_issue(args) -> int:
    """Import a single GitHub issue as a litehive task."""
    ensure_workspace(args.workspace)
    try:
        _check_gh_auth()
    except (GhNotFoundError, GhAuthError) as exc:
        print(f"import-issue failed: {exc}")
        return 1

    try:
        parsed_repo, number = _parse_issue_ref(args.issue_ref)
        repo = args.repo or parsed_repo
        if repo is None:
            repo = _detect_repo(args.workspace)

        issue = _fetch_issue(repo, number)
        action, task_id = _import_single_issue(args.workspace, repo, issue)

        if action == "skipped":
            print(f"Issue #{number} already imported as {task_id} — skipped.")
            return 0
        if action == "error":
            return 1
        print(f"Created task {task_id} from {repo}#{number}")
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"import-issue failed: {exc}")
        return 1


def _cmd_import_issues(args) -> int:
    """Import all open GitHub issues as litehive tasks."""
    ensure_workspace(args.workspace)
    try:
        _check_gh_auth()
    except (GhNotFoundError, GhAuthError) as exc:
        print(f"import-issues failed: {exc}")
        return 1

    try:
        repo = args.repo
        if repo is None:
            repo = _detect_repo(args.workspace)

        issues = _fetch_open_issues(repo)
        created = 0
        skipped = 0
        errors = 0

        for issue in issues:
            action, task_id = _import_single_issue(args.workspace, repo, issue)
            number = issue["number"]
            if action == "created":
                print(f"  created {task_id} from #{number}: {issue['title']}")
                created += 1
            elif action == "skipped":
                print(f"  skipped #{number} (already {task_id})")
                skipped += 1
            else:
                errors += 1

        print(f"\nImported {created} issue(s), skipped {skipped}, errors {errors}.")
        return 0 if errors == 0 else 1
    except (ValueError, RuntimeError) as exc:
        print(f"import-issues failed: {exc}")
        return 1
