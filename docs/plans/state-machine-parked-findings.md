# Parked Findings — Task Lifecycle & Persistence

These findings came out of the pipeline state machine exploration but are **out of scope**
for the current pipeline_v2 work. Parked here so we can come back to them without losing
context. See `state-machine-overhaul.md` for the active pipeline work.

---

## 1. Three Orthogonal Status Fields

No cross-field validation between them today:

1. **`task.status`** (`litehive/models/common.py:37-49`) — queued, in_progress, parked, interrupted, flagged, merge_failed, done, cancelled, wont_do, deferred, duplicate
2. **`task.pipeline_status`** — backlog, grooming, implementing, testing, accepting, commit_to_git, done, flagged, merge_failed, interrupted
3. **`RunnerStatusState.status`** (runtime_models.py:144) — idle, running, late, stale (observability on the process, not the task)

A task can be `status=queued, pipeline_status=implementing` with no guard against
nonsensical combinations. Pipeline v2 addresses the stage dimension; lifecycle and runner
health remain to be unified later.

---

## 2. Where Lifecycle State Lives

- **Type definitions:** `litehive/models/common.py:37-49`
- **User-facing transitions:** `litehive/workspace/task_status.py` — `requeue_task`, `resume_task`, `abandon_task`, `close_task`, `stop_current_task`, `switch_task_engine`
- **Queue/selection:** `litehive/tasks/queue_ops.py` — `dequeue_next_task`, `set_active_task`, dependency validation
- **Recovery resets:** `litehive/tasks/queue_management.py:75` — `_reset_task_for_recovery`
- **Constants:** `litehive/tasks/constants.py:20-21` — `CLOSED_TASK_STATUSES`, `RESUMABLE_TASK_STATUSES`

---

## 3. Dual Persistence

- **YAML:** `litehive/tasks/persistence.py` — `.litehive/tasks/T-*/task.yaml`, `.litehive/state.yaml` (legacy, still authoritative on some reads)
- **SQLite:** `litehive/storage/runtime.py` — `.litehive/db.sqlite` tables `pool_state`, `queue`, `task_state`

Migration (`persistence.py:24-30`) only runs on `WorkspaceState`, not per-task. Dual-write
creates sync risk. Task runtime state lives in a third place: `.litehive/tasks/T-*/.runtime.yaml`.

**Question for later:** unify behind SQLite-only and make YAML an export, or keep both?

---

## 4. Lifecycle Pain Points

1. **No cross-field validation** between `status` and `pipeline_status`.
2. **Scattered guards** — per-function checks in `task_status.py:63, 204, 365, 473`, no central validator.
3. **`_reset_task_for_recovery` complexity** (`queue_management.py:75-112`) — 5 conditional branches reused across requeue/resume/abandon with different semantics; lingering `interruption` / `continuation_handoff` risk.
4. **Dual persistence sync risk** (see above).
5. **Runner health not synced** on task transitions (e.g. during `stop_current_task`).
6. **Four closed statuses** (cancelled / wont_do / deferred / duplicate) should collapse into one state + `close_reason`.

---

## 5. Proposed Unified Lifecycle (draft)

One primary `state` field + a `stage` cursor (only read when mid-flight). `stage` comes
from pipeline_v2; this layer sits above it.

| State | Meaning | Terminal | Resumable |
|---|---|---|---|
| **Draft** | Not yet ready to run (replaces `backlog`) | No | n/a |
| **Ready** | Groomed and enqueued | No | n/a |
| **Blocked** | Enqueued but has unsatisfied deps | No | n/a |
| **Active** | Runner owns it and is executing | No | n/a |
| **Paused** | Clean user-initiated halt | No | Yes |
| **Interrupted** | Unclean halt (crash/stale), awaiting recovery | No | Yes (→ Active via pipeline recovering stage) |
| **Flagged** | Operator intervention required | No | Only explicitly |
| **Done** | Successfully merged | Yes | No |
| **Abandoned** | Terminal non-success; `close_reason` ∈ cancelled / wont_do / deferred / duplicate | Yes | No |

**Note:** `Recovering` is NOT in this layer — it's a *pipeline* stage (see
pipeline_v2). A task in lifecycle state `Active` can be in pipeline stage `recovering`.

**Auxiliary fields (not states):**
- `stage` — cursor into pipeline_v2 state machine
- `close_reason` — only when Abandoned
- `runner_health` — observability on the process
- `recovery_attempt` — counter

### Simplifications vs. today

1. `queued` collapses into `Ready` / `Blocked`.
2. `in_progress` becomes `Active`.
3. `merge_failed` and `recovery_failed` disappear from the lifecycle layer — handled entirely inside pipeline_v2 (stage = recovering, or terminal → flagged).
4. Four closed statuses collapse into `Abandoned + close_reason`.
5. `Paused` vs `Interrupted` becomes explicit (clean vs unclean halt).

---

## 6. Open Questions (parked)

- Should `Blocked` be a state or a computed predicate from the dep graph?
- Single transition function for lifecycle, or per-driver entry points?
- How aggressively to consolidate persistence (drop YAML, or keep as export)?
- How to migrate existing tasks in-flight during the lifecycle overhaul?
- Should `Paused` and `Interrupted` share a resume path, or stay distinct?
