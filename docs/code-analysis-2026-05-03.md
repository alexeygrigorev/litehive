# Code Analysis — 2026-05-03

Cross-cutting analysis backing
`docs/feedback-2026-05-03.md`. The feedback was recorded against a
small set of files; this doc lists every place across the codebase
where each pattern recurs, so we can fix them everywhere instead of
just where the recording happened to land.

Search dates: 2026-05-03. Counts in this doc reflect that snapshot.

## P1. `from __future__ import annotations`

Rule: remove everywhere (R2 in feedback). The project targets a
modern Python; the import is no-op or harmful for runtime
introspection.

11 occurrences:

- `litehive/recovery/execution_recovery.py:3`
- `litehive/recovery/detection.py:3`
- `litehive/recovery/workspace_repair.py:3`
- `litehive/fs_cleanup.py:3`
- `litehive/agents/sandbox_support.py:3`
- `litehive/observability/venv_health.py:3`
- `litehive/config/runtime_settings.py:3`
- `litehive/config/registry.py:3`
- `litehive/tasks/event_log.py:3`
- `litehive/tasks/audit.py:3`
- `litehive/state/rebuild_safety.py:3`

Fix: delete each line. Verify ruff/tests pass after each batch. No
type errors expected because `target-version = "py311"` already
treats annotations as evaluated lazily *only* where the import was
controlling that — and in practice none of these modules use
forward references that require lazy evaluation.

## P2. Inline imports

Rule: imports go to the top of the file (R1). Inline only when the
import is genuinely heavy or breaks a circular dependency that
cannot be untangled by reorganization. Each remaining inline import
must carry a `# inline because ...` comment, and `ruff` must reject
new ones.

Total: ~148 inline imports across `litehive/`. Hotspots, by raw
count:

- `litehive/state/locking.py` — 14
- `litehive/tasks/queue.py` — 21
- `litehive/state/persist.py` — 6
- `litehive/recovery/execution_recovery.py` — 9
- `litehive/tasks/status.py` — 16
- `litehive/main.py` — 3 (CLI fast path — likely justified, document)
- `litehive/worktree.py` — 3
- `litehive/cli/pipeline_cli.py` — 6

Justified inlines (keep, but annotate):

- `litehive/main.py` `fast_status` and the per-subcommand branches in
  `main()` exist precisely to avoid loading the full Click/Typer CLI
  on the fast path. Mark them `# inline: keep CLI cold start fast`.

Likely unjustified (hoist):

- All inlines in `litehive/state/locking.py`,
  `litehive/state/persist.py`, `litehive/tasks/queue.py`,
  `litehive/tasks/status.py`, and
  `litehive/recovery/execution_recovery.py`. Many are inside helper
  functions importing modules from the same package — pure
  organization noise. A small number may need the inline form to
  break cycles between `state` and `tasks`; resolve those by moving
  the shared types into `domain` rather than papering over with
  inlines.

Process: for each file, attempt to hoist; if hoisting causes a real
import cycle, leave the inline and add the comment. Then add a
ruff rule (e.g. `PLC0415` if available, or a custom check) to fail
new inline imports.

## P3. `# type: ignore` annotations

Rule: investigate each ignore; remove by fixing the underlying type
(R5).

10 occurrences, all `# type: ignore[arg-type]` for the same root
cause: callers pass `str` where `PipelineState` is expected (or vice
versa), papering over the impedance mismatch instead of typing the
boundary correctly.

- `litehive/lifecycle/transitions.py:109`, `:123` —
  `# type: ignore[assignment]` on event downcasts. Replace with a
  proper `match` or with `cast(...)` once we add a real check.
- `litehive/lifecycle/orchestration.py:654`, `:714` —
  `pipeline_state=report_stage  # type: ignore[arg-type]`. Same
  root cause.
- `litehive/tasks/recovery_reports.py:32` —
  `runnable_state=runnable_state  # type: ignore[arg-type]`.
- `litehive/state/records.py:272`, `:359` — `pipeline_mode` and
  `stage` typed as strings.
- `litehive/cli/runner.py:384` — `verdict=verdict  # type: ignore[arg-type]`.
- `litehive/agents/parsing.py:34`, `:50` — `pipeline_state=stage
  # type: ignore[arg-type]`. Called out directly in the recording.

Fix shape: walk the call graph upstream from each ignore. Replace
the parameter type on the receiver with the domain enum. Then push
the conversion (string → enum) to the boundary where the value
*actually* enters the system from the outside (DB row, JSON, CLI
arg). The `canonical_pipeline_state(...)` helper in
`litehive/domain/common.py` is the right tool for that boundary.

