# Pipeline State Machine

Clean-slate design for the task pipeline state machine. Scope is **pipeline only**:
stages, verdict routing, stage execution, hooks, recovery dispatch. The broader task
lifecycle (lifecycle states, persistence, closed-state cleanup) is parked in
`state-machine-parked-findings.md`.

## Design Goals

### Self-healing

**Every stage has its own recovery agent slot.** Recovery is not a fallback or an
escape hatch — it is a first-class capability available at each stage. When any stage
hits an unrecoverable error, a recovery agent runs with the specific context of *that
stage* and tries to fix it.

1. **Recovery lives next to the work it fixes.** A grooming failure gets a grooming-
   aware recovery agent. A testing failure gets a testing-aware one.
2. **One recovery attempt per stage.** Bounded by construction — a task can trigger
   recovery at most once at each stage it visits.
3. **Recovery never triggers recovery.** If the recovery agent itself crashes or
   fails, the task goes straight to `failed`. No recursive self-healing.
4. **Pre-execution recovery is a separate slot** with its own once-per-task budget.

### Continuable

**Stopping is not a state change.** When the pool shuts down, the operator hits stop,
or the host reboots, the task simply stays in whatever state it was in. On next pool
start, the runner picks up exactly where it left off — including resuming live agent
sessions via session-continue where the engine supports it.

1. **No `paused` or `interrupted` state.** The task's current state *is* its resume
   point. There is nothing to "unpark."
2. **Session continuity is first-class.** Agent nodes persist their session handle
   (engine session id, conversation id, etc.) so a restart resumes the same thread.
3. **Stops are honored at phase boundaries.** A shutdown signal waits for the current
   hook or agent turn to finish, then persists and exits. Mid-phase hard kills leave
   the task mid-state; pre-exec recovery handles those on next boot.

---

## 1. Nodes

Every node falls into one of four types, based on **who acts in that state**:

| Type | Who acts | Can emit | Nodes |
|---|---|---|---|
| **agent** | AI agent runs here | Pass / Reject / Blocked / error tiers | `grooming`, `implementing`, `testing`, `accepting`, `recovering` |
| **hook** | Bash command(s) run here | hook_ok / Reject / error tiers | `before_<stage>`, `after_<stage>` for every stage |
| **system** | Deterministic code runs here | Pass / Reject / error tiers | `commit` |
| **terminal** | Nothing runs; task is at rest | — | `done`, `failed` |

Each stage is a three-phase sandwich so hooks are first-class:

```
before_<stage>  →  <stage>  →  after_<stage>
   [hook]         [agent or      [hook]
                    system]
```

### Agent nodes

| Node | Purpose |
|---|---|
| `grooming` | Planner clarifies scope, shapes acceptance criteria |
| `implementing` | SWE writes code |
| `testing` | QA verifies the implementation |
| `accepting` | Reviewer makes final done/not-done judgment |
| `recovering` | Recovery agent diagnoses and repairs the task (cross-cutting; reachable from any stage) |

### System nodes

| Node | Purpose |
|---|---|
| `commit` | Merge worktree into main. No agent, deterministic git operation. |

### Hook nodes

One `before_X` and one `after_X` per stage, for all 5 stages — **10 hook nodes total**,
including `before_commit` and `after_commit`. Each runs registered bash commands.

### Initial and terminal nodes

| Node | Meaning |
|---|---|
| `ready` | **Initial state.** Task has been dequeued; about to enter the state machine. Pre-execution recovery decides whether to go straight to `before_grooming` or into `recovering (pre-exec)`. |
| `done` | **Terminal.** Successfully merged. |
| `failed` | **Terminal.** All recovery budgets exhausted, or an unrecoverable error occurred with no budget left. Carries a `failed_reason` field (see below). |

`failed_reason` enum (on the task, set when transitioning into `failed`):

| Reason | Meaning |
|---|---|
| `recovery_exhausted` | Recovery was attempted but the recovery agent gave up |
| `recovery_budget_hit` | A second recovery attempt was requested for the same stage |
| `recovery_crashed` | The recovery agent itself crashed or errored out |
| `pre_exec_recovery_failed` | Pre-execution recovery couldn't salvage the task |
| `operator_abandoned` | Operator explicitly aborted the task (lifecycle layer transition) |
| `unrecoverable_error` | Error tier 3 occurred without any recovery budget remaining |

A free-form `failed_message` field accompanies the reason for operator context.

---

## 2. Error Tiers

Every node — agent, hook, system — can encounter errors. Errors are classified into
three tiers, handled differently. The transition function only sees tier 3; tiers 1
and 2 are handled entirely inside the node executor.

