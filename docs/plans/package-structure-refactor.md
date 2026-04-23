# Litehive refactoring plan

> Historical planning document.
> Most of the structural work described here has already landed, and some
> planning-time details no longer match current code. In particular:
> workspace state is now SQLite-only, `comments.yaml` is the only supported
> task discussion file, and the daemon registry no longer uses a global YAML
> file.

Structural cleanup of `litehive/` to remove v1→v2 legacy shims, consolidate overlapping packages, and rename packages whose names no longer match their contents.

## Goals

1. Delete every piece of v1→v2 compat code.
2. Collapse `tasks/` + `workspace/` into a clearer state/operations split.
3. Rename `pipeline/` → `lifecycle/` (the package is a state machine, not a pipeline).
4. Consolidate models into a single location.
5. Keep every step independently reviewable — no big-bang commit.

---

## Part A — Legacy v1→v2 code to delete

### A1. `pipeline/compat.py` + `pipeline/__init__.py` export

`drain_task_pool` has **zero internal call sites**. It only exists as public API re-exported from `pipeline/__init__.py:6`.

- Delete `litehive/pipeline/compat.py`.
- Edit `litehive/pipeline/__init__.py` to drop the `drain_task_pool` re-export.
- External callers that depended on it should call `run_task()` in a loop (that is what `compat.py:19-60` did anyway).

### A2. `litehive/git_ops.py` (pure shim for `git/ops.py`)

Five importers. Mechanical rewrite:

| File | Line | Change |
|---|---|---|
| `workspace/task_status.py` | 11 | `from litehive.git_ops import current_head` → `from litehive.git.ops import current_head` |
| `tasks/crud.py` | 16 | `from litehive.git_ops import default_commit_message` → `from litehive.git.ops import default_commit_message` |
| `tasks/reports.py` | 8 | `from litehive.git_ops import GitError, current_head, is_git_repo, status_porcelain` → `from litehive.git.ops import …` |
| `cli/queue_cli.py` | 15 | `from litehive.git_ops import GitError, checkpoint_message` → `from litehive.git.ops import …` |
| `cli/runner.py` | 23 | `from litehive.git_ops import GitError, checkpoint_message` → `from litehive.git.ops import …` |

- Delete `litehive/git_ops.py`.

### A3. `agents/base.py` (re-exports `heru.base`)

Nine importers (including tests). Change each to `from heru.base import …` directly.

- Delete `litehive/agents/base.py`.

### A4. `agents/engine_detection.py` (reloads `heru._engine_detection`)

Two real importers: `agents/sandbox.py`, `agents/manager.py`. The `reload()` on import (line 7) is the only subtle bit — investigate whether it is load-bearing (it looks like a test-isolation relic).

- If not load-bearing: change both importers to `from heru._engine_detection import …` and delete the file.
- If load-bearing: move the reload into a test fixture, then delete the file.

### A5. `agents/_execution.py` (SubagentManager monkeypatch hook)

Only caller is `pipeline/heru_factory.py:24` (via `litehive.agents` re-export). The file wraps `SubagentManager` to expose a `get_engine` monkeypatch hook.

- Move the `get_engine` extension point into `agents/manager.py` directly — either as an overridable method or a callable passed to the constructor.
- Update `pipeline/heru_factory.py` to import from `agents.manager` (or via the `agents/__init__.py` lazy export, which already routes to `manager` directly at lines 61–68).
- Delete `agents/_execution.py`.

### A6. `tasks/persistence.py` YAML fallback (historical note)

At the time of this plan, `load_state()` still fell back to reading legacy
`state.yaml` when SQLite was empty. The final cleanup removed that fallback;
workspace state now lives only in SQLite.

**Two options:**

- **Option A (aggressive):** delete the fallback. Unmigrated workspaces crash loudly with a clear migration error.
- **Option B (safer):** add a one-shot `litehive migrate-state` CLI command, run it on every known workspace, then delete the fallback in a follow-up commit.

