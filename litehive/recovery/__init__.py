"""Runtime recovery boundary.

Modules in this package own the post-crash and stale-runner repair flows that
the daemon, CLI, and queue selector reach for when the workspace looks wedged:

- ``execution_recovery`` orchestrates the top-level "is this workspace stuck?"
  scan and dispatches to the per-task repair helpers.
- ``running_task_recovery`` owns the per-task pass for tasks the workspace
  still believes are ``running`` after a runner died.
- ``nonrunning_resumable_repair`` covers tasks left in a wedged shape
  (queued/in-progress/interrupted at a resumable stage) after a crash.
- ``interrupted_subagent`` snapshots a still-running subagent into an
  ``interrupted`` record so resume sees a frozen artifact.
- ``interruption_state`` carries the task-level ``INTERRUPTED`` bookkeeping
  and renders the operator-visible journal line.
- ``workspace_repair`` is the coarse-grained entry point used by the daemon
  startup path and the ``litehive repair`` CLI.
- ``scope_analysis`` distinguishes operator cleanup from SWE scope creep when
  the recovery agent triages a worktree's deletions.
- ``detection`` carries the small structured exceptions shared by selection
  and repair paths.
"""
