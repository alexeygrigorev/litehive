"""
Read-only diagnostics entry point for ``litehive status``.

Owns snapshot construction; the implementation is split across
sibling modules and re-exported here so callers keep importing
from a single path:

- :mod:`.status_types` — :class:`StatusIssue`, :class:`StatusSnapshot`,
  severity literal, recovery-failure context, constants.
- :mod:`.status_io` — YAML/JSON readers and small parsing helpers
  used by loaders and probes.
- :mod:`.status_loaders` — config, state, runner, and
  engine-monitoring loaders.
- :mod:`.status_probes` — every ``_probe_*``.
- :mod:`.status_rendering` — :func:`status_has_problems` and the
  ``render_*`` helpers.
"""

from litehive.workspace import Workspace
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
    "collect_operational_status_snapshot_for_workspace",
    "collect_status_snapshot_for_workspace",
    "render_health_summary",
    "render_issue_lines",
    "render_operational_issue_lines",
    "status_has_problems",
]


def collect_status_snapshot_for_workspace(workspace: Workspace) -> StatusSnapshot:
    """
    Build the full diagnostic snapshot from an injected workspace.

    Status code receives an injected workspace so diagnostics do
    not rebuild dependencies during read-only status collection.
    """
    root = workspace.root
    config, config_issues = _load_config_for_status(root)
    state, state_issues = _load_state_for_status(workspace)
    runner, runner_issue = _load_runner_status_for_status(root)
    monitoring, monitoring_issues = _load_engine_monitoring_for_status(workspace)
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
        *_probe_daemon_status(root),
        *_probe_last_cycle(root),
        *_probe_heru_link(root),
        *_probe_pool_stop_reason(state),
        *_probe_origin_divergence(root, state),
        *_probe_task_index_references(root, state, state_issues),
        *_probe_task_status_damage(workspace, state, runner, state_issues),
    ]
    return StatusSnapshot(
        config=config,
        state=state,
        runner=runner,
        monitoring=monitoring,
        issues=issues,
    )


def collect_operational_status_snapshot_for_workspace(workspace: Workspace) -> StatusSnapshot:
    """
    Collect the small read-only status view from an injected workspace.

    This is the routine status path; keeping the workspace dependency
    explicit avoids a hidden root-to-workspace conversion on every
    status render.
    """
    root = workspace.root
    config, config_issues = _load_config_for_status(root)
    state, state_issues = _load_state_for_status(workspace)
    runner, runner_issue = _load_runner_status_for_status(root)
    monitoring, monitoring_issues = _load_engine_monitoring_for_status(workspace)
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
