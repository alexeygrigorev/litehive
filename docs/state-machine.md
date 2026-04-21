# Task State Machine

This document describes the Litehive task lifecycle state machine, including status transitions, recovery behavior, and operator commands.

## Core Task States

Litehive tasks follow an explicit state machine with well-defined transitions. The main states are:

### Execution States
- **`queued`** - Waiting in the execution queue for an available runner
- **`in_progress`** - Currently executing in a pipeline stage  
- **`interrupted`** - Execution stopped unexpectedly, eligible for automatic recovery
- **`parked`** - Intentionally paused by operator, requires explicit action to resume

### Terminal States
- **`done`** - Successfully completed all pipeline stages
- **`flagged`** - Requires manual operator attention before continuing
- **`merge_failed`** - Failed during git merge operation
- **`cancelled`** - Operator intentionally stopped and closed the task

### Closure States
- **`wont_do`** - Task is not worth implementing
- **`deferred`** - Task should wait for later implementation  
- **`duplicate`** - Another task already covers the same work

## Parked vs Interrupted: Key Distinction

The most important distinction in the task lifecycle is between **parked** and **interrupted** tasks:

### Interrupted Tasks
- **Cause**: System failures, crashes, timeouts, or other unexpected stops
- **Recovery**: Automatically restored to queue by workspace recovery
- **Reason**: Set by system with technical failure details
- **Intent**: Should resume execution without operator intervention

### Parked Tasks  
- **Cause**: Intentional operator action via `litehive stop` or `litehive park`
- **Recovery**: Stay out of automatic recovery; require explicit operator action
- **Reason**: Set to "Task parked via CLI command from {stage} stage"
- **Intent**: Should remain paused until operator decides to resume

## State Transitions

### Normal Execution Flow
```
queued → in_progress → done
```

### Interruption Handling
```
in_progress → interrupted → (auto-recovery) → queued → in_progress
```

### Explicit Parking
```
in_progress → parked → (manual resume) → queued → in_progress
```

### Failure Handling
```
in_progress → flagged → (manual intervention) → queued/cancelled
in_progress → merge_failed → (manual merge fix) → queued/cancelled
```

## CLI Command Semantics

### Stop Commands
- **`litehive stop`** - Parks the currently running task (status = "parked")
- **`litehive park {task-id}`** - Explicitly parks a specific task

### Resume Commands  
- **`litehive resume {task-id}`** - Returns task to queue at current pipeline stage
- **`litehive requeue {task-id}`** - Restarts task from implementation entry stage

### Key Differences
- **Resume**: Continues from where it left off (current `pipeline_status`)
- **Requeue**: Full restart from implementation entry point

## Recovery Behavior

### Automatic Recovery
The workspace recovery system (`recover_stale_runner_state()`) automatically handles:

- **Interrupted tasks**: Restored to queue for continued execution
- **Stale processes**: Detected and marked as interrupted, then restored
- **Crashed runners**: Marked as interrupted with failure context

### Manual Recovery
Requires explicit operator action:

- **Parked tasks**: Must use `litehive resume` or `litehive requeue`  
- **Flagged tasks**: Need diagnosis and manual intervention
- **Merge conflicts**: Require manual git conflict resolution

## Task Eligibility Rules

The `is_task_eligible_for_execution()` function determines which tasks can be automatically restored:

**Eligible for automatic execution:**
- `queued` - Normal queue processing
- `in_progress` - Already running
- `flagged` - Can be retried after manual review
- `interrupted` - System failures eligible for recovery

**Not eligible (require manual action):**
- `parked` - Intentionally paused by operator
- `done` - Already completed  
- `cancelled` - Explicitly closed
- `wont_do`, `deferred`, `duplicate` - Permanent closure states

## Implementation Details

### Status Field
The `task.status` field (TaskStatus enum) is the primary state indicator and drives all recovery logic.

### Interruption Metadata
Both interrupted and parked tasks have `runtime.interruption` metadata for resume functionality:

**Interrupted tasks:**
```python
RuntimeInterruptionState(
    source="runner",
    reason="System failure details...",  
    resume_stage="implementing",
    # ... system failure context
)
```

**Parked tasks:**
```python  
RuntimeInterruptionState(
    source="runner",
    reason="Task parked via CLI command from {stage} stage",
    resume_stage="{current_stage}",
    # ... minimal metadata for resume
)
```

### Queue Restoration
The `restore_missing_queued_tasks()` function only restores tasks that are `eligible_for_execution()`, ensuring parked tasks stay out of automatic recovery while interrupted tasks get restored.

This explicit state-based approach replaces the previous magic string detection and provides clear, deterministic behavior for task lifecycle management.