**Pick Option B** unless every live workspace is confirmed to already be on SQLite.

### A7. `migrate_legacy_worktree()` — 5 call sites

`tasks/worktrees.py:58-63` is called from:

- `tasks/crud.py:176, 184, 195`
- `cli/worktree_support.py:67, 98`
- `workspace/task_status.py:289`
- `tasks/reports.py:50`

Same shape as A6 — any repo with a legacy-path worktree will fail after removal. Gate with the same one-shot migration command as A6.

### A8. Mop-up

Grep `v1`, `legacy`, `compat`, `deprecated`, `backward` across the tree and delete orphaned code paths.

---

## Part B — Consolidate `tasks/` + `workspace/` into `state/` + `tasks/`

### Current reality

The two packages aren't actually duplicates — they have distinct responsibilities — but the split is opaque and the file names mislead. The real layering is: **locking → persist → record I/O → queue ops → status transitions → runtime tracking**.

| Current file | Actual role |
|---|---|
| `tasks/crud.py` | Low-level record I/O: read/write task JSON files |
| `tasks/queue_management.py` | Pure list mutations: `enqueue`, `move`, `prioritize` |
| `tasks/queue_ops.py` | Selection logic: dequeue, block, dep-resolve |
| `workspace/workflow.py` | Atomic persist orchestrator (SQLite + YAML in one tx) |
| `workspace/task_status.py` | High-level status transitions (stop, requeue, abandon, close, park) |
| `workspace/runtime_tracking.py` | Pipeline runtime markers (stage/subagent started/finished) |
| `workspace/locking.py` | All locks + runner status |

### Target layout

```
litehive/
  state/                          # ex-workspace/ + low-level record I/O
    locking.py                    # was workspace/locking.py
    persist.py                    # was workspace/workflow.py (atomic writer)
    records.py                    # was tasks/crud.py (low-level I/O)
    store.py                      # was storage/runtime.py (SQLite repo)

  tasks/                          # task-domain operations
    queue.py                      # MERGE queue_management.py + queue_ops.py
    status.py                     # was workspace/task_status.py
    runtime.py                    # was workspace/runtime_tracking.py
    journal.py                    # unchanged
    archive.py                    # unchanged
    paths.py                      # unchanged
    worktrees.py                  # minus migrate_legacy_worktree
    reports.py                    # unchanged
```

### Key points

- **`workspace/` disappears as a package.** Its contents are all about *persisting task state under a lock*, which is the state layer. Rename to `state/` and the confusion goes away.
- **`tasks/queue_management.py` + `tasks/queue_ops.py` merge into `tasks/queue.py`** with two internal sections (`# --- list ops ---` / `# --- selection ---`). 125 + 529 LOC, no duplication, just bad naming.
- **`tasks/crud.py` moves to `state/records.py`** because it is low-level I/O, not domain logic. After the move, `tasks/` contains only operations that reason about tasks as a concept.
- **`workspace/worktree_inspection.py` → `tasks/worktrees.py`** (same topic).
- **`storage/runtime.py` → `state/store.py`** and delete the `storage/` package (only `backup.py` remains — move it to `state/backup.py` or `maintenance/backup.py`).

### Touch cost

Pure imports — no semantic change. `git mv` + sed, probably 200–400 import-line edits total. Test suite catches anything missed.

---

## Part C — Rename `pipeline/` → `lifecycle/`

"Pipeline" is misleading: the package is a **state machine** (rules + guards + transitions + events), not a linear flow, and it collides with the CI/CD meaning people bring in from elsewhere.

### Rename

- `litehive/pipeline/` → `litehive/lifecycle/`
- Update all `from litehive.pipeline…` imports to `from litehive.lifecycle…`.
- Move `pipeline/orchestration.py:run_task` up to `lifecycle/__init__.py` (or a short `lifecycle/entry.py`) so the common call site is `from litehive.lifecycle import run_task`.

