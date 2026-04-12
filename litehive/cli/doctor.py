from litehive.config import ensure_workspace
from litehive.recovery import apply_doctor_fixes, scan_workspace_doctor


def _render_finding(prefix: str, finding) -> None:
    print(f"{prefix}: {finding.code} {finding.summary} fix={finding.fix_command}")


def cmd_doctor(args) -> int:
    ensure_workspace(args.workspace)
    root = args.workspace.resolve()
    if getattr(args, "fix", False):
        result = apply_doctor_fixes(root)
        for finding in result.fixed:
            _render_finding("fixed", finding)
        for finding in result.remaining:
            _render_finding("finding", finding)
        print(f"doctor_summary: fixed={len(result.fixed)} remaining={len(result.remaining)}")
        return 0 if not result.remaining else 1

    report = scan_workspace_doctor(root)
    if not report.findings:
        print(f"doctor: clean workspace={root}")
        return 0
    for finding in report.findings:
        _render_finding("finding", finding)
    print(f"doctor_summary: findings={len(report.findings)}")
    return 1