## P4. String-typed stage / verdict / mode / role values

Rule: never compare or store domain values as raw strings (R4). The
domain layer in `litehive/domain/common.py` already defines
`PipelineState`, `TaskStage`, `TaskStatus`, `PipelineStatus`,
`PipelineMode`, etc. Use them.

Top offending modules (by raw count of literal stage strings such as
`"implementing"`, `"grooming"`, etc.):

- `litehive/lifecycle/prompt_serializer.py` — 24
- `litehive/config/profiles/defaults.py` — 18 (probably YAML
  defaults — verify, may be acceptable as serialized form)
- `litehive/agents/prompts.py` — 16
- `litehive/domain/lifecycle_deltas.py` — 14
- `litehive/agents/manager.py` — 13
- `litehive/roles/base.py` — 11
- `litehive/roles/recovery.py` — 10
- `litehive/lifecycle/heru_factory.py` — 10
- `litehive/tasks/queue.py` — 6
- `litehive/tasks/normalization.py` — 6
- `litehive/lifecycle/orchestration.py` — 5
- `litehive/config/engine_models.py` — 5
- `litehive/tasks/status.py` — 3

Definitions in `litehive/domain/common.py` are not violations —
that file *defines* the strings. Same for any literal under
`tests/` that's verifying a serialized payload. Everywhere else,
swap.

Particularly egregious patterns to hit first:

- `litehive/lifecycle/prompt_serializer.py:485-494` — a giant
  `if name in {"before_grooming", "grooming", ...}: return "grooming"`
  chain. Replace with a method on `PipelineState` or a
  `TaskStage`-returning helper.
- `litehive/lifecycle/heru_factory.py` — nine occurrences of
  `if state.stage == "recovering":` and similar. Use
  `PipelineState.RECOVERING`.
- `litehive/tasks/status.py:282`, `:719` — string set tests against
  pipeline status. Use the enum sets in `domain.common`.
- `litehive/tasks/normalization.py:74-106` — string returns and set
  membership. Switch to enums.

## P5. `subprocess.run([...git...])` outside the git module

Rule: there is one allowed home for git invocation,
`litehive/git/ops.py` (R9). Every other module should call into it.
The recording explicitly called out the `worktree.py` situation.

Current status: done. The remaining direct
`subprocess.run(["git", ...])` calls are in `litehive/git/ops.py`,
with `litehive/sandbox/git_wrapper.py` only checking for the `git`
binary before it delegates. Do not reintroduce raw git subprocess
calls outside `litehive/git/ops.py`; add missing helpers there instead.

Original finding: the usage was the opposite of that rule; `git/ops.py`
existed but was bypassed almost everywhere git was called.

Original direct subprocess git calls that were migrated:

- `litehive/worktree.py` — ~25 separate `subprocess.run(["git", ...])`
  invocations covering worktree add/list/prune, fetch, merge,
  status, stash, branch -D, cherry-pick, commit. The whole file
  needs a sweep.
- `litehive/lifecycle/nodes/system.py` — ~14 calls (rev-parse,
  add, commit, status, ls-files, merge, cherry, diff, etc.).
- `litehive/recovery/scope_analysis.py` — ~7 calls (diff,
  cat-file, stash push/pop, checkout, show).
- `litehive/tasks/status.py:617` — `git diff --quiet`.
- `litehive/daemon/execution.py:52` — `["git", *args]`.
- `litehive/sandbox/git_wrapper.py:111` — sandbox-specific
  formatting; might stay if it's about the sandboxed mock, but
  audit.
- `litehive/agents/sandbox.py:232` — `shutil.which("git")`. Might
  stay.

Migration plan:

1. Audit `litehive/git/ops.py` and add the missing helpers (status
   variants, stash, cherry-pick, worktree management, etc.).
2. Replace each call site with the typed helper. Each helper should
   raise `GitError` instead of returning a `CompletedProcess`.
3. Delete the per-site retry/parse logic that becomes redundant.

Also fold in: the recording mentioned that `cd_out` (i.e. `git -C`)
is sometimes available but not used. The new helpers should always
take an explicit `cwd: Path` so we never `os.chdir`.

## P6. Files too large

Rule: break up files that have grown past readable size. The
recording singled out `worktree.py` (~1400 lines).

Top offenders (`wc -l` over `litehive/**`):

