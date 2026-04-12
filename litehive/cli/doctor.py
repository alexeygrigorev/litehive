from litehive.config import ensure_workspace
from litehive.recovery import apply_doctor_fixes, scan_workspace_doctor


def _render_finding(prefix: str, finding) -> None:
    print(f"{prefix}: {finding.code} {finding.summary} fix={finding.fix_command}")


def cmd_doctor(workspace, fix: bool = False) -> int:
    ensure_workspace(workspace)
    root = workspace.resolve()
    if fix:
        result = apply_doctor_fixes(root)
        if result.stale_unmerged_worktrees_removed:
            print(
                "doctor_cleanup: "
                f"stale_unmerged_worktrees_removed={result.stale_unmerged_worktrees_removed}"
            )
        for finding in result.fixed:
            _render_finding("fixed", finding)
        for finding in result.remaining:
            _render_finding("finding", finding)
        print(f"doctor_summary: fixed={len(result.fixed)} remaining={len(result.remaining)}")
        return 0 if not result.remaining else 1

    report = scan_workspace_doctor(root)
    if report.stale_unmerged_worktrees_removed:
        print(
            "doctor_cleanup: "
            f"stale_unmerged_worktrees_removed={report.stale_unmerged_worktrees_removed}"
        )
    if not report.findings:
        print(f"doctor: clean workspace={root}")
        return 0
    for finding in report.findings:
        _render_finding("finding", finding)
    print(f"doctor_summary: findings={len(report.findings)}")
    return 1