| Tier | Example | Handling | State change? |
|---|---|---|---|
| **1. Recoverable** | Transient network blip, malformed tool response, retryable API error, empty agent response | Continue the same session — same agent, same session handle, just nudge and retry the step | No |
| **2. Engine-change** | Quota hit, rate limit, model overloaded, engine crashed | Switch to next engine in `engine_preference` list and start a fresh session on the new engine. Detection is **explicit**: engine adapters raise typed exceptions (`QuotaExceeded`, `EngineOverloaded`, `ModelUnavailable`), never parsed from error text. | No |
| **3. Unrecoverable** | Agent hard-errors, repeated tier-1 retries exhausted, hook emits reject, guard violation, unhandled exception in system node | Emit an event to the transition function. For rejects → routed per table. For crashes → `recovering`. | Yes |

**Key points:**

- **Tier 1 is invisible to the state machine.** The executor handles it internally. No
  event, no transition, no counter bump at the state-machine level.
- **Tier 2 is also invisible to the state machine.** Engine fallback lives inside the
  node executor. The task doesn't change state just because we swapped from codex to
  claude. Only the engine history on the task changes.
- **Tier 3 is the only tier that produces events.** This is what the transition table
  routes.

Tiers 1 and 2 have their own internal budgets (e.g. "retry the same session 3 times,
then try next engine, then escalate to tier 3"). Those budgets are node-level
concerns, not state-machine concerns.

---

## 3. Attempt Limits (no loops)

The machine is designed so that every path from any non-terminal state reaches a
terminal state in a **bounded number of steps**.

| Counter | Scope | Limit | On exhaustion |
|---|---|---|---|
| `tier1_retry` | Per session, per node visit | N (e.g. 3) | Escalate to tier 2 |
| `tier2_engine_switches` | Per node visit | M (length of engine_preference list) | Escalate to tier 3 |
| `stage_retry[stage]` | Per stage (implementing, testing, accepting) | N (e.g. 3) | → recovering |
| `recovery_attempt[stage]` | **Per stage** — each stage can trigger recovery at most once | **1** | → failed (`recovery_budget_hit`) |
| `merge_attempt` | Commit stage only | **1** | → recovering |
| `pre_exec_recovery_attempt` | Per task, before executor picks it up | **1** | → failed (`pre_exec_recovery_failed`) |
| `hook_retry` | Per hook phase | **0** (no retry at state-machine level; tier 1/2 retries still apply inside the hook) | → reject event |

**Key rule: recovery is once per stage, not once per task.** `grooming` recovering
once and `testing` recovering once later are both legal — different counters.
`grooming` recovering twice is not.

Worst-case total: at most **7 recovery attempts ever per task** (5 agent stages +
commit + pre-exec), after which the task is `failed`. Typically far fewer.

---

## 4. Event Vocabulary

Single event type flowing into one pure transition function.

```python
class Event:
    # outcomes from a node (agent, hook, system, recovery)
    Pass
    Reject(source: "agent" | "hook" | "guard" | "system", reason: str)
    Blocked(reason: str)

    # tier-3 unrecoverable errors
    Crash(exc_type: str, message: str)
    Timeout

    # retry escalations (emitted by the runner, not by nodes directly)
    StageRetryLimitHit
    OverallRetryLimitHit

    # recovery outcomes (only emitted from `recovering`)
    RecoverySucceeded(resume: Stage | "done")
    RecoveryFailed(reason: str)
    RecoveryBudgetHit
```

No `Interrupt` event, no `Pause` event — stopping is not a state transition (see §7).

Auxiliary task fields:
- `stage` — current node in the state machine
- `origin_stage` — set on entry to `recovering`, cleared on exit
- `stage_retry[stage]` — per-stage retry counters
- `recovery_attempt[stage]` — per-stage recovery counters
- `session_handles[stage]` — session ids for resuming agent nodes
- `failed_reason`, `failed_message` — set only when transitioning to `failed`

---

## 5. Transition Table

**Format:** `state before | event | state after | comment`

Wildcards: `*` = any non-terminal stage phase. `<stage>` = the origin stage.

### Pre-execution recovery

Before the task enters the stage machine proper, a lightweight check runs. One-shot.

| State Before | Event | State After | Comment |
|---|---|---|---|
| ready | clean_state | before_grooming | Normal entry |
| ready | needs_pre_exec_recovery | recovering (pre-exec) | Dirty worktree, orphaned state, stale runner, etc. |
| recovering (pre-exec) | pre_exec_recovery_succeeded | before_<origin_stage> | Resume wherever the task was |
| recovering (pre-exec) | pre_exec_recovery_failed | failed (pre_exec_recovery_failed) | No retry |
| recovering (pre-exec) | pre_exec_recovery_budget_hit | failed (pre_exec_recovery_failed) | Second entry is a hard failure |
| recovering (pre-exec) | crash | failed (recovery_crashed) | Recovery itself died |

### Happy path

| State Before | Event | State After | Comment |
|---|---|---|---|
| before_grooming | hook_ok | grooming | Pre-stage hooks passed |
| grooming | pass | after_grooming | Agent approved |
| after_grooming | hook_ok | before_implementing | |
| before_implementing | hook_ok | implementing | |
| implementing | pass | after_implementing | Subject to guards first |
| after_implementing | hook_ok | before_testing | |
| before_testing | hook_ok | testing | |
| testing | pass | after_testing | |
| after_testing | hook_ok | before_accepting | |
| before_accepting | hook_ok | accepting | |
| accepting | pass | after_accepting | |
| after_accepting | hook_ok | before_commit | |
| before_commit | hook_ok | commit | |
| commit | pass | after_commit | Merge succeeded |
| after_commit | hook_ok | done | Terminal |

### Rejections (agent, hook, guard, or system — all uniform)

| State Before | Event | State After | Comment |
|---|---|---|---|
| before_grooming | reject | recovering | No earlier stage to roll back to |
| grooming | reject | recovering | Grooming can't self-retry |
| after_grooming | reject | recovering | |
| before_implementing | reject | implementing (retry) | |
| implementing | reject | implementing (retry) | |
| after_implementing | reject | implementing (retry) | |
| before_testing | reject | implementing (retry) | |
| testing | reject | implementing (retry) | Tests failed |
| after_testing | reject | implementing (retry) | |
| before_accepting | reject | implementing (retry) | |
| accepting | reject | implementing (retry) | Reviewer rejected |
| after_accepting | reject | implementing (retry) | |
| before_commit | reject | recovering | |
| commit | reject | recovering | Merge failed |
| after_commit | reject | recovering | Post-commit hook failed after merge landed |

**Uniform treatment:** all sources emit the same `reject` event. The transition
function decides routing based on current stage, not on who raised it.

### Blocked and retry escalations

| State Before | Event | State After | Comment |
|---|---|---|---|
| grooming | blocked | recovering | |
| implementing | blocked | recovering | |
| testing | blocked | recovering | |
| accepting | blocked | recovering | |
| implementing | stage_retry_limit_hit | recovering | |
| testing | stage_retry_limit_hit | recovering | |
| accepting | stage_retry_limit_hit | recovering | |
| * | overall_retry_limit_hit | recovering | Whole-task budget exhausted |

### Tier-3 unrecoverable errors

| State Before | Event | State After | Comment |
|---|---|---|---|
| * | crash | recovering | Unhandled exception after tier-1/tier-2 exhausted |
| * | timeout | recovering | Agent or hook ran too long |

### Entering `recovering`

Every non-terminal non-ready node (15 stage phases) can enter `recovering` via:
`crash`, `timeout`, `blocked`, `stage_retry_limit_hit`, `overall_retry_limit_hit`, or
`reject` from nodes that can't self-retry (grooming phases, commit phases).

On entry: `origin_stage := current`, `recovery_attempt[origin_stage] += 1`.

### Exiting `recovering`

| State Before | Event | State After | Comment |
|---|---|---|---|
| recovering | recovery_succeeded (resume = origin_stage) | before_<origin_stage> | Resume where we left off |
| recovering | recovery_succeeded (resume = other_stage) | before_<other_stage> | Recovery decided to back up |
| recovering | recovery_succeeded (done) | done | Recovery fixed and merged directly |
| recovering | recovery_failed | failed (recovery_exhausted) | Recovery gave up |
| recovering | recovery_budget_hit | failed (recovery_budget_hit) | Second attempt for same stage |
| recovering | crash | failed (recovery_crashed) | Recovery itself errored |
| recovering | timeout | failed (recovery_crashed) | Recovery hung |

---

## 6. Guards

Guards are pure predicates that can emit `Reject(source="guard", reason=...)` at
specific stage phases. They compose with hooks and agent verdicts.

Initial guard set:

| Guard | Applies To | Rejects When |
|---|---|---|
| `acceptance_criteria_present` | before_grooming, before_accepting | Task has no acceptance criteria |
| `non_empty_change_set` | after_implementing | Worktree has no diff AND no new tests |
| `no_hallucinated_files` | after_implementing | Agent claimed files, worktree clean for those paths |
| `worktree_clean_before_stage` | before_commit | Unexpected uncommitted state |

Guards are registered into the transition function; adding one is a single entry.

---

## 7. Stopping and Resuming

**Stopping is not a state change.** The task stays in whatever node it was in. There
is no `paused`, no `interrupted`, no "parked" state. The node itself *is* the resume
point.

### Clean stop (pool drain, operator stop, SIGTERM)

1. Runner receives stop signal.
2. Current hook or agent turn finishes (bounded by per-node timeout or grace period).
3. Runner persists `session_handles[stage]` for the current node.
4. Runner exits; task stays in current state with `running=false`.

On next pool start, the runner sees the task in e.g. `implementing`, loads
`session_handles["implementing"]`, and resumes the same agent session via
session-continue (engine-dependent).

### Hard stop (SIGKILL, host crash)

1. Process dies mid-phase without persisting.
2. Task's on-disk state is from the last checkpoint (phase entry).
3. On next pool start, the runner detects inconsistency (e.g. worktree dirty, last
   heartbeat stale) and routes the task through `ready → recovering (pre-exec)`.
4. Pre-exec recovery either salvages and resumes, or fails the task.

### Grace period

A shutdown signal gives each running node a configurable grace period (e.g. 60s) to
finish its current turn. If the grace expires, the runner force-kills the node and
marks the task for pre-exec recovery next time — i.e. it "downgrades" a clean stop to
a hard stop for that task only.

### Session continuation

Each agent node persists a session handle on entry (or on first turn). The runner
reads it on resume and passes it back to the engine. **All supported engines provide
session continuation** — there is no "restart fresh" fallback branch.

The recovery node also participates: a recovery session in progress when a stop hits
is resumed the same way.

---

## 8. Hooks

Hooks are external bash commands registered per phase in workspace config. Each hook
has:

- `command` — bash snippet to run
- `reject_on_failure: bool` — if true, non-zero exit emits `Reject(source="hook")`
- `timeout_seconds: int`
- `execution_mode: "fail_fast" | "run_all"` — when multiple hooks are registered

Hook phases (10 total):

| Phase | Hook Point |
|---|---|
| Pre-stage | `before_grooming`, `before_implementing`, `before_testing`, `before_accepting`, `before_commit` |
| Post-stage | `after_grooming`, `after_implementing`, `after_testing`, `after_accepting`, `after_commit` |

Notes:
- **Every stage has both pre and post hooks**, including commit.
- Post-stage hooks only fire on a `pass` from the stage body. Reject paths skip
  post-hooks.
- Hooks emit the same `Reject` event as agents and guards.

---

## 9. Recovery Agent

`recovering` is a first-class state, one per stage budget.

### Responsibilities

- Diagnose why the origin stage failed (via `RecoveryRequest` context)
- Attempt to repair — fix code, resolve merge conflict, patch task config, etc.
- Emit one of: `RecoverySucceeded(resume=...)`, `RecoveryFailed(reason=...)`,
  `RecoveryBudgetHit`

### Unified recovery request

Every recovery dispatch carries the same shape:

```python
RecoveryRequest(
    task_id: str,
    origin_stage: Stage,         # where we came from
    trigger_event: Event,        # what caused the transition
    failure_context: dict,       # last report, last exception, hook outputs, …
    recovery_attempt: int,       # always 1 in practice (budget is 1 per stage)
)
```

### Attempt limits (recap)

- **Per-stage:** 1 recovery attempt per stage.
- **Pre-execution:** 1 pre-exec attempt per task lifetime.
- **Merge:** 1 merge-conflict recovery attempt (the commit stage's budget).
- **No recursive recovery:** recovery crash/fail → `failed`.

### Merge conflicts

Merge conflicts are just one trigger into `recovering` from `commit`. The recovery
agent gets the conflict context and decides: resolve in place, back up to
implementing, or give up. No separate merge-agent sub-flow.

---

## 10. Core Classes

The design goal is that **the transition logic reads like the transition table**. All
complexity — tier-1/2 error handling, session continuation, engine fallback, hook
dispatch — is hidden behind a small number of abstractions.

### `Node` (base)

A node is anything the machine can be in. It knows how to execute itself and produces
an `Event`. Tier 1 and tier 2 errors are handled **inside** `run()` — the state
machine never sees them.

```python
class Node(ABC):
    name: str
    node_type: NodeType  # agent | hook | system | terminal

    @abstractmethod
    def run(self, ctx: TaskContext) -> Event:
        """Execute this node. Returns a single Event for the transition function.
        Tier-1 (retry same session) and tier-2 (switch engine) are handled
        internally — they never leak out as events."""

class TerminalNode(Node):
    """done, failed — run() is a no-op."""
    def run(self, ctx): return NoEvent
```

### `AgentNode`

Runs an AI agent. Owns the session handle, handles tier-1 retry and tier-2 engine
fallback internally. All the runner sees is one of: `Pass`, `Reject`, `Blocked`,
`Crash`, `Timeout`.

```python
class AgentNode(Node):
    node_type = NodeType.AGENT
    prompt_template: PromptTemplate
    engines: list[Engine]           # tier-2 fallback order
    tier1_budget: int               # e.g. 3 retries same session
    timeout: Duration

    def run(self, ctx: TaskContext) -> Event:
        session = ctx.session_store.get_or_create(self.name)
        for engine in self.engines:                  # tier 2
            for attempt in range(self.tier1_budget): # tier 1
                try:
                    verdict = engine.run_turn(session, self.prompt_template, ctx)
                    return self._verdict_to_event(verdict)
                except TransientError:
                    continue                          # tier 1: retry same session
                except QuotaError | EngineOverload:
                    break                             # tier 2: next engine
                except HardError as e:
                    return Crash(exc_type=type(e).__name__, message=str(e))
        return Crash(exc_type="AllEnginesExhausted", message="...")
```

**This is the only place engine fallback lives.** The transition function has no idea
engines even exist.

### `HookNode`

Runs one or more bash hooks for a phase. Same pattern: tier-1 retry (e.g. transient
shell failures), no tier-2, errors become events.

```python
class HookNode(Node):
    node_type = NodeType.HOOK
    phase: Phase                       # before_grooming, after_implementing, …
    hooks: list[HookSpec]
    execution_mode: ExecutionMode      # fail_fast | run_all

    def run(self, ctx: TaskContext) -> Event:
        results = self._run_all_or_fail_fast(ctx)
        if all(r.ok for r in results): return Pass
        if any(r.reject_on_failure and not r.ok for r in results):
            return Reject(source="hook", reason=self._summarize(results))
        return Pass  # failed hooks without reject_on_failure are logged but don't block
```

### `SystemNode`

Deterministic code (currently only `commit`). Same `run() → Event` contract.

```python
class CommitNode(SystemNode):
    def run(self, ctx: TaskContext) -> Event:
        try:
            self._merge_worktree(ctx)
            return Pass
        except MergeConflict as e:
            return Reject(source="system", reason=f"merge conflict: {e}")
        except GitError as e:
            return Crash(exc_type="GitError", message=str(e))
```

### `Event`

Sealed hierarchy. Only tier-3 outcomes. Dataclasses, immutable.

```python
@dataclass(frozen=True)
class Event: pass

@dataclass(frozen=True)
class Pass(Event): pass

@dataclass(frozen=True)
class Reject(Event):
    source: Literal["agent", "hook", "guard", "system"]
    reason: str

@dataclass(frozen=True)
class Blocked(Event):
    reason: str

@dataclass(frozen=True)
class Crash(Event):
    exc_type: str
    message: str

@dataclass(frozen=True)
class Timeout(Event): pass

@dataclass(frozen=True)
class RecoverySucceeded(Event):
    resume: NodeName | Literal["done"]

@dataclass(frozen=True)
class RecoveryFailed(Event):
    reason: str
```

### Coding the transitions

The transition table is a **list of `Rule` objects**, evaluated in order. First match
wins. The list is the single source of truth: the table in §5, the CLI inspection in
§12, the generated Mermaid diagram, and the runtime router all consume the same list.

```python
@dataclass(frozen=True)
class Rule:
    from_:       NodeName | frozenset[NodeName]   # single node or wildcard set
    event:       type[Event] | EventPattern       # event class, optionally with field match
    to:          NodeName | Callable[[Ctx], NodeName]
    when:        Callable[[Ctx], bool] | None = None   # guard predicate
    effect:      StateDelta | Callable[[Ctx], StateDelta] | None = None
    description: str = ""
```

`StateDelta` is a pure data patch applied by the runner after the transition — no
mutation inside the rule itself:

```python
@dataclass(frozen=True)
class StateDelta:
    set_fields:   dict[str, Any] = field(default_factory=dict)
    increment:    tuple[str, ...] = ()   # counter names to bump
    reset:        tuple[str, ...] = ()   # counter names to zero
```

### Example: the rules list

```python
from .events import Pass, Reject, Blocked, Crash, Timeout, CleanState
from .events import RecoverySucceeded, RecoveryFailed
from .deltas import enter_recovery, inc_stage_retry, clear_recovery_attempt
from .guards import mode, stage_retries_remaining, stage_retries_exhausted

ANY_STAGE_PHASE = frozenset({
    "before_grooming", "grooming", "after_grooming",
    "before_implementing", "implementing", "after_implementing",
    "before_testing", "testing", "after_testing",
    "before_accepting", "accepting", "after_accepting",
    "before_commit", "commit", "after_commit",
})

RULES: list[Rule] = [
    # ── pre-execution ────────────────────────────────────────────────
    Rule("ready", CleanState, "before_grooming",    when=mode("full")),
    Rule("ready", CleanState, "before_implementing", when=mode("single")),
    Rule("ready", NeedsPreExecRecovery, "recovering_pre_exec"),

    # ── happy path (full mode) ───────────────────────────────────────
    Rule("before_grooming",      HookOk, "grooming"),
    Rule("grooming",             Pass,   "after_grooming"),
    Rule("after_grooming",       HookOk, "before_implementing"),
    Rule("before_implementing",  HookOk, "implementing"),
    Rule("implementing",         Pass,   "after_implementing"),
    Rule("after_implementing",   HookOk, "before_testing",  when=mode("full")),
    Rule("after_implementing",   HookOk, "before_commit",   when=mode("single")),
    Rule("after_implementing",   HookOk, "done",
         when=mode("single") & zero_change_shortcut(),
         description="Single mode: skip commit if no diff"),
    Rule("before_testing",       HookOk, "testing"),
    Rule("testing",              Pass,   "after_testing"),
    Rule("after_testing",        HookOk, "before_accepting"),
    Rule("before_accepting",     HookOk, "accepting"),
    Rule("accepting",            Pass,   "after_accepting"),
    Rule("after_accepting",      HookOk, "before_commit"),
    Rule("before_commit",        HookOk, "commit"),
    Rule("commit",               Pass,   "after_commit"),
    Rule("after_commit",         HookOk, "done"),

    # ── rejections that self-retry ───────────────────────────────────
    Rule("implementing", Reject, "implementing",
         when=stage_retries_remaining("implementing"),
         effect=inc_stage_retry("implementing")),
    Rule("implementing", Reject, "recovering",
         when=stage_retries_exhausted("implementing"),
         effect=enter_recovery),
    Rule("testing", Reject, "implementing",
         when=stage_retries_remaining("testing"),
         effect=inc_stage_retry("testing")),
    Rule("testing", Reject, "recovering",
         when=stage_retries_exhausted("testing"),
         effect=enter_recovery),
    Rule("accepting", Reject, "implementing",
         when=stage_retries_remaining("accepting"),
         effect=inc_stage_retry("accepting")),
    Rule("accepting", Reject, "recovering",
         when=stage_retries_exhausted("accepting"),
         effect=enter_recovery),

    # ── rejections that escalate directly to recovery ────────────────
    Rule("grooming",       Reject, "recovering", effect=enter_recovery),
    Rule("before_commit",  Reject, "recovering", effect=enter_recovery),
    Rule("commit",         Reject, "recovering", effect=enter_recovery),
    Rule("after_commit",   Reject, "recovering", effect=enter_recovery),

    # ── blocked ──────────────────────────────────────────────────────
    Rule(ANY_STAGE_PHASE, Blocked, "recovering", effect=enter_recovery),

    # ── tier-3 runtime errors (wildcards) ────────────────────────────
    Rule(ANY_STAGE_PHASE, Crash,   "recovering", effect=enter_recovery),
    Rule(ANY_STAGE_PHASE, Timeout, "recovering", effect=enter_recovery),

    # ── exiting recovery ─────────────────────────────────────────────
    Rule("recovering", RecoverySucceeded, resume_origin,  effect=clear_recovery_attempt),
    Rule("recovering", RecoveryFailed,    "failed",       effect=fail("recovery_exhausted")),
    Rule("recovering", Crash,             "failed",       effect=fail("recovery_crashed")),
    Rule("recovering", Timeout,           "failed",       effect=fail("recovery_crashed")),
]
```

### The transition function

Thirty lines, nothing clever:

```python
def transition(current: NodeName, event: Event, ctx: Ctx) -> Transition:
    for rule in RULES:
        if not _matches_from(rule.from_, current):       continue
        if not _matches_event(rule.event, event):        continue
        if rule.when is not None and not rule.when(ctx): continue

        target = rule.to(ctx) if callable(rule.to) else rule.to
        delta  = rule.effect(ctx) if callable(rule.effect) else (rule.effect or EMPTY_DELTA)
        return Transition(next=target, delta=delta, rule=rule)

    raise NoTransitionError(current, event)


def _matches_from(pattern, current):
    return current in pattern if isinstance(pattern, frozenset) else pattern == current

def _matches_event(pattern, event):
    if isinstance(pattern, type):
        return isinstance(event, pattern)
    return pattern.matches(event)   # EventPattern for source-matching on Reject, etc.
```

### Why this shape

- **Readable like the table.** Each `Rule(...)` line is one row. Grouping with
  section comments preserves the structure of §5.
- **Order-sensitive, explicitly.** Specific rules come before wildcards. First
  match wins. No precedence magic.
- **Pure.** `transition()` has no I/O. It doesn't even mutate the context — it
  returns a `Transition` with a `StateDelta` that the runner applies after
  persisting.
- **Counters and mode-gating live in guards, not in the runner.** The runner
  never checks "am I out of retries" — it asks `transition()`, which picks the
  right rule based on the `when` predicate.
- **Effects are data.** `StateDelta` is inspectable, testable, serializable. No
  callback pyramid.
- **One source of truth.** `RULES` is a module-level constant. The CLI, diagram
  generator, dashboard API, and invariant tests all import it.

### How side-effects compose

`enter_recovery`, `inc_stage_retry`, `fail(...)` are tiny helpers that return a
`StateDelta`:

```python
def enter_recovery(ctx: Ctx) -> StateDelta:
    return StateDelta(
        set_fields={"origin_stage": ctx.current_stage},
        increment=(f"recovery_attempt.{ctx.current_stage}",),
    )

def inc_stage_retry(stage: NodeName) -> Callable[[Ctx], StateDelta]:
    def _effect(ctx: Ctx) -> StateDelta:
        return StateDelta(increment=(f"stage_retry.{stage}",))
    return _effect

def fail(reason: str) -> Callable[[Ctx], StateDelta]:
    def _effect(ctx: Ctx) -> StateDelta:
        return StateDelta(set_fields={"failed_reason": reason})
    return _effect
```

### How guards compose

Guards are also small functions returning bool. They combine with `&` / `|`:

```python
def mode(m: PipelineMode) -> Guard:
    return lambda ctx: ctx.pipeline_mode == m

def stage_retries_remaining(stage: NodeName) -> Guard:
    return lambda ctx: ctx.stage_retry.get(stage, 0) < ctx.limits.stage_retry_limit

def stage_retries_exhausted(stage: NodeName) -> Guard:
    return lambda ctx: not stage_retries_remaining(stage)(ctx)

def zero_change_shortcut() -> Guard:
    return lambda ctx: ctx.last_report.files_changed == 0 and ctx.last_report.tests_added == 0
```

### Where complexity goes

The router is pure and tiny. Everything else lives in typed abstractions:

| Concern | Lives in |
|---|---|
| Rules | `transitions.py` (the `RULES` list) |
| Guards | `guards.py` (predicate factories) |
| Deltas | `deltas.py` (side-effect factories) |
| Events | `events.py` (sealed dataclass hierarchy) |
| Tier-1/2 error handling | Inside `Node.run()` subclasses — invisible to `transition()` |
| Engine fallback | Inside `AgentNode.run()` — invisible to `transition()` |
| Session continuation | Inside `SessionStore` — invisible to `transition()` |
| Persistence | Inside `Runner` via `Persistence.save()` — invisible to `transition()` |

Reading `transitions.py` top to bottom gives you the entire behavior of the state
machine.

### `Runner` (orchestrator)

The only class that connects pure transitions to effects. This is where the simplicity
of the design pays off:

```python
class Runner:
    def __init__(self, registry: NodeRegistry, persistence: Persistence):
        self.registry = registry
        self.persistence = persistence

    def run_task(self, task: Task) -> None:
        while not self._is_terminal(task.stage):
            node = self.registry.get(task.stage)
            ctx = self._build_context(task)

            event = node.run(ctx)
            trans = transition(task.stage, event, ctx)

            self._apply_side_effect(task, trans.side_effect)
            task.stage = trans.next
            self.persistence.save(task)

            if self._stop_requested():
                return  # task stays in trans.next; resumes next time
```

**That's the entire loop.** Every complication lives inside either `node.run()` (tier
1/2, session handles, engine fallback) or `transition()` (routing rules, counter
guards). The runner itself is ~20 lines of actual logic.

### `RecoveryRequest` / `RecoveryAgent`

Recovery is just another `AgentNode`, but it's instantiated with the context of the
origin stage. The transition table routes to a single `recovering` node; the runner
builds a fresh `RecoveryAgent` instance each time with the origin-stage context.

```python
class RecoveryAgent(AgentNode):
    origin_stage: NodeName
    trigger_event: Event
    failure_context: FailureContext

    def _verdict_to_event(self, verdict) -> Event:
        if verdict.outcome == "resume":
            return RecoverySucceeded(resume=self.origin_stage)
        if verdict.outcome == "advance":
            return RecoverySucceeded(resume=verdict.target_stage)
        if verdict.outcome == "done":
            return RecoverySucceeded(resume="done")
        return RecoveryFailed(reason=verdict.reason)
```

### `Session` (continuation)

Hidden behind `session_store`. Runner doesn't care about the details.

```python
class SessionStore:
    def get_or_create(self, node_name: NodeName) -> Session: ...
    def persist(self, node_name: NodeName, session: Session) -> None: ...

class Session:
    engine_session_id: str | None
    conversation_id: str | None
    turn_count: int
    def resumable(self) -> bool: ...
```

### Package layout

```
litehive/pipeline/
├── __init__.py
├── nodes/
│   ├── base.py          # Node, NodeType, NodeRegistry
│   ├── agent.py         # AgentNode (tier 1/2 handling)
│   ├── hook.py          # HookNode
│   ├── system.py        # SystemNode, CommitNode
│   └── terminal.py      # TerminalNode
├── events.py            # Event hierarchy
├── transitions.py       # TRANSITION_TABLE + transition()
├── guards.py            # Guard predicates
├── recovery.py          # RecoveryAgent, RecoveryRequest
├── sessions.py          # SessionStore
├── runner.py            # Runner (the 20-line loop)
└── persistence.py       # Task state writes
```

### Readability payoff

Anyone reading the state machine should be able to:

1. **Read the transition table** in §5 and understand what happens on every event.
2. **Read `Runner.run_task()`** (20 lines) and understand the whole loop.
3. **Drill into a specific node** (e.g. `AgentNode.run()`) only if they care about
   tier 1/2 error handling.

Nothing about engines, sessions, quotas, prompts, or timeouts appears in the
transition table or the runner. That complexity is fully encapsulated in `Node`
subclasses.

---

## 11. Pipeline Modes

Tasks run in one of two modes, set at creation time and stored as `pipeline_mode` on
the task.

| Mode | Stage sequence |
|---|---|
| `full` | grooming → implementing → testing → accepting → commit → done |
| `single` | implementing → commit → done (or straight to done if zero changes) |

`single` mode is for trivial tasks where grooming, testing, and accepting add
overhead without value (e.g. doc typo fix, formatter-only change).

### How the state machine decides

The transition table is shared between modes. Only three edges are mode-sensitive,
and the transition function reads `ctx.pipeline_mode` to pick the target:

| From | Event | mode=full | mode=single |
|---|---|---|---|
| ready | clean_state | before_grooming | before_implementing |
| after_implementing | hook_ok | before_testing | before_commit |
| after_implementing | hook_ok + zero_change_shortcut | before_testing (guard doesn't fire) | done |

Plus one mode-scoped guard:

| Guard | Applies To | Mode | Fires When |
|---|---|---|---|
| `single_mode_zero_change_shortcut` | after_implementing | single only | Worktree has no diff AND no new tests → short-circuit to `done` |

Everything else — hooks, recovery, error tiers, counters, session continuation — is
identical between modes. No alternate transition table. No code fork.

---

## 12. Rule Inspection

Because the transition table is pure data, it's easy to expose for inspection by
operators and tests.

### CLI

```
litehive pipeline transitions [--node <name>] [--mode <full|single>]
```

Prints every `(state, event) → next_state` row. Filterable.

### Diagram generation

```
litehive pipeline graph --format mermaid > docs/state-machine-diagram.md
```

Emits a Mermaid diagram generated from the same `list_transitions()` function. The
generated diagram is committed to `docs/` so it stays in sync with the table. A
pre-commit hook or CI check regenerates it and fails if it drifts.

### Invariant tests

A unit test iterates the table and asserts:

- Every non-terminal node has at least one outgoing edge for every event it can emit.
- Every `Crash` and `Timeout` event routes somewhere (no silent dead-ends).
- No unreachable nodes from `ready`.
- No node can loop to itself without incrementing a counter that has a finite limit.
- Recovery is never recursive (no edge from `recovering` back to `recovering`).

These invariants catch routing bugs at test time, not in production.

---

## 13. Retry Budgets and Grace Period (decided)

### Tier-1 / tier-2 budgets — per node, reset on entry

Each node gets a fresh tier-1 and tier-2 budget every time it is entered. Budgets do
not carry across nodes, even within the same stage visit. When `before_implementing`
exhausts its tier-1 budget and escalates to tier-2 (engine switch), that has no
effect on the budget `implementing` will get when it runs next.

Rationale: budgets model "how much should *this* piece of work try before giving
up?" — a property of the node, not of the task or the stage visit. Sharing budgets
across nodes invites weird interactions (a noisy hook burning the agent's retry
budget).

### Grace period — global default, per-node override

Workspace config sets a global `grace_period_seconds` (default **120s**). Individual
nodes can declare their own:

```python
class TestingNode(AgentNode):
    grace_period_seconds = 600   # long test suites need more time
```

On clean stop, the runner asks the current node to finish its turn and waits up to
its grace period. If exceeded, force-kill and mark the task for pre-exec recovery
next time (downgrades this task's stop from clean to hard).
