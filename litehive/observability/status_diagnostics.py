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

from litehive.config.model import LitehiveConfig
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import WorkspaceState
from litehive.workspace import Workspace
# Re-exported public API. Imports kept in `status_diagnostics` so existing
# `from litehive.observability.status_diagnostics import ...` callers keep working.
from litehive.observability.status_loaders import (
    _load_config_for_status_impl,
    _load_engine_monitoring_for_status,
    _load_runner_status_for_status_impl,
    _load_state_for_status,
)
from litehive.observability.status_probes import (
    _probe_daemon_status_impl,
    _probe_heru_link_impl,
    _probe_last_cycle_impl,
    _probe_origin_divergence_impl,
    _probe_pool_stop_reason,
    _probe_runner_state_impl,
    _probe_task_index_references_impl,
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
    "render_health_summary",
    "render_issue_lines",
    "render_operational_issue_lines",
    "status_has_problems",
]


class StatusSnapshotCollector:
    """
    Workspace-bound read-only collector for status diagnostics.

    It binds the workspace once and owns the loader/probe orchestration that
    was previously spread across module-level collection functions.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def load_config(self) -> tuple[LitehiveConfig, list[StatusIssue]]:
        return _load_config_for_status_impl(self.workspace)

    def load_state(self) -> tuple[WorkspaceState, list[StatusIssue]]:
        return _load_state_for_status(self.workspace)

    def load_runner(self) -> tuple[RunnerStatusState, StatusIssue | None]:
        return _load_runner_status_for_status_impl(self.workspace)

    def probe_runner(self, state: WorkspaceState, runner: RunnerStatusState) -> list[StatusIssue]:
        return _probe_runner_state_impl(self.workspace, state, runner)

    def probe_daemon(self) -> list[StatusIssue]:
        return _probe_daemon_status_impl(self.workspace)

    def probe_last_cycle(self) -> list[StatusIssue]:
        return _probe_last_cycle_impl(self.workspace)

    def probe_heru_link(self) -> list[StatusIssue]:
        return _probe_heru_link_impl(self.workspace)

    def probe_origin_divergence(self, state: WorkspaceState) -> list[StatusIssue]:
        return _probe_origin_divergence_impl(self.workspace, state)

    def probe_task_index_references(
        self,
        state: WorkspaceState,
        state_issues: list[StatusIssue],
    ) -> list[StatusIssue]:
        return _probe_task_index_references_impl(self.workspace, state, state_issues)

    def probe_recovery_failure(
        self,
        state: WorkspaceState,
        state_issues: list[StatusIssue],
        runner: RunnerStatusState,
    ) -> list[StatusIssue]:
        return _probe_task_status_damage(self.workspace, state, runner, state_issues)

    def collect(self) -> StatusSnapshot:
        """
        Build the full diagnostic snapshot.
        """
        config, config_issues = self.load_config()
        state, state_issues = self.load_state()
        runner, runner_issue = self.load_runner()
        monitoring, monitoring_issues = _load_engine_monitoring_for_status(self.workspace)
        if runner_issue is not None:
            runner_issues_list: list = [runner_issue]
        else:
            runner_issues_list = []
        issues = [
            *config_issues,
            *state_issues,
            *runner_issues_list,
            *monitoring_issues,
            *self.probe_runner(state, runner),
            *self.probe_daemon(),
            *self.probe_last_cycle(),
            *self.probe_heru_link(),
            *_probe_pool_stop_reason(state),
            *self.probe_origin_divergence(state),
            *self.probe_task_index_references(state, state_issues),
            *self.probe_recovery_failure(state, state_issues, runner),
        ]
        return StatusSnapshot(
            config=config,
            state=state,
            runner=runner,
            monitoring=monitoring,
            issues=issues,
        )

    def collect_operational(self) -> StatusSnapshot:
        """
        Collect the small read-only status view.
        """
        config, config_issues = self.load_config()
        state, state_issues = self.load_state()
        runner, runner_issue = self.load_runner()
        monitoring, monitoring_issues = _load_engine_monitoring_for_status(self.workspace)
        if runner_issue is not None:
            runner_issues_list: list = [runner_issue]
        else:
            runner_issues_list = []
        issues = [
            *config_issues,
            *state_issues,
            *runner_issues_list,
            *monitoring_issues,
            *self.probe_runner(state, runner),
            *_probe_pool_stop_reason(state),
        ]
        return StatusSnapshot(
            config=config,
            state=state,
            runner=runner,
            monitoring=monitoring,
            issues=issues,
        )
