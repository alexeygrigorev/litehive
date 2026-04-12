# Pipeline Monitoring & Debugging

## What you're monitoring

The litehive daemon (`litehive start`) runs v2 pipeline tasks autonomously via `litehive run` subprocesses. Each task goes through: `ready → worktree_sync → grooming → implementing → testing → accepting → commit → done`. Agents (codex) execute at each stage. The state machine is defined in `litehive/pipeline/rules.py`.

## Periodic check (every 3-5 minutes)

```bash
litehive status                          # active task, runner status, queue
litehive pipeline journal <task_id>      # v2 state + transitions for the active task
```

Healthy signs: transitions growing, stage advancing, no wedged `recovering`, no `failed`.

## When something is wrong

### Task stuck in one stage for 10+ minutes with no stdout activity

```bash
stat -c '%Y' .litehive/tasks/<task-slug>/subagents/SA-*/stdout.log  # last write timestamp
date +%s                                                              # compare with now
ps -ef | grep 'codex exec' | grep -v grep                           # is codex alive?
```

If stdout is stale and codex is dead, the run crashed. Check daemon logs: `litehive logs --daemon`.

### Task in `recovering`

Check `litehive pipeline journal <task_id>` for the crash/reject event that triggered it. If recovery agent fails, the task goes to `failed`. Check `failed_reason` and `failed_message`.

### Task in `failed`

Read the journal to trace what happened. Common causes:

- `recovery_exhausted` — recovery agent couldn't fix it. Investigate the failure_context.
- `recovery_crashed` — recovery agent itself errored. Check recovery agent logs.
- `pre_exec_recovery_failed` — stale worktree or broken state before pipeline started.

## Fixing things

**Small, scoped fix (one function, clear bug):** fix directly, commit, push. The daemon picks up the fix on next iteration.

**Reset a stuck task:** `litehive pipeline reset <task_id>` clears all v2 state so it starts fresh from `ready`.

**Change a task's stage:** `litehive pipeline set-state <task_id> <stage>` (e.g., reset to `ready` or skip to `implementing`).

**Bugs the recovery agent can't fix:** create a litehive task with `litehive task add "..." --goal "..." --acceptance-criteria "..."`.

## Key files

| File | Purpose |
|---|---|
| `litehive/pipeline/rules.py` | The transition table — read this to understand routing |
| `litehive/pipeline/stages.py` | Stage constants linking to node classes |
| `litehive/pipeline/orchestration.py` | `run_task()` — wires up the full stack |
| `litehive/pipeline/heru_factory.py` | Engine adapter — translates heru to v2 |
| `litehive/pipeline/agents/swe.py` | SWE prompt (boy scout rule, no self-reject) |
| `litehive/pipeline/agents/recovery.py` | Recovery agent prompt (log-pulling instructions) |
| `litehive/cli/agent_cli.py` | Restricted agent CLI (role-based verdict enforcement) |

## Known issues to watch for

1. **Wrong step on verdict** — fixed in `83ee87bf` but older running agents may still use stale v1 `pipeline_status`. Symptom: verdict in comments.yaml has wrong step, NudgeRequired fires, crash.
2. **Ruff hook rejects** — the `after_implementing` hook runs `ruff check` on the entire codebase. If any file has lint errors (even ones the SWE didn't touch), the hook rejects. SWE should fix them (boy scout rule) but sometimes can't.
3. **Dirty worktree on sync** — `worktree_sync` skips merge when worktree has uncommitted changes. If the SWE's prior run left WIP, the task resumes on the stale base.