### Split out role agents

The current `pipeline/agents/` directory holds `RoleAgent` subclasses (planner, swe, qa, reviewer, merge, recovery). These are conceptually distinct from the state machine mechanics. Move them:

- `litehive/pipeline/agents/` → `litehive/roles/`

After this, `lifecycle/` contains only state-machine mechanics (rules, runner, nodes, events, deltas, guards, transitions, stages, journal, persistence). `roles/` holds per-role prompts and verdict logic. `agents/` (renamed later — see Part D) holds the heru-facing subagent runtime.

### Optional follow-up: rename `agents/` → `engines/`

`litehive/agents/` (SubagentManager, sandbox, session) is the runtime that talks to external CLI engines via heru. Renaming to `engines/` would make the `engines/` vs `roles/` distinction self-explanatory: engines execute, roles specify what to execute.

This can be deferred until after the `lifecycle/` rename lands.

---

## Part D — Consolidate models into `domain/`

Task-shaped data is currently defined in six places:

- `litehive/models/` (shared models package)
- `litehive/tasks/models.py` (`RunnerLockState`, `BlockedTask`, `TaskSelection`)
- `litehive/agents/models.py` (`SubagentResult`, `EngineFailure`)
- `litehive/config/pool_types.py` (`RunResult`, `ExecutionSummary`, `TaskPoolRunSummary`)
- `litehive/pipeline/persistence.py` (`TaskState`)
- `litehive/pipeline/deltas.py` (`StateDelta`)

Target: **one `litehive/domain/` package** containing all dataclasses and enums, with submodules for topical grouping (`domain/task.py`, `domain/pipeline.py`, `domain/agent.py`, `domain/config.py`). Every other package imports from `domain/`.

This is a separate commit from Parts A–C because it touches a different axis.

---

## Part E — Config package slimdown

`litehive/config/` has 12 files for what is essentially "load YAML, validate, expose a dataclass":

```
constants.py  dataclasses.py  engine_models.py  formatting.py  loading.py
model.py  normalization.py  paths.py  pool_types.py  startup_guidance.py
workspace.py  workspace_registry.py
```

Target: 4–5 files.

- `config/model.py` — the `LitehiveConfig` dataclass + all supporting dataclasses (merge `dataclasses.py`, `model.py`, `constants.py`, `pool_types.py` into `domain/config.py` per Part D, re-exported here).
- `config/loading.py` — YAML load + merge + validation (merge `loading.py`, `normalization.py`).
- `config/paths.py` — unchanged (filesystem paths).
- `config/workspace.py` — bootstrap (`.litehive` dir init).
- `config/registry.py` — renamed from `workspace_registry.py`.
- `config/engine_models.py` — stays (engine resolution logic is substantial).
- Delete: `formatting.py` (move helpers to `cli/display.py`), `startup_guidance.py` (move to `roles/guidance.py` — it is role-specific anyway).

---

## Staged migration order

Each step is a separate commit. Each is independently reviewable and independently revertible.

### Stage 1 — dead shims (pure wins, immediate)

1. Delete `pipeline/compat.py` + drop `drain_task_pool` re-export. **(A1)**
2. Delete `git_ops.py`, rewrite 5 imports. **(A2)**
3. Delete `agents/base.py`, rewrite ~9 imports. **(A3)**

These are pure mechanical changes with zero behavior risk. Land them first.

### Stage 2 — monkeypatch collapse

4. Collapse `agents/_execution.py` into `agents/manager.py`; update `pipeline/heru_factory.py`. **(A5)**
5. Investigate and collapse `agents/engine_detection.py`. **(A4)**

### Stage 3 — user-data migrations (needs deployment coordination)

6. Add `litehive migrate-state` one-shot CLI command covering both YAML state and legacy worktrees. **(A6/A7 prep)**
7. Run it on every live workspace.
8. Delete YAML fallback in `tasks/persistence.py`. **(A6)**
9. Delete `migrate_legacy_worktree()` and all 5 call sites. **(A7)**

