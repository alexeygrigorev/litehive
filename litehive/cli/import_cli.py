from pathlib import Path
from typing import Annotated
import json
import re
import subprocess
import sys

import typer

from litehive.cli.common import WorkspaceOption, choice, make_typer, require_subcommand
from litehive.cli.parse import parse_acceptance_criteria
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task, discard_created_task, list_tasks
from litehive.tasks.constants import VALID_TASK_PRIORITIES
from litehive.tasks.paths import task_dir

app = make_typer(invoke_without_command=True)


class GhNotFoundError(RuntimeError):
    pass


class GhAuthError(RuntimeError):
    pass


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
}


@app.callback()
def import_group(ctx: typer.Context, workspace: WorkspaceOption = Path.cwd()) -> None:
    del workspace
    if ctx.invoked_subcommand is not None:
        return
    require_subcommand(ctx)


def map_labels(labels: list[str]) -> tuple[str | None, str | None]:
    task_type = None
    priority = None
    for label in labels:
        lowered = label.lower()
        if lowered in LABEL_TO_TASK_TYPE and task_type is None:
            task_type = LABEL_TO_TASK_TYPE[lowered]
        if lowered in LABEL_TO_PRIORITY and priority is None:
            priority = LABEL_TO_PRIORITY[lowered]
    return task_type, priority


