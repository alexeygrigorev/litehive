"""
Pure data types shared across packages.

Each submodule owns one slice of the domain vocabulary so callers can
locate a record by name without grepping:

- ``agent`` — subagent execution result, engine failure, inactivity timeout.
- ``common`` — shared enums and timestamp/feedback helpers.
- ``engine`` — engine usage records and workspace monitoring snapshots.
- ``lifecycle_deltas`` — typed state-machine effect functions.
- ``pool`` — dirty-worktree finding/report dataclasses.
- ``recovery`` — recovery trigger/outcome/fingerprint vocabulary.
- ``reports`` — stage and recovery reports plus activity entries.
- ``runtime`` — task runtime state slices persisted alongside ``TaskRecord``.
- ``task`` — ``TaskRecord`` and its intent/state projections.
- ``task_ops`` — non-record dataclasses used by task operations (locks, plans,
  selection results, repair summaries).
- ``worktree`` — worktree dataclasses and the ``WorktreeMergeConflict`` exception.

This package holds records only. Behaviour (lifecycle, state machine,
worktree mechanics, persistence) lives in dedicated packages so the
architecture guardrail tests can keep ``domain/`` IO-free.
"""