### Stage 4 — internal consolidation

10. Merge `tasks/queue_management.py` + `tasks/queue_ops.py` → `tasks/queue.py`. **(Part B)**
11. Rename `workspace/` → `state/`, move `tasks/crud.py` → `state/records.py`, move `storage/runtime.py` → `state/store.py`. Big import-rewrite commit, no behavior change. **(Part B)**

### Stage 5 — lifecycle rename

12. Rename `pipeline/` → `lifecycle/`, move `run_task` up to `lifecycle/__init__.py`. **(Part C)**
13. Split `pipeline/agents/` → `roles/`. **(Part C)**
14. *(Optional)* Rename `agents/` → `engines/`. **(Part C)**

### Stage 6 — structural cleanup

15. Consolidate all models into `domain/`. **(Part D)**
16. Slim down `config/` from 12 files to 5. **(Part E)**
17. Final mop-up: grep `v1`/`legacy`/`compat`/`deprecated` and delete stragglers. **(A8)**

---

## Notes

- **Do not combine stages.** Each stage is independently valuable and independently revertible. Combining them produces a PR that nobody can review.
- **Stages 1–2 can land today** with zero deployment risk.
- **Stage 3 requires coordination** with any live workspaces — this is the only user-data-touching work in the plan.
- **Stages 4–6 are pure refactors** once stage 3 is done. Pace them as bandwidth allows.
- Keep the test suite green on every commit. Import rewrites are the main failure mode; `uv run pytest` catches them.

---

## Next phase — go-live monitoring

After the refactor stages land, turn the daemon back on and run the workspace under executor-driven monitoring.

### Steps

1. **Start the background runner:**
   ```bash
   litehive start
   ```
   Verify it came up with `litehive status` before proceeding.

2. **Retarget and unpause tmuxctl job #2.** Job #2 currently targets session `litehive` every `30m` and is disabled. Keep the `30m` interval — just switch the session to `lh2` and enable it:
   ```bash
   tmuxctl edit 2 --session lh2 --enable
   tmuxctl resume 2
   ```
   This is the long-interval heartbeat that pings the executor with the checklist from `litehive-heartbeat.txt` every 30 minutes.

3. **Executor self-monitoring loop (every 4 minutes).** Separately from the tmuxctl heartbeat, the executor runs its own tighter status poll:
   ```bash
   sleep 4m && litehive status
   ```
   The executor's job during this phase is **active repair**, not passive observation. Immediately after the refactor lands, things will break — imports will be wrong in edge paths the tests didn't cover, renamed modules will be referenced by stringly-typed lookups, the daemon may fail to start, tasks may wedge on stages that moved. Every 4-minute tick:
   - Read status and any new journal/transition rows since the last tick.
   - If something is broken: diagnose root cause, fix in code, commit, restart the daemon if needed, verify the fix stuck.
   - If nothing is broken: still scan for silent regressions (tasks not advancing, recovery looping, queue not draining).
   - Keep going until the system has run clean across several heartbeats.

   The 30-minute tmuxctl heartbeat is the deeper checklist walk; the 4-minute self-poll is the tight repair loop that catches breakage fast while the refactor is still settling.

### Verified commands

All of the above have been smoke-checked against the current CLIs:

- `litehive start` / `litehive status` / `litehive stop` — present in `litehive --help`.
- `tmuxctl edit <id> --session --every --enable` — supported by `tmuxctl edit --help`.
- `tmuxctl resume <id>` — supported.
- Job #2 exists (`tmuxctl jobs 2`) and is currently disabled, session `litehive`, every `30m`.

### Not yet implemented

This phase is intentionally deferred until the refactor stages above are complete. When ready to flip it on, land it as its own operation — do not bundle with a refactor stage.
