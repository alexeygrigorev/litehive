# Prompt Context Management Plan

## Problem

When agents get rejected and retry, they need enough context to understand
what went wrong and fix it — but not so much that they blow their output
budget before submitting a verdict. T-0294 failed because 18 prior subagent
attempts produced a 24KB thread that overwhelmed the agent.

### Current prompt structure (what the engine receives)

```
Task: T-XXXX — title
Stage: implementing
Role: swe
Pipeline mode: full

Instructions:
  ## Role guidance (SWE instructions, ~2KB)
  ## all:startup (recovery guidance)
  ## swe:startup (if configured)
  ## profile (if configured)

Goal: <from task.yaml>
Acceptance criteria: <from task.yaml>
Plan: <from task.yaml>
Constraints: <from task.yaml>

Last rejection: <source, phase, reason>  ← only on retry

Discussion thread:                       ← THIS IS THE PROBLEM
  [grooming] planner (pass): ...
  [implementing] swe (pass): ...
  [testing] qa (reject): ...             ← repeated per attempt
  [implementing] swe (pass): ...
  ...

Checks that will reject your work:
  - uv run ruff check ...
  - uv run pytest -q

IMPORTANT: submit verdict via --message-file
```

### What's already fixed

- Thread trimming (`12656ac7`): keeps grooming pass + last per (step, verdict)
  pair + caps at 4000 chars. This prevents the T-0294-style blowup.

### What's still wrong

1. **Rejection reason is duplicated.** The `last_rejection` section has the
   reason, AND the thread has the same qa/reviewer reject entry. The agent
   sees the same info twice.

2. **Prior pass messages are useless on retry.** When the SWE retries after
   a QA reject, it doesn't need to see its own prior "pass" messages — it
   needs the rejection reason and its own code diff.

3. **Thread doesn't distinguish "relevant to this stage" vs "historical."**
   A SWE retry cares about: (a) grooming scope, (b) the rejection it needs
   to fix. It does NOT need the reviewer's pass from a different task cycle.

4. **No summary of prior work done.** The agent knows it's on "stage retry
   attempt: 1" but doesn't know what files it already changed. It has to
   re-discover this by running `git diff`.

5. **Engine-level context not managed.** Codex accumulates its own internal
   context across the session (via continuation/thread_id). If the session
   is long, codex may hit its own context limits even with a lean prompt.

## Proposed changes

### 1. Stage-aware thread filtering (prompt_serializer.py)

Replace the current "last per (step, verdict)" heuristic with stage-aware logic:

```python
def _relevant_thread_for_stage(thread, current_stage):
    """For a given stage, return only the entries that matter."""
    relevant = []
    
    # Always: grooming pass (sets scope)
    relevant += [e for e in thread if e["step"] == "grooming" and e["verdict"] == "pass"]
    
    # For implementing retry: the rejection that sent us back
    if current_stage == "implementing":
        # Last testing/accepting reject, or last hook reject
        for e in reversed(thread):
            if e["verdict"] == "reject" and e["step"] in ("testing", "accepting"):
                relevant.append(e)
                break
    
    # For testing: the implementing pass (what the SWE claims it did)
    if current_stage == "testing":
        for e in reversed(thread):
            if e["step"] == "implementing" and e["verdict"] == "pass":
                relevant.append(e)
                break
    
    # For accepting: implementing pass + testing pass
    if current_stage == "accepting":
        for e in reversed(thread):
            if e["step"] == "implementing" and e["verdict"] == "pass":
                relevant.append(e)
                break
        for e in reversed(thread):
            if e["step"] == "testing" and e["verdict"] == "pass":
                relevant.append(e)
                break

    return relevant
```

### 2. Rejection message deduplication

When `last_rejection` is set, don't repeat the same entry in the thread
section. The serializer should skip any thread entry that matches
`last_rejection.source` + `last_rejection.reason`.

### 3. Work-done summary for retries

When `stage_retry > 0`, include a compact "Prior work" section:

```
Prior work from your last attempt:
  Files changed: litehive/config/__init__.py, litehive/config/paths.py, ...
  Tests status: 496 passed, 5 skipped (from after_implementing hook)
  Rejection reason: (already in last_rejection section)
```

