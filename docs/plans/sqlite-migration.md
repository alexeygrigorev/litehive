# SQLite Migration Plan

## Goal

Separate what belongs in git (task intent) from what belongs in a local database
(execution state, runtime data, logs). Move runtime state out of the repo so it
can't accidentally get committed, shrinks clone size, and becomes queryable.

## Motivation

The April 10 incident: a non-sandboxed codex agent picked up T-0266 (git history
cleanup), ran `git filter-repo` on a mirror clone, and force-pushed a rewritten
history to origin — all automatically, while the operator was asleep. The
daemon kept committing to local `main` for hours on a history that no longer
matched origin, producing stuck `merge_failed` tasks and hours of rescue work.

Root causes that this plan addresses:

1. Too much runtime state lives inside `.litehive/` and gets committed by
   accident (`state.yaml`, `engine-monitoring.yaml`, nested `.litehive/.litehive/`,
   `$tmpdir/` literals, subagent transcripts, reports, artifacts).
2. `ensure_workspace()` blindly creates `.litehive/` at whatever path is passed,
   so a subagent with a bad cwd creates garbage workspaces.
3. Task execution data (journal, reports, hook artifacts, runtime.yaml) is
   tangled with task intent (goal, criteria, plan) in the same YAML files.
4. Archive is another directory in git that grows forever.

## Target layout

### In git (committed, small, clean)

```
.litehive/
├── config.yaml              # workspace config (engines, hooks, policies)
├── context.md               # human-written project context
└── tasks/                   # only active tasks
    ├── T-0001-xxx/
    │   ├── task.yaml        # task intent (plan, criteria, constraints)
    │   └── comments.yaml    # agent review comments (formerly thread.yaml)
    └── T-0002-yyy/
        ├── task.yaml
        └── comments.yaml
```

No `archive/`, no `state.yaml`, no `logs/`, no `worktrees/`, no runtime files.

### Out of git (per-workspace, XDG paths)

```
~/.config/litehive/
└── workspaces.yaml          # registry: workspace-id → repo path

~/.local/share/litehive/<workspace-id>/
├── data.db                  # SQLite: everything queryable
├── backups/                 # T-0292 nightly SQLite backups
│   └── data-2026-04-10T03.db.gz
└── subagents/               # big transcripts (too big for DB)
    └── <task-id>/<subagent-id>/

~/.local/state/litehive/<workspace-id>/
├── worktrees/               # git worktrees (must be outside repo)
│   └── T-0100-xxx/
└── logs/                    # daemon run-all logs
    └── 20260410T030000Z/
```

`<workspace-id>` is a hash of the repo's canonical path (or git remote). The
registry is how the CLI finds the right DB from any cwd or from an explicit
`LITEHIVE_TASK_ID` env var (see T-0290).

## SQLite schema

One DB per workspace with tables:

| Table              | Purpose                                             |
|--------------------|-----------------------------------------------------|
| `schema_migrations`| Applied migration versions (T-0293)                 |
| `pool_state`       | Key/value: active_task_id, mode, pool_stop_reason, daemon_pid, heartbeat_at. `next_task_number` becomes obsolete: IDs come from `task_sequence` autoincrement. |
| `task_sequence`    | `id INTEGER PRIMARY KEY AUTOINCREMENT` — sole source of new task numbers. `T-XXXX` is just `T-<id:04d>`. Retires the `state.yaml:next_task_number` race and the T-0143 triple-ID incident we had today. |
| `queue`            | Ordered queue (position, task_id)                   |
| `task_state`       | Per-task mutable fields (status, pipeline_status, flag_count, commit_sha, updated_at, runtime snapshot) |
| `task_intent`      | Mirror of task.yaml + comments.yaml (for query and for archived tasks) |
| `task_journal`     | Append-only timeline replacing `journal.md`        |
| `stage_reports`    | All stage report YAMLs                              |
| `hook_artifacts`   | Hook stdout/stderr replacing `artifacts/*.yaml`    |
| `subagent_sessions`| Session metadata (references files in `subagents/`)|
| `events`           | Event log replacing `events.jsonl`                 |
| `engine_monitoring`| Per-engine invocation counts, usage, limits        |
| `attention`        | Human-attention queue (T-0289)                     |
| `worktrees`        | Registered git worktrees (replaces state unmerged_worktrees) |

