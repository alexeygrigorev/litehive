"""Import GitHub issues as litehive tasks via the gh CLI."""

import json
import re
import subprocess
from pathlib import Path

from litehive.models import GitHubOrigin
from litehive.tasks.crud import create_task, list_tasks

# ── Label mapping ────────────────────────────────────────────────────────

LABEL_TO_TASK_TYPE: dict[str, str] = {
    "bug": "bugfix",
    "documentation": "docs",
    "enhancement": "refactor",
    "question": "research",
}

LABEL_TO_PRIORITY: dict[str, str] = {
    "priority: critical": "critical",
    "priority: high": "high",
    "priority: low": "low",
    "critical": "critical",
    "high priority": "high",
    "low priority": "low",
}


def map_labels(labels: list[str]) -> tuple[str | None, str | None]:
    """Map GitHub issue labels to task_type and priority."""
    task_type = None
    priority = None
    for label in labels:
        low = label.lower()
        if low in LABEL_TO_TASK_TYPE and task_type is None:
            task_type = LABEL_TO_TASK_TYPE[low]
        if low in LABEL_TO_PRIORITY and priority is None:
            priority = LABEL_TO_PRIORITY[low]
    return task_type, priority


# ── gh CLI helpers ───────────────────────────────────────────────────────


class GhNotFoundError(RuntimeError):
    pass


class GhAuthError(RuntimeError):
    pass


def run_gh(args: list[str], cwd: Path | None = None) -> str:
    """Run a gh CLI command and return stdout. Raises on missing gh or failure."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=30,
        )
    except FileNotFoundError:
        raise GhNotFoundError(
            "gh CLI not found. Install it from https://cli.github.com/ and run 'gh auth login'."
        )
    if proc.returncode != 0:
        raise RuntimeError(f"gh command failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def check_gh_auth(cwd: Path | None = None) -> None:
    """Verify gh is installed and authenticated."""
    try:
        run_gh(["auth", "status"], cwd=cwd)
    except GhNotFoundError:
        raise
    except RuntimeError:
        raise GhAuthError(
            "gh CLI is not authenticated. Run 'gh auth login' first."
        )


def detect_repo_from_remote(cwd: Path | None = None) -> str:
    """Infer owner/repo from git remote origin."""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
    except FileNotFoundError:
        raise RuntimeError("git not found")
    if proc.returncode != 0:
        raise RuntimeError("No git remote 'origin' found. Use --repo owner/repo.")
    url = proc.stdout.strip()
    # SSH: git@github.com:owner/repo.git
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    # HTTPS: https://github.com/owner/repo.git
    m = re.match(r"https?://github\.com/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    raise RuntimeError(f"Cannot parse owner/repo from remote URL: {url}. Use --repo owner/repo.")


def parse_issue_ref(ref: str) -> tuple[str | None, int]:
    """Parse a GitHub issue URL or number into (repo_or_none, issue_number)."""
    # Full URL: https://github.com/owner/repo/issues/42
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/issues/(\d+)", ref)
    if m:
        return m.group(1), int(m.group(2))
    # Just a number
    try:
        return None, int(ref)
    except ValueError:
        raise ValueError(f"Cannot parse issue reference: {ref!r}. Use a URL or issue number.")


# ── Duplicate detection ──────────────────────────────────────────────────


def find_existing_task_for_issue(root: Path, repo: str, issue_number: int) -> str | None:
    """Return the task ID if an issue has already been imported, else None."""
    for task in list_tasks(root, include_runtime=False):
        if (
            task.github_origin is not None
            and task.github_origin.repo == repo
            and task.github_origin.issue_number == issue_number
        ):
            return task.id
    return None


# ── Core import logic ────────────────────────────────────────────────────


def fetch_issue(repo: str, issue_number: int, cwd: Path | None = None) -> dict:
    """Fetch a single issue via gh."""
    raw = run_gh(
        ["issue", "view", str(issue_number), "--repo", repo, "--json",
         "number,title,body,labels,url"],
        cwd=cwd,
    )
    return json.loads(raw)


def fetch_open_issues(repo: str, cwd: Path | None = None) -> list[dict]:
    """Fetch all open issues via gh."""
    raw = run_gh(
        ["issue", "list", "--repo", repo, "--state", "open", "--json",
         "number,title,body,labels,url", "--limit", "1000"],
        cwd=cwd,
    )
    return json.loads(raw)


def import_single_issue(root: Path, repo: str, issue_number: int, cwd: Path | None = None) -> tuple[str, str]:
    """Import one issue. Returns (task_id, status) where status is 'created' or 'skipped'."""
    existing = find_existing_task_for_issue(root, repo, issue_number)
    if existing is not None:
        return existing, "skipped"

    data = fetch_issue(repo, issue_number, cwd=cwd)
    labels = [lbl["name"] for lbl in data.get("labels", [])]
    task_type, priority = map_labels(labels)

    task = create_task(
        root,
        title=data["title"],
        goal=data.get("body") or "",
        task_type=task_type,
        priority=priority,
        github_origin=GitHubOrigin(
            repo=repo,
            issue_number=data["number"],
            issue_url=data["url"],
        ),
        auto_commit=True,
    )
    return task.id, "created"