This comes from `state.last_report` which already tracks `files_changed`
and `tests`. No new infrastructure needed — just serialize it.

### 4. Message length caps per entry

Individual thread messages should be capped at 500 chars (with truncation
marker). The grooming pass often contains a full TASK_UPDATE block that's
1-2KB — only the first paragraph matters for the SWE.

### 5. Session continuity vs fresh start

When retrying after a reject, the pipeline currently reuses the same
engine session (via `continuation.thread_id`). This means the engine
already has the prior context in its own memory. So the prompt doesn't
need to repeat what the engine already saw — it just needs the NEW info
(the rejection reason).

Decision: **on stage retry, start a fresh session** if the rejection was
from a different agent (QA/reviewer). The fresh session avoids stale
context accumulation. Keep session continuity only for nudge retries
(same agent, same turn).

## T-0294 case study (what the failing agent actually saw)

14 thread entries, 24KB total. For the SWE on retry, here's what was
relevant vs waste:

| Entry | Role | Verdict | Relevant? | Why |
|-------|------|---------|-----------|-----|
| 1 | planner | pass | YES (scope only) | Acceptance criteria + plan. But message is 2KB; only ~300 chars matter. |
| 2 | recovery | comment | NO | "stale_runner_recovery" bookkeeping |
| 3 | recovery | pass | NO | Infrastructure diagnosis from old crash |
| 4 | qa | reject | MAYBE | Old rejection — superseded by last_rejection if retrying from a newer reject |
| 5 | recovery | comment | NO | Another stale_runner recovery |
| 6 | recovery | pass | NO | Another recovery diagnosis |
| 7 | recovery | comment | NO | Another stale_runner recovery |
| 8 | recovery | pass | NO | Another recovery diagnosis |
| 9 | reviewer | pass | NO | From a prior cycle that was rolled back |
| 10 | planner | pass | NO | Second grooming (duplicate of entry 1) |
| 11 | swe | pass | NO | Prior SWE's claim — irrelevant on retry |
| 12 | swe | pass | NO | Another prior SWE's claim |
| 13 | recovery | pass | NO | Recovery from a different cycle |
| 14 | recovery | pass | NO | Recovery from a different cycle |

**Verdict: 1 entry relevant out of 14.** The SWE only needs:
- Grooming scope (300 chars of entry 1)
- The specific rejection reason (already in `last_rejection`)

Everything else is noise that consumed budget.

### Rules for what to include per stage

| Current stage | Include | Exclude |
|--------------|---------|---------|
| implementing (retry) | Grooming pass (truncated), last_rejection | All recovery entries, old SWE passes, old QA rejects, reviewer passes |
| testing | Last implementing pass | Everything else |
| accepting | Last implementing pass + last testing pass | Everything else |
| recovering | The crash/rejection that triggered recovery, last implementing pass | Old recovery entries, grooming details |
| grooming | Nothing (first stage, no history needed) | Everything |

## Implementation steps

1. **Stage-aware filtering** — replace `_trim_thread_for_prompt` with
   `_relevant_thread_for_stage`. ~30 lines.

2. **Dedup rejection** — skip thread entries matching last_rejection.
   ~5 lines in `_thread_section`.

3. **Per-entry message cap** — truncate individual messages to 500 chars.
   ~3 lines.

4. **Work-done summary** — serialize `state.last_report` into the prompt
   when `stage_retry > 0`. ~15 lines in `serialize_prompt`.

5. **Fresh session on cross-agent retry** — in `AgentNode._run_with_retries`,
   clear `session.engine_session_id` when the retry source is a different
   agent's rejection (not a nudge or hook). ~5 lines.

## Expected impact

- T-0294-style budget exhaustion: eliminated (thread capped + stage-aware)
- SWE retry efficiency: improved (sees only the rejection, not history)
- QA/reviewer context: improved (sees only implementing pass, not old cycles)
- Total prompt size: predictable upper bound (~4KB thread max → ~2KB with
  stage-aware filtering)
