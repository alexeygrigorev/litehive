"""Read-only diagnostics for `litehive status`.

This module is the entry point for status snapshot collection; the implementation is split across
sibling modules and re-exported here so callers keep importing from a single path:

- `status_types`     — `StatusIssue`, `StatusSnapshot`, severity literal, recovery-failure context, constants
- `status_io`        — YAML/JSON readers and small parsing helpers used by loaders and probes
- `status_loaders`   — config/state/runner/engine-monitoring loaders
- `status_probes`    — every `_probe_*` and the `probe_registry_files` public probe
- `status_rendering` — `status_has_problems` + `render_*` helpers
"""

from pathlib import Path

# Re-exported public API. Imports kept in `status_diagnostics` so existing
# `from litehive.observability.status_diagnostics import ...` callers keep working.
from litehive.observability.status_loaders import _load_runner_status_for_status  # noqa: F401
from litehive.observability.status_loaders import (
    _load_config_for_status,
    _load_engine_monitoring_for_status,
    _load_state_for_status,
)
from litehive.observability.status_probes import (
    _probe_daemon_status,
    _probe_heru_link,
    _probe_last_cycle,
    _probe_origin_divergence,
    _probe_pool_stop_reason,
    _probe_runner_state,
    _probe_task_index_references,
    _probe_task_status_damage,
    probe_registry_files,
)
from litehive.observability.status_rendering import (  # noqa: F401
    render_health_summary,
    render_issue_lines,
    render_operational_issue_lines,
    status_has_problems,
)
from litehive.observability.status_types import (  # noqa: F401
    StatusIssue,
    StatusSeverity,
    StatusSnapshot,
)

__all__ = [
    "StatusIssue",
    "StatusSeverity",
    "StatusSnapshot",
    "collect_operational_status_snapshot",
    "collect_status_snapshot",
    "probe_registry_files",
    "render_health_summary",
    "render_issue_lines",
    "render_operational_issue_lines",
    "status_has_problems",
]


def collect_status_snapshot(root: Path) -> StatusSnapshot:
    """Build the full diagnostic snapshot used by `litehive health` to surface every probe finding."""
    root = root.resolve()
    registry_issues = probe_registry_files()
    config, config_issues = _load_config_for_status(root)
    state, state_issues = _load_state_for_status(root)
    runner, runner_issue = _load_runner_status_for_status(root)
    monitoring, monitoring_issues = _load_engine_monitoring_for_status(root)
    if runner_issue is not None:
        runner_issues_list: list = [runner_issue]
    else:
        runner_issues_list = []
    issues = [
        *registry_issues,
        *config_issues,
        *state_issues,
        *runner_issues_list,
        *monitoring_issues,
        *_probe_runner_state(root, state, runner),
        *_probe_daemon_status(root),
        *_probe_last_cycle(root),
        *_probe_heru_link(root),
        *_probe_pool_stop_reason(state),
        *_probe_origin_divergence(root, state),
        *_probe_task_index_references(root, state, state_issues),
        *_probe_task_status_damage(root, state, runner, state_issues),
    ]
    return StatusSnapshot(
        config=config,
        state=state,
        runner=runner,
        monitoring=monitoring,
        issues=issues,
    )


def collect_operational_status_snapshot(root: Path) -> StatusSnapshot:
    """Collect the small read-only status view used by default status output."""
    root = root.resolve()
    config, config_issues = _load_config_for_status(root)
    state, state_issues = _load_state_for_status(root)
    runner, runner_issue = _load_runner_status_for_status(root)
    monitoring, monitoring_issues = _load_engine_monitoring_for_status(root)
    if runner_issue is not None:
        runner_issues_list: list = [runner_issue]
    else:
        runner_issues_list = []
    issues = [
        *config_issues,
        *state_issues,
        *runner_issues_list,
        *monitoring_issues,
        *_probe_runner_state(root, state, runner),
        *_probe_pool_stop_reason(state),
    ]
    return StatusSnapshot(
        config=config,
        state=state,
        runner=runner,
        monitoring=monitoring,
        issues=issues,
    )