def run_gh(args: list[str], cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GhNotFoundError(
            "gh CLI not found. Install it from https://cli.github.com/ and run 'gh auth login'."
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(f"gh command failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def check_gh_auth(cwd: Path | None = None) -> None:
    try:
        run_gh(["auth", "status"], cwd=cwd)
    except GhNotFoundError:
        raise
    except RuntimeError as exc:
        raise GhAuthError("gh CLI is not authenticated. Run 'gh auth login' first.") from exc


def detect_repo_from_remote(cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git not found") from exc
    if proc.returncode != 0:
        raise RuntimeError("No git remote 'origin' found. Use --repo owner/repo.")
    url = proc.stdout.strip()
    match = re.match(r"git@github\.com:(.+?)(?:\.git)?$", url)
    if match:
        return match.group(1)
    match = re.match(r"https?://github\.com/(.+?)(?:\.git)?$", url)
    if match:
        return match.group(1)
    raise RuntimeError(f"Cannot parse owner/repo from remote URL: {url}. Use --repo owner/repo.")


def parse_issue_ref(ref: str) -> tuple[str | None, int]:
    match = re.match(r"https?://github\.com/([^/]+/[^/]+)/issues/(\d+)", ref)
    if match:
        return match.group(1), int(match.group(2))
    try:
        return None, int(ref)
    except ValueError as exc:
        raise ValueError(f"Cannot parse issue reference: {ref!r}. Use a URL or issue number.") from exc


def _issue_marker(repo: str, issue_number: int) -> str:
    return f"Imported from GitHub issue: {repo}#{issue_number}"


def find_existing_task_for_issue(root: Path, repo: str, issue_number: int) -> str | None:
    marker = _issue_marker(repo, issue_number)
    for task in list_tasks(root, include_runtime=False, strict=False):
        if marker in task.goal:
            return task.id
    return None


def fetch_issue(repo: str, issue_number: int, cwd: Path | None = None) -> dict:
    raw = run_gh(
        ["issue", "view", str(issue_number), "--repo", repo, "--json", "number,title,body,labels,url"],
        cwd=cwd,
    )
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("gh issue view returned invalid JSON")
    return data


def fetch_open_issues(repo: str, cwd: Path | None = None) -> list[dict]:
    raw = run_gh(
        ["issue", "list", "--repo", repo, "--state", "open", "--json", "number,title,body,labels,url", "--limit", "1000"],
        cwd=cwd,
    )
    data = json.loads(raw)
    if not isinstance(data, list):
        raise RuntimeError("gh issue list returned invalid JSON")
    return data


def _render_github_goal(repo: str, issue_data: dict) -> str:
    issue_number = issue_data["number"]
    issue_url = issue_data["url"]
    body = (issue_data.get("body") or "").strip()
    lines = [
        _issue_marker(repo, issue_number),
        f"Source URL: {issue_url}",
    ]
    if body:
        lines.extend(["", body])
    return "\n".join(lines)


def _write_brief(root: Path, task, contents: str) -> None:
    (task_dir(root, task) / "brief.md").write_text(contents, encoding="utf-8")


def import_single_issue(root: Path, repo: str, issue_number: int, cwd: Path | None = None) -> tuple[str, str]:
    existing = find_existing_task_for_issue(root, repo, issue_number)
    if existing is not None:
        return existing, "skipped"
    issue_data = fetch_issue(repo, issue_number, cwd=cwd)
    labels = [label["name"] for label in issue_data.get("labels", [])]
    task_type, priority = map_labels(labels)
    task = create_task(
        root,
        title=issue_data["title"],
        goal=_render_github_goal(repo, issue_data),
        task_type=task_type,
        priority=priority,
    )
    brief_lines = [
        f"# {issue_data['title']}",
        "",
        f"- Source: {issue_data['url']}",
        f"- Repository: {repo}",
        f"- Issue: #{issue_number}",
    ]
    if labels:
        brief_lines.append(f"- Labels: {', '.join(labels)}")
    body = (issue_data.get("body") or "").strip()
    if body:
        brief_lines.extend(["", body])
    try:
        _write_brief(root, task, "\n".join(brief_lines).strip() + "\n")
    except OSError:
        discard_created_task(root, task.id)
        raise
    return task.id, "created"


def _derive_spec_title(spec_text: str) -> str:
    for line in spec_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        normalized = stripped.lstrip("#").strip()
        return normalized[:120] or "Imported spec"
    return "Imported spec"


@app.command("spec", help="Create a task from a spec file or stdin")
def spec(
    file: Annotated[Path | None, typer.Argument(help="Spec file; reads stdin when omitted")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    title: Annotated[str | None, typer.Option(help="Override the derived task title")] = None,
    priority: Annotated[
        str | None, typer.Option(click_type=choice(VALID_TASK_PRIORITIES), help="Set task priority")
    ] = None,
    acceptance_criteria: Annotated[
        list[str] | None, typer.Option(help="Add acceptance criteria; repeat for multiple")
    ] = None,
) -> int:
    ensure_workspace(workspace)
    try:
        acceptance_criteria = parse_acceptance_criteria(acceptance_criteria)
        spec_text = file.read_text(encoding="utf-8") if file is not None else sys.stdin.read()
    except OSError as exc:
        print(f"import spec failed: {exc}")
        return 1
    except ValueError as exc:
        print(f"import spec failed: {exc}")
        return 1

    spec_text = spec_text.strip()
    if not spec_text:
        print("import spec failed: empty spec")
        return 1

    task = create_task(
        workspace,
        title=title or _derive_spec_title(spec_text),
        goal=spec_text,
        priority=priority,
        acceptance_criteria=None if acceptance_criteria is ... else acceptance_criteria,
    )
    try:
        _write_brief(workspace, task, spec_text + "\n")
    except OSError as exc:
        discard_created_task(workspace, task.id)
        print(f"import spec failed: {exc}")
        return 1

    print(f"Created task {task.id}: {task.title}")
    print(f"brief: {task_dir(workspace, task) / 'brief.md'}")
    return 0


@app.command("github", help="Import GitHub issues as Litehive tasks")
def github(
    issue_ref: Annotated[str | None, typer.Argument(help="GitHub issue URL or issue number")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    repo: Annotated[str | None, typer.Option(help="GitHub repo as owner/repo")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Import all open issues")] = False,
) -> int:
    ensure_workspace(workspace)
    if all_:
        try:
            check_gh_auth(cwd=workspace)
            resolved_repo = repo or detect_repo_from_remote(cwd=workspace)
            issues = fetch_open_issues(resolved_repo, cwd=workspace)
        except (GhNotFoundError, GhAuthError, RuntimeError) as exc:
            print(f"import github failed: {exc}")
            return 1
        created = 0
        skipped = 0
        for issue_data in issues:
            issue_number = issue_data["number"]
            if find_existing_task_for_issue(workspace, resolved_repo, issue_number) is not None:
                skipped += 1
                continue
            try:
                _, status = import_single_issue(workspace, resolved_repo, issue_number, cwd=workspace)
            except (RuntimeError, OSError, ValueError) as exc:
                print(f"import github failed: {exc}")
                return 1
            if status == "created":
                created += 1
        print(f"Imported {created} issue(s), skipped {skipped} already-tracked issue(s).")
        return 0

    if not issue_ref:
        print("import github failed: provide an issue reference or use --all")
        return 1

    try:
        check_gh_auth(cwd=workspace)
        repo_from_ref, issue_number = parse_issue_ref(issue_ref)
        resolved_repo = repo or repo_from_ref or detect_repo_from_remote(cwd=workspace)
        task_id, status = import_single_issue(workspace, resolved_repo, issue_number, cwd=workspace)
    except (GhNotFoundError, GhAuthError, RuntimeError, OSError, ValueError) as exc:
        print(f"import github failed: {exc}")
        return 1

    if status == "skipped":
        print(f"Issue #{issue_number} already imported as {task_id}, skipping.")
        return 0
    print(f"Created task {task_id} from {resolved_repo}#{issue_number}")
    return 0
