# Litehive Task State Machine

This is the desired state machine design. Tasks should reference this document.

## Pipeline Modes

### Full Pipeline (default)
```
grooming → implementing → testing → accepting → commit_to_git → done
```

### Single Pipeline (`--single`)
```
implementing → commit_to_git → done
```
If implementing produces no file changes, skip commit_to_git and go straight to done.

## Stage Owners

| Stage | Owner | Purpose |
|-------|-------|---------|
| grooming | planner | Clarify scope, shape acceptance criteria |
| implementing | swe | Write code |
| testing | qa | Verify the implementation |
| accepting | reviewer | Final done/not-done judgment |
| commit_to_git | system | Merge worktree into main |

## Stage Transitions

### Grooming
- pass → implementing
- blocked → flagged (missing info, can't proceed)

### Implementing
- pass (with file changes) → testing
- pass (no files, no tests) → REJECT back to implementing (guard)
- no CLI report submitted → continue session with nudge
- timeout/crash → see "Failure Handling"

### Testing
- pass → accepting
- reject → implementing (retry)
- retry limit hit → see "Failure Handling"

### Accepting
- pass → commit_to_git
- reject → implementing (retry)
- retry limit hit → see "Failure Handling"

### Commit to Git
- merge succeeds → done
- merge conflict → merge agent (once)
  - resolved → done
  - failed → recovery agent (once)
    - resolved → done
    - failed → recovery_failed
- no new commits → fail → recovery agent (once)

## Verdict Submission

Agents MUST submit verdicts via `litehive report` CLI. No text parsing.

1. Agent runs and invokes `litehive report --verdict <pass|fail|reject|blocked>`
2. If agent finishes without invoking CLI → continue session, nudge with prompt
3. If after nudge still no report → verdict is fail

## Failure Handling

When an agent fails, classify the failure by exit code:

### Exit Code Classification
| Exit Code | Meaning | Action |
|-----------|---------|--------|
| 0 (no CLI report) | Agent forgot to report | Continue session, nudge |
| 0 (with CLI report) | Normal completion | Use the reported verdict |
| 1 (error) | Could be task bug or engine bug | Try recovery agent |
| 124 (timeout) | Engine problem | Retry with fallback engine |
| Signal (killed) | Infrastructure problem | Retry same engine |
| Quota/limit message | Engine exhausted | Switch to next engine in preference list |

### Engine Failure Flow
```
engine fails (timeout, quota, crash)
  → try next engine in engine_preference list
    → next engine also fails → try next
      → all engines exhausted → recovery_failed
```

### Task Failure Flow
```
agent ran but couldn't complete the task
  → launch recovery agent (once, prefer different engine)
    → recovery solves it → continue pipeline
    → recovery fails → recovery_failed
```

### Merge Conflict Flow
```
git merge → conflict
  → merge agent (once)
    → resolved → done
    → failed → recovery agent (once)
      → resolved → done
      → failed → recovery_failed
```

## Task States

### Active States
| State | Meaning |
|-------|---------|
| queued | Waiting in pool for execution |
| in_progress | Currently being worked on |

### Terminal States (success)
| State | Meaning |
|-------|---------|
| done | Completed and merged to main |

### Terminal States (failure)
| State | Meaning |
|-------|---------|
| flagged | Needs operator attention |
| recovery_failed | Recovery agent tried and failed, manual only |

### Resumable States
| State | Meaning |
|-------|---------|
| interrupted | Stopped mid-execution, can resume |
| parked | Deliberately paused by operator |

### Closed States
| State | Meaning |
|-------|---------|
| wont_do | Explicitly decided not to do |
| deferred | Postponed to later |
| duplicate | Duplicate of another task |

## Pool Behavior

- Pool NEVER stops on task failure
- Failed tasks are flagged/recovery_failed and pool moves to next task
- Pool stops only for: queue exhausted, operator checkpoint, dirty git, quota threshold

## Engine Selection

1. CLI `--engine` override (highest priority)
2. Workspace `default_engine` from config

When engine fails, walk the `engine_preference` list skipping the failed engine.

## Recovery Philosophy

- Be conservative with recovery_failed — most things are recoverable
- Engine failures → switch engine, not recovery agent
- Task failures → recovery agent with different engine
- Only declare recovery_failed when genuinely exhausted all options
- recovery_failed tasks are left for manual resolution