- `litehive/worktree.py` — 1404
- `litehive/tasks/status.py` — 1280
- `litehive/lifecycle/orchestration.py` — 895
- `litehive/tasks/queue.py` — 873
- `litehive/observability/status_diagnostics.py` — 834
- `litehive/observability/status.py` — 770
- `litehive/lifecycle/nodes/system.py` — 756
- `litehive/recovery/execution_recovery.py` — 723
- `litehive/lifecycle/prompt_serializer.py` — 699
- `litehive/tasks/event_log.py` — 681
- `litehive/lifecycle/heru_factory.py` — 650
- `litehive/agents/manager.py` — 648

Plan: convert each to a package (`litehive/<name>/`). Suggested
splits:

- `worktree.py` → `worktree/{__init__.py, manager.py, sync.py,
  branches.py, cherry_pick.py, locks.py}`. Move
  merge-resolver-agent code out into `litehive/agents/`. Move git
  calls into `litehive/git/ops.py` (P5). Move dataclasses into
  `litehive/domain/` if they describe domain state, or into the
  new sub-modules if they're internal.
- `tasks/status.py` → split status mutation, status query, and
  resume/recovery into separate modules.
- `lifecycle/orchestration.py` → split orchestration-loop
  scaffolding from per-stage helpers.
- `tasks/queue.py` → split queue mutation, queue read, eligibility,
  and runtime store interactions.

Do *not* batch all of these together. One file at a time, behind a
green test suite, with structural-only commits separated from
behavior changes.

## P7. `if x is not None:` deep blocks vs. early return

Rule: prefer early-return + flat continuation (R3).

Hotspots in the files the recording covered:

- `litehive/agents/parsing.py:21`, `:29` — both are exactly this
  shape and were called out by name.
- `litehive/agents/manager.py:300`, `:320`, `:329`, `:342`, `:350`,
  `:354`, `:418`, `:509` — multiple nested guards inside the
  callback machinery. Refactor as: `if x is None: return`, then
  continue at the outer level.
- `litehive/agents/prompts.py:128`, `:197`, `:362`, `:367`.

Beyond those files, treat this as a code-style addition (write it
into `docs/code-style.md`) and apply opportunistically when
touching surrounding code.

## P8. Subagent / artifact deletion

Rule: do not delete debug artifacts on the success path (R6).

Sites (`litehive/agents/artifacts.py`):

- Line 16: `compressed_path.unlink()` when content is empty after a
  format-flip from gzip → plain.
- Line 22: same idea, plain → gzip switch.
- Line 26: format-flip cleanup.
- Lines 50, 54: same in `write_text_artifact`.
- Lines 60–61: `remove_text_artifact` deletes both variants.
- Line 64: `prune_superseded_subagent_artifacts` deletes prior
  attempts.

Caller: `litehive/agents/manager.py:417` calls
`prune_superseded_subagent_artifacts(...)` whenever a new
subagent attempt starts.

Plan:

1. Stop calling `prune_superseded_subagent_artifacts` from
   `manager.py`. Keep the file's prior-attempt artifacts.
2. Either delete `prune_superseded_subagent_artifacts` outright (no
   callers other than the ones we are removing) or leave it behind
   guarded only by an explicit retention policy in config. Default
   is "keep".
3. The `.unlink()` calls in `write_*` functions exist to reconcile
   format flips (gzip ↔ plain). Those are fine to keep — they're
   not deleting subagent debug evidence, they're switching the
   storage form for a single artifact. Leave them as is, but add a
   docstring saying so.

## P9. Prompts living in code

Rule: long prompt bodies move to templates, build via a typed
`PromptBuilder` (R7).

Affected files:

- `litehive/agents/prompts.py` (372 lines, mostly literal scaffold
  lines and per-stage logic). Primary target.
- `litehive/lifecycle/prompt_serializer.py` (699 lines, also
  string-heavy).
- `litehive/roles/*.py` (`base.py` 11 stage strings,
  `recovery.py` 10) — also build text fragments inline.

Plan (sequenced, do not bundle):

1. Add a `templates/prompts/` directory and a Jinja2 dependency.
2. Extract one stage's prompt at a time. Each extraction lands as
   its own commit with a regression test that asserts the rendered
   text matches the previous baseline (golden file).
3. Once all stages are extracted, refactor the inputs into a
   typed `PromptBuilder` (rename existing `stage_prompt(...)` to
   `PromptBuilder(...).render(stage)`).
