# Workspace-First Argument Audit - 2026-05-07

Source checklist item: `docs/voice-instructions-2026-05-06.md` SA5.

Command used:

```bash
rg -n "^(def|async def) [^(]+\(workspace\b|^    def [^(]+\(self, workspace\b|^    def [^(]+\(cls, workspace\b|^    async def [^(]+\(self, workspace\b" litehive -g '*.py'
```

This audit intentionally covers production code only. Test helpers and
pytest fixtures named `workspace` are not product ownership boundaries.

## Findings

The current code still has many free functions whose first argument is
`workspace`. They are not all the same kind of problem:

- CLI commands and CLI support functions receive `workspace` because
  Typer or a thin command handler resolved the workspace at the boundary.
  These should stay thin and dispatch into services.
- Daemon APIs still use `Path` in several places because the daemon process
  boundary and lock metadata predate the `Workspace` object. These are
  migration candidates for daemon-specific services, not methods on
  `Workspace` itself.
- State, task, report, activity, queue, worktree, recovery, observability,
  and subagent-session helpers mostly operate on a real `Workspace`. These
  should move toward a method on `Workspace` only when the behavior is a
  direct workspace capability; otherwise they should become focused bound
  services such as `RuntimeStore`, `SubagentArtifactStore`, worktree
  services, queue services, or report stores.
- Constructors such as `RuntimeStore(workspace)`, `WorktreeService(workspace)`,
  `SubagentIdRepository(workspace)`, and lifecycle persistence/journal
  classes already follow the preferred shape: bind the workspace once and
  expose methods on the collaborator.

## Module Inventory

High-volume modules that need focused follow-up rather than piecemeal
renames:

- `litehive/daemon/execution.py`: 13 workspace-first functions. These are
  daemon process-control and status operations; move toward a daemon service.
- `litehive/cli/runner.py`: 12 functions. CLI-boundary handlers and helper
  functions; keep handlers thin and move logic behind services.
- `litehive/daemon/registry.py`: 11 functions. Daemon lock/metadata registry;
  this should become a bound daemon registry service.
- `litehive/tasks/queue_selection.py`: 9 functions. Queue state selection and
  mutation should move behind a queue service.
- `litehive/lifecycle/worktree_setup.py`: 9 functions. These are lifecycle
  worktree collaborators and should continue moving to worktree services.
- `litehive/cli/task_debug_support.py`: 9 functions. CLI debug presentation;
  keep as thin rendering/dispatch only.
- `litehive/cli/task_logs_support.py`: 8 functions. CLI log presentation;
  several already call `workspace.load_subagent_session_record(...)`.
- `litehive/tasks/report_storage.py`: 7 functions. Candidate for a bound
  report store.
- `litehive/agents/session_store.py`: 7 functions. Already partly migrated
  to `SubagentArtifactStore`; remaining free loaders are compatibility
  entry points or slice readers.
- `litehive/state/locking.py`: 6 functions. Runner/mutation lock ownership
  should stay in state locking but can be exposed through a bound lock
  service.
- `litehive/tasks/queue_mutations.py`: 5 functions. Same queue-service
  migration as queue selection.
- `litehive/tasks/event_log.py`: 5 functions. Candidate for a task event-log
  store.
- `litehive/cli/daemon_cli.py`: 5 functions. CLI boundary.

Lower-volume modules with one to four hits:

- Config/runtime: `litehive/config/runtime_settings.py`,
  `litehive/config/loading.py`, `litehive/config/engine_models.py`.
- Task helpers: activity, activity rendering, audit, completed-task
  recovery, journal, stop, status resume, queue eligibility,
  `_status_helpers.py`.
- Recovery helpers: running-task recovery, nonrunning resumable repair,
  scope analysis, workspace repair.
- Observability helpers: attention, events, engine monitoring, status,
  status dashboard, status diagnostics, status loaders, status probes,
  status summary.
- Worktree helpers: cleanup, inspection, rescue, service constructors.
- Lifecycle helpers: sessions, prompt serializer, persistence, journal,
  Heru factory, runtime sync.
- Role helpers: recovery prompt diagnostics and latest reject-stage lookup.
- CLI helpers: agent, engine, pool, queue, task, workspace, worktree.

## Disposition

Verified exact production code paths with the `rg` command above. The audit
does not claim that all workspace-first functions are fixed. It records the
remaining ownership map so future checklist items can convert them in bounded
slices:

- Prefer a bound collaborator when the module already has a natural owner
  (`SubagentArtifactStore`, `RuntimeStore`, worktree service, queue service,
  report store, daemon registry).
- Prefer a `Workspace` method only for behavior that reads as a direct
  workspace capability and does not belong to a narrower domain service.
- Keep CLI command functions as workspace-boundary functions, but do not let
  them own business logic.