## Archive semantics

Current: `litehive close T-0123` moves files from `tasks/` to `archive/`, both
in git.

New: `litehive close T-0123`:

1. Final sync of task intent to `task_intent` table in DB
2. `rm -rf .litehive/tasks/T-0123-xxx/`
3. `git add -A && git commit -m "archive T-0123"`
4. DB retains the full record indefinitely

To browse archived tasks: `litehive archive list/show/restore` which queries
DB. To see pre-archive state from git: `git show <commit>^:.litehive/tasks/...`.

## Phases / Tasks

| Task   | Title                                                    | Depends on            |
|--------|----------------------------------------------------------|-----------------------|
| T-0290 | Resolve workspace from task ID or env/config             | —                     |
| T-0291 | Migrate workspace state from files to SQLite             | —                     |
| T-0292 | SQLite backup mechanism                                  | T-0291                |
| T-0293 | SQLite schema migrations framework                       | T-0291                |
| T-0294 | Rename task discussion storage to `comments.yaml`        | —                     |
| T-0295 | Split `task.yaml` into intent + runtime state            | T-0291                |
| T-0296 | Move worktrees to `~/.local/state/litehive/<wid>/worktrees/` | T-0291            |
| T-0297 | Move run-all logs to `~/.local/state/litehive/<wid>/logs/`   | T-0291            |
| T-0298 | Archive deletes filesystem, keeps in DB                  | T-0291, T-0295        |
| T-0299 | `ensure_workspace()` refuses nested `.litehive/` creation| —                     |
| T-0300 | Migrate existing workspace to new layout                 | T-0291, T-0295, T-0296, T-0297, T-0298 |

Rough execution order:

```
T-0290 (parallel)
T-0294 (parallel, isolated rename)
T-0299 (parallel, small guard)

T-0291 (schema + dual-write)
  ├─ T-0292 (backups)
  ├─ T-0293 (migrations framework, also lands schema 0001)
  └─ T-0295 (task.yaml split)
       ├─ T-0296 (worktrees relocation)
       ├─ T-0297 (logs relocation)
       └─ T-0298 (archive semantics)
            └─ T-0300 (one-time migration of current workspace)
```

## Migration strategy (T-0291 internal phases)

1. **Dual-write**: write to both DB and files. Files remain source of truth.
2. **Dual-read**: read from DB first, fall back to files if missing. Cross-check.
3. **Flip**: DB becomes source of truth. Files become read-only mirror.
4. **Drop files**: stop writing runtime files; old files stay until T-0300.
5. **Clean**: T-0300 backfills any missing rows from existing files, then
   deletes the migrated files from filesystem (and from git on the next commit).

## Open questions / notes

- Multi-machine story: task intent ships via git, execution state stays per
  machine. Fine for single-daemon setups. A shared-state mode could be added
  later via a remote SQLite or rsync — out of scope for this plan.
- `archive/INDEX.csv` in git: dropped. DB is the archive. Optional quick-view
  summary file could be auto-generated but not committed.
- Git history of archived tasks: preserved via the archive-commit diff. Use
  `git log --diff-filter=D -- .litehive/tasks/` to find when each task was
  archived.
- `git blame task.yaml`: still works for the fields that remain in task.yaml
  (intent). Runtime fields that move to DB lose git blame but gain query
  capabilities via `task_journal`.
- No accidental runtime commits ever again: if a subagent tries to write to
  a runtime path, it writes to the DB which is outside the repo.