4. Delete the now-empty branches in `agents/prompts.py`.

Until step 1 lands, `agents/prompts.py` should at least:

- get docstrings on each helper explaining what the field is for
  (R8 — cross-references P11);
- drop the `task_type` field from the prompt (the recording flagged
  this as useless);
- replace `workspace_content: str` with a typed object (a small
  dataclass that knows how to render itself);
- move `_stage_owner_for_stage` (the prose lookup) onto
  `TaskStage` as a property — `TaskStage.GROOMING.owner_label`.

## P10. Agent-related code outside `litehive/agents/`

Rule: things that *run* a subagent must live next to the other
agents (R10).

Concrete site flagged by the recording:

- `litehive/worktree.py` invokes the merge-resolver agent inline.
  Extract to `litehive/agents/merge_resolver.py` (or wherever the
  other roles already live — check `litehive/roles/`); have
  `worktree.py` call into it.

Worth reviewing in the same pass:

- `litehive/lifecycle/nodes/agent.py` is fine where it is (it's
  the lifecycle node, not an agent runner).
- `litehive/cli/agent_cli.py` and
  `litehive/cli/runner.py` — keep as CLI wiring.

## P11. Missing docstrings on helper functions

Rule: small helper functions must answer "what problem does this
solve?" (R8).

Highest-priority files (recording flagged the first one
explicitly; the others are similar density):

- `litehive/agents/artifacts.py` — every public function has zero
  documentation. Add one-line docstrings before doing any other
  work in this file (it gates the deletion-removal cleanup in P8,
  because we need to know which ones are dead).
- `litehive/agents/parsing.py` — `stage_report_from_subagent`
  needs a real docstring. The current first-line comment is OK but
  it's not a docstring.
- `litehive/agents/prompts.py` — the `_runner_hook_*` and
  `_stage_*` helpers are unclear (recording quote: "Format runner
  hook prompt entry hook top section — what is this?").
- `litehive/git/ops.py` — partial coverage; add docstrings as we
  fold the new helpers in (P5).

## P12. Attention persistence on disk vs. DB

Rule: SQLite is the source of truth. No more file-based attention
log (recording quote about `attention.py`).

Current status: done. `litehive/attention.py` reads and writes the
SQLite `attention_log` table, and daemon/worktree/sandbox diagnostics
append through that module. Keep new attention surfaces on the DB path;
do not recreate `.litehive/attention/` file writes.

Original sites:

- `litehive/attention.py` — central file.
- `.litehive/attention/` directory writes (gitignored, per
  `.gitignore:18`).
- `litehive/observability/` may also touch the attention log —
  audit before changes.

Plan:

1. Locate the attention table (or add one) in
   `litehive/db/schema.py`.
2. Switch attention writes/reads in `attention.py` to DB.
3. Delete the file-based plumbing.
4. Verify `litehive status` / `litehive health` still surface the
   same information.

## P13. `fast_status` naming

Rule: rename `fast_status` to `status` (recording).

Single site: `litehive/main.py:59`. The fast/slow distinction is
gone; `--full` is the only remaining variant and it's handled in
the same dispatcher. Rename + update call sites.

## P14a. Package `__init__.py` re-exports cause real import cycles

While hoisting inline imports in `litehive/observability/status.py`
we hit a concrete cycle: `daemon/__init__.py` re-exports
`start_background_daemon` / `stop_workspace_daemon` from
`daemon.execution`, which means *importing anything from
`litehive.daemon.<submodule>`* runs `daemon/__init__.py` and pulls
in `daemon.execution` — even when the caller only wants something
unrelated like `daemon.logs.latest_run_all_log_dir`. Because
`daemon.execution` imports `observability.status`, the cycle bites.

This pattern recurs across the codebase: package `__init__.py`
files re-export a "public surface" that drags in heavy modules.
The user-recorded preference is that `__init__.py` is the import
surface only, with no behavior — typer apps, CLI logic, and
side-effecting helpers live in a named sibling module.

Fix shape:

1. Remove the imports from `litehive/daemon/__init__.py`. Update
   each caller to import directly from `daemon.execution` /
   `daemon.registry`.
2. Sweep the rest of the package `__init__.py` files in
   `litehive/`. Keep only docstrings; move re-exports out.
3. After the sweep, attempt the inline-import hoist in
   `observability/status.py` again — the cycle should be gone.

This unblocks P2 (inline import hoist) for several hot files and
makes module dependencies legible at the import statement instead
of "you have to know which `__init__.py` re-exports what".

## P14. Static type checking is not enforced

The codebase has 10+ `# type: ignore[arg-type]` lines (P3) and the
recent fixes show many of them existed because no checker was
running CI-side to catch the regressions. We should adopt a static
type checker — strong candidates:

- **pyrefly** (Meta) — Rust-backed, very fast, tuned for monorepo
  ergonomics. Good fit for a project where speed matters more than
  exhaustive Pythonic-edge coverage.
- **pyright / basedpyright** — well-known, broad ecosystem support,
  good IDE integration.
- **mypy** — slower, baseline coverage, the conservative pick.

Recommendation: **pyrefly** because it is fast and we will be
running it on every PR.

Adoption plan (mechanical, low-risk to ship gradually):

1. Add the checker as a dev dependency. Pin a version.
2. Land a baseline config (strict on `litehive/domain/`,
   `litehive/lifecycle/`, and `litehive/agents/`; lenient elsewhere
   for now).
3. Bake the existing errors into a baseline file so CI is green
   from day one. New code must not add to the baseline.
4. Add a CI step that runs the checker on PRs.
5. Burn down the baseline file over time. Each `# type: ignore`
   removed (P3) drops one entry from the baseline.
6. Remove escape hatches (`# type: ignore`, `Any`, missing
   annotations) only after the checker is wired up — otherwise a
   later refactor will re-introduce them and we won't notice.

This sits between the style sweeps (P1, P2) and the larger
refactors (P5, P6, P9): it raises the floor so future cleanup
doesn't regress.

## Sequencing for execution

The recording asked for "step by step, never break things". Order:

1. **Style sweep, mechanical, low risk.** Drop
   `from __future__ import annotations` (P1). Add the early-return
   rule to `docs/code-style.md` (R3 → P7). Run tests.
2. **Naming.** Rename `fast_status` → `status` (P13). Run tests.
3. **Add `ruff` rule for inline imports.** First, fix the inline
   imports that aren't justified (P2). Annotate the rest. Then
   enable the rule.
3a. **Adopt a static type checker (pyrefly).** Pin it, land a
    baseline file, wire CI. Once enabled, the type-ignore burndown
    in P3 has automated guardrails — anything we remove can't slip
    back in unnoticed.
4. **Stop deleting debug artifacts.** Remove the
   `prune_superseded_subagent_artifacts` call from
   `agents/manager.py` (P8). Add docstrings to
   `agents/artifacts.py` (P11). Optionally delete the now-unused
   prune function.
5. **Fix `agents/parsing.py`.** Drop the `root: Path | None`
   default; require it. Apply early-return. Move the verdict set
   to domain. Plumb the type properly so the
   `# type: ignore[arg-type]`s die (P3). Run tests.
6. **Adjust nudge / non-completion handling.** Verify the lifecycle
   node reuses the session ID and the parser does not turn a
   missing-verdict into a `Reject` upstream of it.
6a. **Introduce the DI container and burn down `root: Path`.** Done
    to the intended boundary-only state. `litehive/container.py`
    owns the production path-to-dependency conversion, with
    lightweight workspace-only assembly for read-only paths that must
    not load config. CLI / daemon / lifecycle / agent / task /
    worktree helpers now receive `Workspace`, config, the container,
    or focused services instead of rebuilding them from raw roots.
    Constructors that previously built collaborators
    (`SubagentManager`, `HeruEngineAdapter`, `RuntimeStore`) now
    receive dependencies explicitly; production factories assemble
    them. Remaining `Workspace.from_path` / `load_config` hits are the
    container, config loading, workspace config caching, and
    `runtime_store(root)` factory boundaries.
7. **Centralize git.** Done. Raw git subprocess calls now live behind
   `litehive/git/ops.py` (P5).
8. **Move attention to DB**. Done. Attention writes/readbacks now use
   the SQLite `attention_log` table (P12).
9. **Move merge-resolver agent out of `worktree.py`** (P10).
10. **Split `worktree.py` into a package** (P6).
11. **Prompt extraction** (P9). Templates first, builder second.
12. **String-typed domain values** (P4). In progress. The small task
    normalization/status outcome slices have been migrated to typed
    `PipelineStatus`, `OutcomeKind`, and `OutcomeReasonCode` values.
    Continue file by file before tackling
    `lifecycle/prompt_serializer.py`.

Each step lands as its own commit. Tests must be green between
steps. UI / CLI smoke checks (`litehive --help`,
`litehive status`) must succeed at every step.
